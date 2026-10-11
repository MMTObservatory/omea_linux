"""
Cameras: the ZWO ASI294MC Pro inside the OMEA 8C, and a simulator.

Both take single snapshot exposures and return a :class:`RawFrame` holding the
full-sensor RAW16 mosaic. Everything after that (crop, demosaic, binning) is
:mod:`omea.imaging`, so the simulator exercises the same pipeline.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import asi
from .imaging import cube_to_raw

log = logging.getLogger(__name__)


@dataclass
class RawFrame:
    data: np.ndarray            # uint16 (height, width) RAW16
    start: datetime             # UTC, exposure start
    end: datetime               # UTC, readout complete
    exposure: float             # s
    gain: int
    offset: int
    sensor_temp: float = float("nan")
    cooler_power: float = float("nan")
    camera: str = ""


class CameraError(RuntimeError):
    pass


class AbortedError(CameraError):
    pass


def gain_factor(gain):
    """ADU scale of a ZWO gain setting (units of 0.1 dB), relative to gain 0."""
    return 10.0 ** (gain / 200.0)


class ZwoCamera:
    """A ZWO ASI camera opened through libASICamera2."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lib = asi.ASILibrary(cfg.sdk_library)
        self.id = None
        self.info = None
        self.caps = {}
        self._buffer = None
        self._lock = threading.Lock()

    @property
    def name(self):
        return self.info.Name.decode() if self.info else ""

    def open(self):
        n = self.lib.num_cameras()
        if n == 0:
            raise CameraError("no ZWO camera found (is the USB device attached to "
                              "this machine? see docs/hardware.md)")
        for i in range(n):
            info = self.lib.camera_info(i)
            if self.cfg.name.lower() in info.Name.decode().lower():
                break
        else:
            raise CameraError(f"no camera matching {self.cfg.name!r}")
        self.info = info
        self.id = info.CameraID
        self.lib.open(self.id)
        self.caps = self.lib.controls(self.id)
        width, height = int(info.MaxWidth), int(info.MaxHeight)
        self.lib.set_roi(self.id, width, height, 1, asi.IMG_RAW16)
        self._buffer = np.empty((height, width), dtype=np.uint16)
        self.lib.set(self.id, asi.OFFSET, self.cfg.offset)
        self.lib.set(self.id, asi.BANDWIDTHOVERLOAD, self.cfg.usb_bandwidth)
        if asi.HIGH_SPEED_MODE in self.caps:
            self.lib.set(self.id, asi.HIGH_SPEED_MODE, 0)
        self.lib.set(self.id, asi.FLIP, 0)
        log.info("opened %s (SDK %s), %dx%d %s, %d-bit", self.name,
                 self.lib.sdk_version(), width, height,
                 asi.BAYER_NAMES.get(info.BayerPattern, "?"), info.BitDepth)

    def close(self):
        if self.id is not None:
            try:
                self.set_cooler(False)
            except Exception:                                # noqa: BLE001
                pass
            try:
                self.lib.close(self.id)
            except asi.ASIError as err:
                log.warning("closing camera: %s", err)
            self.id = None

    def describe(self):
        """Camera properties and controls, for ``omea camera-info``."""
        info = self.info
        lines = [f"{self.name}  id={self.id}  serial={self.lib.serial_number(self.id)}",
                 f"  sensor {info.MaxWidth}x{info.MaxHeight}, {info.PixelSize} um, "
                 f"{info.BitDepth} bit, bayer {asi.BAYER_NAMES.get(info.BayerPattern)}, "
                 f"e/ADU {info.ElecPerADU:.3f}, USB3 host {bool(info.IsUSB3Host)}"]
        for ctype, c in sorted(self.caps.items()):
            try:
                value = self.lib.get(self.id, ctype)
            except asi.ASIError:
                value = "?"
            lines.append(f"  {c.Name.decode():24s} = {value!s:>8}  "
                         f"[{c.MinValue}, {c.MaxValue}] {c.Description.decode()}")
        return "\n".join(lines)

    # -- cooling -------------------------------------------------------------

    @property
    def has_cooler(self):
        return bool(self.info and self.info.IsCoolerCam)

    def set_cooler(self, on, target=None):
        if not self.has_cooler:
            return
        if target is not None:
            self.lib.set(self.id, asi.TARGET_TEMP, int(round(target)))
        self.lib.set(self.id, asi.COOLER_ON, int(bool(on)))

    def temperature(self):
        return self.lib.get(self.id, asi.TEMPERATURE) / 10.0

    def cooler_power(self):
        if not self.has_cooler:
            return float("nan")
        return float(self.lib.get(self.id, asi.COOLER_POWER_PERC))

    # -- exposures -----------------------------------------------------------

    def expose(self, exposure, gain, abort=None):
        """Take one exposure (s, ZWO gain units) and return a :class:`RawFrame`."""
        with self._lock:
            lib, cid = self.lib, self.id
            lib.set(cid, asi.EXPOSURE, max(1, int(round(exposure * 1e6))))
            lib.set(cid, asi.GAIN, int(gain))
            start = datetime.now(timezone.utc)
            lib.start_exposure(cid)
            deadline = time.monotonic() + exposure + self.cfg.readout_timeout
            while True:
                status = lib.exposure_status(cid)
                if status == asi.EXP_SUCCESS:
                    break
                if status == asi.EXP_FAILED:
                    raise CameraError("exposure failed")
                if abort is not None and abort.is_set():
                    lib.stop_exposure(cid)
                    raise AbortedError("exposure aborted")
                if time.monotonic() > deadline:
                    lib.stop_exposure(cid)
                    raise CameraError(f"no image {self.cfg.readout_timeout:.0f} s "
                                      "after the exposure should have ended")
                time.sleep(0.01 if exposure < 1 else 0.1)
            lib.read(cid, self._buffer)
            end = datetime.now(timezone.utc)
            return RawFrame(
                data=self._buffer.copy(), start=start, end=end,
                exposure=lib.get(cid, asi.EXPOSURE) / 1e6, gain=int(gain),
                offset=self.cfg.offset, sensor_temp=self.temperature(),
                cooler_power=self.cooler_power(), camera=self.name)


