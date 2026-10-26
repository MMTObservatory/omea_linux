"""
The running system: acquisition, products, science, sensors and publishing.

Threads:

* acquisition (the caller's thread): day/night mode, iris, sensor cooling,
  exposure control, one exposure after another, each converted to the
  Skywatch-format cube and queued;
* products: writes the FITS file, renders the JPEGs and keogram, updates the
  latest-image files, and submits night frames to the science pool;
* board: polls the RS232 sensor board, appends the weather CSV, publishes;
* web (optional, see :mod:`omea.web`): read-only status pages.

Science (skycam_utils photometry and sky brightness) runs in a process pool so
a slow frame never delays the next exposure.
"""

import logging
import math
import queue
import threading
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import board as board_mod
from . import outputs, pointing, render, science
from .camera import AbortedError, CameraError, open_camera
from .clock import Clock
from .ephem import Site
from .exposure import ExposureController
from .fitsout import build_header, write_fits_bz2
from .heater import DewGuard
from .imaging import raw_to_cube

log = logging.getLogger(__name__)

# the redis keys agreed with the MMTO software group (June 2026); all floats,
# "nan" when there is no measurement
SCIENCE_KEYS = ("allsky_mv_zenith", "allsky_mv_tucson", "allsky_mv_nogales",
                "allsky_mv_target", "allsky_extinction_median",
                "allsky_extinction_target", "allsky_extinction_lost_fraction",
                "allsky_fog_index", "allsky_dome_fog")

LIVE_FIELDS = ["time", "file", "exposure", "gain", "fratio", "sun_alt", "moon_alt",
               "sky_level", "ccd_temp", "cooler_power", "allsky_mv_zenith",
               "allsky_mv_tucson", "allsky_mv_nogales", "allsky_mv_target",
               "target_az", "target_alt", "allsky_mv_best", "best_az", "best_alt",
               "nstars", "ndetected", "ext_g", "ext_g_all", "fog_index"]
EXTINCTION_FIELDS = ["time", "median", "target", "lost", "fog"]
KEOGRAM_FILE = "omea_keogram.npz"       # the day's strip so far, kept across restarts