class SimCamera:
    """
    Replays Skywatch frames from an archive as if they came off the sensor.

    For each exposure it takes the archive frame closest in local time of day
    (falling back to cycling through the files), rescales its signal above bias
    by the requested exposure x gain relative to the archived frame's, adds read
    noise, and converts it back to a RAW16 mosaic with
    :func:`omea.imaging.cube_to_raw`. Without an archive it draws a flat noisy
    sky. Time comes from ``clock``, so a :class:`omea.clock.SimClock` replays a
    whole day in minutes with the matching frames.
    """

    name = "Simulated ASI294MC Pro"
    has_cooler = True

    def __init__(self, cfg, image_cfg, tz=None, clock=None, rng=None):
        from .clock import Clock
        self.cfg = cfg
        self.image_cfg = image_cfg
        self.tz = tz
        self.clock = clock or Clock()
        self.rng = rng or np.random.default_rng()
        self.files = []
        if cfg.sim_archive:
            root = Path(cfg.sim_archive)
            self.files = sorted(root.glob("*.fits.bz2")) or sorted(root.glob("*/*.fits.bz2"))
            if not self.files:
                raise CameraError(f"no *.fits.bz2 under {root}")
        # time-of-day lookup ("HH_MM_SS" from YYYY_MM_DD__HH_MM_SS); a day
        # directory runs noon to noon, so sort the stamps rather than the files
        stamps = [f.name[12:20] for f in self.files]
        self._order = sorted(range(len(stamps)), key=stamps.__getitem__)
        self._stamps = [stamps[i] for i in self._order]
        self._pos = -1
        self._target = 20.0
        self._cooling = False
        self._temp = 20.0

    def open(self):
        log.info("simulated camera, %d archive frames", len(self.files))

    def close(self):
        pass

    def describe(self):
        return f"{self.name} replaying {len(self.files)} frames from {self.cfg.sim_archive or '-'}"

    def set_cooler(self, on, target=None):
        self._cooling = bool(on)
        if target is not None:
            self._target = target

    def temperature(self):
        goal = self._target if self._cooling else 20.0
        self._temp += 0.5 * (goal - self._temp)
        return round(self._temp, 1)

    def cooler_power(self):
        return 40.0 if self._cooling else 0.0

    def _pick(self, now):
        """The archive frame nearest in local time of day to ``now``."""
        if not self.files:
            return None
        n = len(self.files)
        if self.tz is not None:
            want = now.astimezone(self.tz).strftime("%H_%M_%S")
            pos = int(np.searchsorted(self._stamps, want)) % n
        else:
            pos = (self._pos + 1) % n
        self._pos = pos
        return self.files[self._order[pos]]

    def expose(self, exposure, gain, abort=None):
        from astropy.io import fits

        start = self.clock.now()
        if self.clock.sleep(exposure, abort):
            raise AbortedError("exposure aborted")
        path = self._pick(start)
        if path is None:
            cube = np.full((3, 1411, 1422), 1930.0) + self.rng.normal(0, 20, (3, 1411, 1422))
            ref = 1.0
        else:
            with fits.open(path) as hdul:
                cube = hdul[0].data.astype(np.float32)
                hdr = hdul[0].header
            ref = float(hdr["EXPOSURE"]) * gain_factor(hdr.get("GAIN_ELE", 0))
            # The archive's iris (F16 day, F2.8 night) is unknown to the caller;
            # the controller sees an honest exposure response either way.
        bias = np.median(cube[:, :10, :10], axis=(1, 2))[:, None, None]
        scale = exposure * gain_factor(gain) / ref
        cube = (cube - bias) * scale + bias
        cube += self.rng.normal(0, 8.0, cube.shape).astype(np.float32)
        ic = self.image_cfg
        raw = cube_to_raw(np.clip(cube, 0, ic.clip), crop_x=ic.crop_x, crop_y=ic.crop_y,
                          bayer=ic.bayer, mirror_x=ic.mirror_x, mirror_y=ic.mirror_y,
                          adu_scale=ic.adu_scale)
        end = self.clock.now()
        return RawFrame(data=raw, start=start, end=end, exposure=exposure,
                        gain=int(gain), offset=30, sensor_temp=self.temperature(),
                        cooler_power=self.cooler_power(), camera=self.name)


def open_camera(cfg, image_cfg, tz=None, clock=None):
    if cfg.driver == "zwo":
        cam = ZwoCamera(cfg)
    elif cfg.driver == "sim":
        cam = SimCamera(cfg, image_cfg, tz=tz, clock=clock)
    else:
        raise ValueError(f"unknown camera driver {cfg.driver!r}")
    cam.open()
    return cam