@dataclass
class Status:
    """Everything the web pages show; written by the threads, read by the server."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    started: float = field(default_factory=time.time)
    state: str = "starting"
    camera: str = ""
    mode: str = ""
    sun_alt: float = float("nan")
    moon_alt: float = float("nan")
    fratio: float = 0.0
    focus: int = 0
    exposure: float = 0.0
    gain: int = 0
    sky_level: float = float("nan")
    ccd_temp: float = float("nan")
    cooler_power: float = float("nan")
    cooler_target: float = float("nan")
    frames: int = 0
    dropped: int = 0
    last_frame: str = ""
    last_frame_time: float = 0.0
    board: dict = field(default_factory=dict)
    science: dict = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=6000))
    weather: deque = field(default_factory=lambda: deque(maxlen=3000))
    extinction: deque = field(default_factory=lambda: deque(maxlen=2000))
    log: deque = field(default_factory=lambda: deque(maxlen=200))
    images: dict = field(default_factory=dict)        # name -> JPEG bytes

    def update(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self):
        with self.lock:
            return {k: getattr(self, k) for k in (
                "started", "state", "camera", "mode", "sun_alt", "moon_alt", "fratio",
                "focus", "exposure", "gain", "sky_level", "ccd_temp", "cooler_power",
                "cooler_target", "frames", "dropped", "last_frame", "last_frame_time",
                "board", "science")}


class StatusLogHandler(logging.Handler):
    def __init__(self, status):
        super().__init__(logging.INFO)
        self.status = status
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                            "%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        self.status.log.append(self.format(record))


def _since(rows, since):
    """The CSV ``rows`` whose ISO ``time`` is at or after ``since`` (naive = UT)."""
    out = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) >= since:
            out.append(r)
    return out


def _jpeg_bytes(img, quality):
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class Monitor:
    def __init__(self, cfg, clock=None, publish=True):
        self.cfg = cfg
        self.clock = clock or Clock()
        self.site = Site(cfg.site)
        self.status = Status()
        self.stop_event = threading.Event()
        self.queue = queue.Queue(maxsize=3)
        self.redis = outputs.Redis(cfg.redis) if publish else outputs.Redis(
            type(cfg.redis)(enabled=False))
        self.camera = None
        self.board = None
        self.controller = ExposureController(cfg.exposure, cfg.image)
        self.projector = None
        self._projector_epoch = None
        self.keogram = render.Keogram()
        self.dew = DewGuard(cfg.board)
        self._fog_recent = deque(maxlen=max(1, cfg.science.fog_frames))
        self.dome_fog = False
        self._lost = math.nan               # stars lost from the latest extinction map
        self._fog_high_since = None         # when the fog index last rose above fog_on
        self.pool = None
        self._threads = []
        self._live_lock = threading.Lock()
        self._last_latest = 0.0
        self._last_keogram_save = time.monotonic()
        self._last_extinction = 0.0
        self._extinction_future = None
        self._science_live = False
        self._cooler_setpoint = None
        self._cooler_hot_since = None
        self._cooler_warming = False
        self._count = 0
        logging.getLogger("omea").addHandler(StatusLogHandler(self.status))

    # -- life cycle ----------------------------------------------------------

    def stop(self, *_):
        log.info("stopping")
        self.stop_event.set()

    def run(self, max_frames=None):
        cfg = self.cfg
        self._load_history()
        if cfg.science.enabled:
            self.pool = ProcessPoolExecutor(max_workers=cfg.science.workers)
        self._start_thread(self._products_loop, "products")
        if cfg.board.enabled:
            self._start_thread(self._board_loop, "board")
        try:
            self._acquire(max_frames)
        finally:
            self.stop_event.set()
            self.queue.put(None)
            for t in self._threads:
                t.join(timeout=60)
            self._save_keogram()
            if self.pool is not None:
                self.pool.shutdown(wait=True, cancel_futures=False)
            if self.camera is not None:
                self.camera.close()
            if self.board is not None:
                if self.dew.state != "auto":
                    self._force_heater("auto", "stopping")
                self.board.close()
            self.status.update(state="stopped")

    def _load_history(self):
        """Refill the web plots from tonight's files, so a restart loses nothing."""
        now = self.clock.now()
        day = self.site.day_dir(now)
        day_dir = Path(self.cfg.output.data_dir) / day
        noon = datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=self.site.tz)
        # the night plots run from 00:00 UT, which (west of Greenwich) can be in
        # yesterday's directory: start from whichever of the two is earlier
        since = min(noon, now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0,
                                                                microsecond=0))
        frames, extinction = [], []
        for d in sorted({self.site.day_dir(since), day}):
            frames += _since(outputs.read_csv(Path(self.cfg.output.data_dir) / d
                                              / "omea_live.csv"), since)
            extinction += _since(outputs.read_csv(Path(self.cfg.output.data_dir) / d
                                                  / "omea_extinction.csv"), since)
        weather = (outputs.read_weather(self.cfg.output.weather_csv, since.timestamp(),
                                        self.site.tz)
                   if self.cfg.output.weather_csv else [])
        with self.status.lock:
            self.status.history.extend(frames)
            self.status.extinction.extend(extinction)
            self.status.weather.extend(weather)
        self._seed_exposure(frames, now)
        if self.keogram.load(day_dir / KEOGRAM_FILE, day):
            keo = self.keogram.image()
            with self.status.lock:
                self.status.images["keogram"] = _jpeg_bytes(keo, 90)
            log.info("keogram picked up from %s", day_dir / KEOGRAM_FILE)
        if frames or extinction or weather:
            log.info("loaded %d frames, %d extinction maps and %d sensor readings since %s",
                     len(frames), len(extinction), len(weather), since.isoformat(timespec="minutes"))

    def _seed_exposure(self, frames, now, max_age=900):
        """Start the exposure from the last frame, if it is recent (a restart)."""
        if not frames:
            return
        last = frames[-1]
        try:
            age = (now - datetime.fromisoformat(last["time"])).total_seconds()
            if not 0 <= age < max_age:
                return
            self.controller.seed(float(last["exposure"]), float(last["gain"]),
                                 float(last["fratio"]), float(last["sky_level"]))
        except (KeyError, TypeError, ValueError):
            return
        log.info("exposure starts from the last frame (%s s, gain %g, f/%g, sky %g ADU)",
                 last["exposure"], last["gain"], last["fratio"], last["sky_level"])

    def _start_thread(self, target, name):
        t = threading.Thread(target=self._guard, args=(target,), name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _guard(self, target):
        try:
            target()
        except Exception:                                      # noqa: BLE001
            log.exception("%s thread died; stopping", threading.current_thread().name)
            self.stop_event.set()

    # -- hardware --------------------------------------------------------------

    def _open_hardware(self):
        cfg = self.cfg
        if self.board is None and cfg.board.enabled:
            try:
                self.board = board_mod.open_board(cfg.board)
            except Exception as err:                           # noqa: BLE001
                log.error("sensor board unavailable (%s); continuing without iris/focus", err)
                self.board = None
            if self.board is not None:
                try:
                    # Skywatch's start-up sequence: open the iris, home the focus
                    # and drive it to the reference position
                    self.board.set_iris(cfg.mode.night_fratio)
                    self.board.focus_to(cfg.mode.focus_position)
                    self.status.update(focus=cfg.mode.focus_position)
                except Exception as err:                       # noqa: BLE001
                    log.error("lens initialisation failed: %s", err)
                try:
                    self._configure_heater()
                except Exception as err:                       # noqa: BLE001
                    log.error("dome heater set-up failed: %s", err)
        self.camera = open_camera(cfg.camera, cfg.image, tz=self.site.tz, clock=self.clock)
        self.status.update(camera=self.camera.name)

    def _configure_heater(self):
        """Store the configured heater settings in the board, if they differ."""
        b = self.cfg.board
        if b.heater_mode not in ("dew", "auto", "on", "off"):
            raise ValueError(f"heater_mode must be dew, auto, on or off, not {b.heater_mode!r}")
        auto = b.heater_mode in ("dew", "auto")
        temp, rh = self.board.get_config()
        data = self.board.read()
        # thresholds come back through the ADC conversion: compare loosely
        same = (abs(temp - b.heater_temp) < 0.5 and abs(rh - b.heater_rh) < 1.0
                and data.auto_heat == auto and data.heater_power == b.heater_power)
        if not same:
            self.board.set_config(b.heater_temp, b.heater_rh, auto, b.heater_power)
            log.info("dome heater: %s, below %.1f C and above %.0f %% RH, power %d %% "
                     "(was %s, %.1f C, %.0f %%, %d %%)", b.heater_mode, b.heater_temp,
                     b.heater_rh, b.heater_power, "auto" if data.auto_heat else "manual",
                     temp, rh, data.heater_power)
        if not auto:
            self.board.set_heater(b.heater_mode == "on")

    def _force_heater(self, state, reason):
        """dew mode: force the heater "on" or "off", or hand it back to the board ("auto")."""
        b = self.cfg.board
        try:
            self.board.set_config(b.heater_temp, b.heater_rh, state == "auto", b.heater_power)
            if state != "auto":
                self.board.set_heater(state == "on")
        except Exception as err:                               # noqa: BLE001
            log.warning("dome heater switch failed: %s", err)
            self.dew.state = self.dew.previous  # try again at the next reading
            return
        if state == "auto":
            log.info("dome heater back to automatic: %s", reason)
        else:
            log.info("dome heater %s: %s", state, reason)

    def _set_iris(self, fratio):
        """Drive the iris; returns the f-ratio now in front of the sensor."""
        current = getattr(self.board, "iris", None) if self.board else None
        if self.board is None or current == fratio:
            return current or fratio
        try:
            self.board.set_iris(fratio)
            self.controller.change_aperture(current, fratio)
            log.info("iris f/%s -> f/%s", current, fratio)
            return fratio
        except Exception as err:                               # noqa: BLE001
            log.error("iris change failed: %s", err)
            return current or fratio

    def _manage_cooling(self, sun_alt):
        c = self.cfg.camera
        cam = self.camera
        if not (c.cooling and cam.has_cooler):
            return
        want = sun_alt < c.cooling_sun_altitude
        temp = cam.temperature()
        power = cam.cooler_power()
        now = time.monotonic()
        if self._cooler_setpoint is None:
            if not want:
                self.status.update(ccd_temp=temp, cooler_power=power,
                                   cooler_target=float("nan"))
                return
            log.info("cooling on, ramping %.1f -> %.1f degC", temp, c.target_temperature)
            self._cooler_setpoint = temp
            self._cooler_last = now
        # ramp the set point gently both ways, as Skywatch does, to spare the Peltier
        step = c.cooling_ramp * (now - self._cooler_last) / 60.0 * self.clock.speed
        self._cooler_last = now
        if not want:
            # warm up at the same rate; switch off once the cooler is idle
            if not self._cooler_warming:
                log.info("cooling off: warming at %.1f degC/min (Sun at %.1f deg)",
                         c.cooling_ramp, sun_alt)
                self._cooler_hot_since = None
            self._cooler_warming = True
            target = self._cooler_setpoint + step
            if (math.isfinite(power) and power <= c.cooler_off_power) or target >= c.warm_temperature:
                log.info("cooler off at %.1f degC (power %.0f%%)", temp, power)
                cam.set_cooler(False)
                self._cooler_setpoint = None
                self._cooler_warming = False
                self.status.update(ccd_temp=temp, cooler_power=power,
                                   cooler_target=float("nan"))
                return
        else:
            if self._cooler_warming:
                log.info("cooling on again, ramping %.1f -> %.1f degC",
                         self._cooler_setpoint, c.target_temperature)
            self._cooler_warming = False
            target = max(self._cooler_setpoint - step, c.target_temperature)
            # back off when the cooler cannot hold the set point
            if math.isfinite(power) and power >= c.max_cooler_power:
                self._cooler_hot_since = self._cooler_hot_since or now
                if now - self._cooler_hot_since > 300 / self.clock.speed:
                    target = self._cooler_setpoint + 1.0
                    self._cooler_hot_since = now
                    log.warning("cooler at %.0f%% power; set point raised to %.1f degC",
                                power, target)
            else:
                self._cooler_hot_since = None
        if round(target) != round(self._cooler_setpoint) or not cam_cooling(cam):
            cam.set_cooler(True, target)
        self._cooler_setpoint = target
        self.status.update(ccd_temp=temp, cooler_power=power, cooler_target=target)

    # -- acquisition -----------------------------------------------------------

    def _acquire(self, max_frames):
        cfg = self.cfg
        ic = cfg.image
        failures = 0
        while not self.stop_event.is_set():
            if self.camera is None:
                try:
                    self._open_hardware()
                    failures = 0
                except Exception as err:                       # noqa: BLE001
                    failures += 1
                    wait = min(300, 10 * failures)
                    log.error("cannot open camera: %s (retry in %d s)", err, wait)
                    self.status.update(state=f"camera unavailable: {err}")
                    self.clock.sleep(wait, self.stop_event)
                    continue

            now = self.clock.now()
            sun_alt = self.site.sun_altitude(now)
            moon_alt, moon_az = self.site.moon_altaz(now)
            night = sun_alt < cfg.mode.night_sun_altitude
            fratio = self._set_iris(cfg.mode.night_fratio if night else cfg.mode.day_fratio)
            try:
                self._manage_cooling(sun_alt)
            except Exception as err:                           # noqa: BLE001
                log.warning("cooling control: %s", err)
            setting = self.controller.setting(night, fratio)
            self.status.update(state="exposing", mode="night" if night else "day",
                               sun_alt=sun_alt, moon_alt=moon_alt, fratio=fratio,
                               exposure=setting.exposure, gain=setting.gain)
            start = time.monotonic()
            try:
                frame = self.camera.expose(setting.exposure, setting.gain, abort=self.stop_event)
            except AbortedError:
                break
            except (CameraError, OSError) as err:
                log.error("exposure failed: %s; reopening the camera", err)
                self.status.update(state=f"camera error: {err}")
                try:
                    self.camera.close()
                finally:
                    self.camera = None
                self.clock.sleep(10, self.stop_event)
                continue

            cube = raw_to_cube(frame.data, ic.crop_x, ic.crop_y, ic.crop_width,
                               ic.crop_height, ic.bayer, ic.mirror_x, ic.mirror_y,
                               ic.adu_scale, ic.clip)
            level = self.controller.update(cube, frame.exposure, frame.gain, night)
            self._count += 1
            meta = {"sun_alt": sun_alt, "moon_alt": moon_alt, "moon_az": moon_az,
                    "fratio": fratio, "focus": self.status.focus, "night": night,
                    "sky_level": level, "count": self._count}
            meta.update(self.status.board)
            try:
                self.queue.put_nowait((cube, frame, meta))
            except queue.Full:
                log.warning("products are falling behind; frame %s dropped",
                            self.site.stem(frame.end))
                with self.status.lock:
                    self.status.dropped += 1
            with self.status.lock:
                self.status.frames += 1
                self.status.sky_level = level
            log.info("%s %s exp %.4g s gain %d f/%.1f sky %.0f ADU, sun %.1f",
                     self.site.stem(frame.end), "night" if night else "day",
                     frame.exposure, frame.gain, fratio, level, sun_alt)
            if max_frames is not None and self._count >= max_frames:
                break
            interval = cfg.exposure.interval_night if night else cfg.exposure.interval_day
            elapsed = (time.monotonic() - start) * self.clock.speed
            self.status.update(state="waiting")
            self.clock.sleep(interval - elapsed, self.stop_event)

    # -- products ----------------------------------------------------------------

    def _projector_for(self, when):
        """The display index maps, rebuilt only when the calibration epoch changes."""
        from astropy.time import Time
        from skycam_utils.alcor import alcor_calibration
        cal = alcor_calibration(Time(when))
        epoch = (cal["xcen"], cal["ycen"], cal["rotation"], cal["horizon_radius"])
        if self.projector is None or epoch != self._projector_epoch:
            wcs, cal = render.alcor_wcs(when)
            self.projector = render.Projector(wcs)
            self._projector_epoch = epoch
        return self.projector

    def _products_loop(self):
        while True:
            item = self.queue.get()
            if item is None:
                break
            try:
                self._products(*item)
            except Exception:                                  # noqa: BLE001
                log.exception("writing products failed")

    def _products(self, cube, frame, meta):
        cfg = self.cfg
        out = cfg.output
        when = frame.end
        local = self.site.local(when)
        day = self.site.day_dir(when)
        stem = self.site.stem(when)
        day_dir = Path(out.data_dir) / day
        fits_path = day_dir / f"{stem}.fits.bz2"

        if out.write_fits:
            header = build_header(cube, frame, self.site, cfg.image, meta)
            write_fits_bz2(fits_path, cube, header)

        proj = self._projector_for(when)
        rgb = render.display_rgb(render.repair_badpix(cube, when))
        # the same pointing the science uses: the mount's redis keys, else the
        # telescope file; the zenith when parked, no marker when unknown
        tel = self._pointing(when, parked=(0.0, 90.0))
        if tel is not None:
            tel = (*tel, True)
        info = dict(meta, time=when, local=local, exposure=frame.exposure, gain=frame.gain)
        allsky = render.render_allsky(proj, rgb, info, self.site.location, tel)
        unwrap = render.render_unwrap(proj, rgb)
        noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
        if local.hour < 12:
            noon -= timedelta(days=1)
        keo_file = day_dir / KEOGRAM_FILE
        if day != self.keogram.day and self.keogram.load(keo_file, day):
            log.info("keogram picked up from %s", keo_file)
        self.keogram.add(day, (local - noon).total_seconds(), rgb, proj.zcol)
        if time.monotonic() - self._last_keogram_save >= 300 / self.clock.speed:
            self._save_keogram()

        q = out.jpeg_quality
        allsky_jpg = _jpeg_bytes(allsky, q)
        unwrap_jpg = _jpeg_bytes(unwrap, q)
        if out.write_jpeg:
            outputs.write_atomic(day_dir / f"{stem}.jpg", allsky_jpg)
        if out.write_unwrap:
            outputs.write_atomic(day_dir / f"Unwrap_{stem}.jpg", unwrap_jpg)
        keo = self.keogram.image()
        images = {"allsky": allsky_jpg, "unwrap": unwrap_jpg}
        if keo is not None:
            images["keogram"] = _jpeg_bytes(keo, 90)
        with self.status.lock:
            self.status.images.update(images)
            self.status.last_frame = str(fits_path)
            self.status.last_frame_time = when.timestamp()

        if out.latest_dir and time.monotonic() - self._last_latest >= out.latest_interval / self.clock.speed:
            self._last_latest = time.monotonic()
            latest = Path(out.latest_dir)
            outputs.write_atomic(latest / "ImageLastFTP_AllSKY.jpg", allsky_jpg)
            outputs.write_atomic(latest / "ImageLastFTP_AllSKY_UnWrap.jpg", unwrap_jpg)
            if "keogram" in images:
                outputs.write_atomic(latest / "ImageLastFTP_AllSKY_slice.jpg", images["keogram"])

        row = {"time": when.isoformat(timespec="seconds"), "file": fits_path.name,
               "exposure": frame.exposure, "gain": frame.gain, "fratio": meta["fratio"],
               "sun_alt": meta["sun_alt"], "moon_alt": meta["moon_alt"],
               "sky_level": meta["sky_level"], "ccd_temp": frame.sensor_temp,
               "cooler_power": frame.cooler_power}
        sci = cfg.science
        if (sci.enabled and out.write_fits and self.pool is not None
                and meta["sun_alt"] < sci.sun_alt_max):
            self._science_live = True
            mid = frame.start + timedelta(seconds=frame.exposure / 2)
            target = self._pointing(mid)
            fut = self.pool.submit(science.process_frame, str(fits_path), sci.photometry,
                                   sci.sun_alt_max, target)
            fut.add_done_callback(lambda f, row=row, day_dir=day_dir:
                                  self._science_done(f, row, day_dir))
        else:
            if self._science_live:
                # daylight: blank the night-only values so nothing stale lingers
                self._science_live = False
                self.redis.publish(dict.fromkeys(SCIENCE_KEYS, float("nan")))
                self._fog_recent.clear()
                self.dome_fog = False
                self._lost = math.nan
                self._fog_high_since = None
            self._record(row, day_dir)

    def _pointing(self, when, parked=None):
        """The MMT's (az, alt) at ``when``; ``parked`` when parked, None when unknown."""
        return pointing.mmt_altaz(when, self.site.location, self.redis.client,
                                  self.cfg.output.telescope_file, parked=parked)

    def _science_done(self, fut, row, day_dir):
        try:
            result = fut.result()
        except Exception as err:                               # noqa: BLE001
            log.warning("science on %s failed: %s", row["file"], err)
            result = {}
        row.update({k: v for k, v in result.items() if k in LIVE_FIELDS})
        self._record(row, day_dir)
        if result:
            pub = {k: result[k] for k in ("allsky_mv_zenith", "allsky_mv_tucson",
                                          "allsky_mv_nogales", "allsky_mv_target")}
            if "fog_index" in result:
                pub.update(self._update_fog(result["fog_index"]))
            self.redis.publish(pub)
            with self.status.lock:
                self.status.science.update(pub, allsky_mv_best=result["allsky_mv_best"],
                                           ndetected=result.get("ndetected"),
                                           sb_time=row["time"])
            self._maybe_extinction(day_dir)

    def _update_fog(self, index, now=None):
        """Track the dome-fog flag from one frame's fog index; the redis values."""
        sci = self.cfg.science
        now = self.clock.now().timestamp() if now is None else now
        if math.isfinite(index):
            self._fog_recent.append(index)
        smooth = float(np.median(self._fog_recent)) if self._fog_recent else float("nan")
        rh = (self.status.board or {}).get("rh_ext", math.nan)
        humid = not math.isfinite(rh) or rh >= sci.fog_rh
        full = len(self._fog_recent) == self._fog_recent.maxlen
        # how long the index has stayed up (NaN frames, every zenith star lost,
        # don't interrupt it)
        if full and smooth >= sci.fog_on:
            if self._fog_high_since is None:
                self._fog_high_since = now
        elif not smooth >= sci.fog_off:
            self._fog_high_since = None
        held = self._fog_high_since is not None and now - self._fog_high_since >= sci.fog_persist
        # cloud overhead raises the index too, but it also hides stars and moves
        # on; fog stays, and in time hides stars too
        starry = self._lost <= sci.fog_lost_max
        if not self.dome_fog and full and smooth >= sci.fog_on and humid and (starry or held):
            self.dome_fog = True
            log.warning("dome fog: extinction %.2f mag higher near the zenith than low down "
                        "(outside %.0f %% RH, %.0f %% of stars lost%s)", smooth, rh,
                        100 * self._lost, ", for %.0f min" % ((now - self._fog_high_since) / 60)
                        if held else "")
        elif self.dome_fog and smooth < sci.fog_off:
            self.dome_fog = False
            log.info("dome fog cleared (index %.2f mag)", smooth)
        # with every star near the zenith lost the index is NaN: keep the flag
        return {"allsky_fog_index": smooth, "allsky_dome_fog": int(self.dome_fog)}

    def _maybe_extinction(self, day_dir):
        sci = self.cfg.science
        if not sci.photometry or not sci.extinction_file:
            return
        if self.stop_event.is_set():
            return
        # one map at a time, so a slow build is never overtaken by an older one
        if self._extinction_future is not None and not self._extinction_future.done():
            return
        if time.monotonic() - self._last_extinction < sci.extinction_interval / self.clock.speed:
            return
        self._last_extinction = time.monotonic()
        try:
            self._extinction_future = self.pool.submit(
                science.extinction_map, str(day_dir), sci.extinction_file, sci.extinction_frames,
                sci.extinction_opaque_fraction, sci.extinction_opaque)
        except RuntimeError:                # the pool is shutting down
            return
        self._extinction_future.add_done_callback(
            lambda f: self._extinction_done(f, day_dir))

    def _extinction_done(self, fut, day_dir):
        from astropy.io import fits
        from skycam_utils.alcor import alcor_extinction_at
        try:
            result = fut.result()
        except Exception as err:                               # noqa: BLE001
            log.warning("extinction map failed: %s", err)
            return
        if result is None:                  # not enough frames yet tonight
            return
        target = float("nan")
        pos = self._pointing(self.clock.now())
        if result["opaque"]:
            target = result["median"]
        elif pos is not None:
            try:
                with fits.open(result["path"], memmap=False) as hdul:   # see extinction_map
                    target = alcor_extinction_at(hdul, *pos)
            except Exception as err:                           # noqa: BLE001
                log.warning("extinction lookup at the MMT failed: %s", err)
        pub = {"allsky_extinction_median": result["median"],
               "allsky_extinction_target": target,
               "allsky_extinction_lost_fraction": result["lost_fraction"]}
        self.redis.publish(pub)
        self._lost = result["lost_fraction"]
        row = {"time": result["tmid"] + "Z" if result["tmid"] else self.clock.now().isoformat(),
               "median": result["median"], "target": target, "lost": result["lost_fraction"],
               "fog": int(self.dome_fog)}
        with self.status.lock:
            self.status.science.update(pub, extinction_time=result["tmid"])
            self.status.extinction.append(row)
        outputs.append_csv(day_dir / "omea_extinction.csv", EXTINCTION_FIELDS, row)
        log.debug("extinction map %s: median %.3f, target %.3f, %.0f%% of stars lost",
                  result["tmid"], result["median"], target, 100 * result["lost_fraction"])

    def _save_keogram(self):
        self._last_keogram_save = time.monotonic()
        if self.keogram.day is None:
            return
        try:
            self.keogram.save(Path(self.cfg.output.data_dir) / self.keogram.day / KEOGRAM_FILE)
        except OSError as err:
            log.warning("saving the keogram: %s", err)

    def _record(self, row, day_dir):
        with self._live_lock:
            self.status.history.append(dict(row))
            outputs.append_csv(day_dir / "omea_live.csv", LIVE_FIELDS, row)

    # -- sensor board ------------------------------------------------------------

    def _board_loop(self):
        cfg = self.cfg
        while not self.stop_event.is_set():
            if self.board is not None:
                try:
                    data = self.board.read()
                    ccd = self.status.ccd_temp
                    self.status.update(board={
                        "temp_ext": data.temp_ext, "rh_ext": data.rh_ext,
                        "temp_int": data.temp_int, "rh_int": data.rh_int,
                        "temp_hot": data.temp_hot, "dew_point": data.dew_point,
                        "heater_on": data.heater_on, "auto_heat": data.auto_heat,
                        "heater_power": data.heater_power, "time": data.time})
                    self.status.weather.append(dict(self.status.board, ccd_temp=ccd))
                    if cfg.board.heater_mode == "dew":
                        with self.status.lock:
                            rows = list(self.status.weather)
                        sun_up = self.site.sun_altitude(self.clock.now()) > 0
                        switch = self.dew.update(data, rows, fog=self.dome_fog, sun_up=sun_up)
                        if switch is not None:
                            self._force_heater(switch, self.dew.reason)
                        elif self.dew.dropped(data):
                            self._force_heater("on", f"the board had dropped it ({self.dew.reason})")
                    local = self.site.local(self.clock.now())
                    if cfg.output.weather_csv:
                        outputs.append_weather(cfg.output.weather_csv,
                                               outputs.weather_line(local, data, ccd))
                    self.redis.publish({
                        "allsky_external_temp": round(data.temp_ext, 2),
                        "allsky_internal_temp": round(data.temp_int, 2),
                        "allsky_hotside_temp": round(data.temp_hot, 2),
                        "allsky_ccd_temp": ccd if math.isfinite(ccd) else "nan",
                        "allsky_external_rh": round(data.rh_ext, 1),
                        "allsky_internal_rh": round(data.rh_int, 1),
                        "allsky_dewpoint": round(data.dew_point, 2),
                        "allsky_heater": int(data.heater_on)})
                except Exception as err:                       # noqa: BLE001
                    log.warning("sensor board read failed: %s", err)
            self.clock.sleep(cfg.board.poll_interval, self.stop_event)


def cam_cooling(cam):
    """Whether the camera's cooler is switched on (best effort)."""
    try:
        from . import asi
        return bool(cam.lib.get(cam.id, asi.COOLER_ON))
    except Exception:                                          # noqa: BLE001
        return getattr(cam, "_cooling", False)
