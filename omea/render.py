"""
Display products: the annotated all-sky JPEG, the unwrapped panorama and the
running keogram strip.

Geometry comes from skycam_utils' calibrated WCS rather than Skywatch's
hand-set centre/radius/north circle: each display pixel is assigned an (az,
alt) on an ideal north-up, east-left equidistant fisheye (or an az x alt
panorama), and the WCS gives the raw pixel that sees it. Those index maps are
computed once per calibration epoch, so a frame is rendered by indexing --
which also removes the lens distortion and axis tilt from the picture.
"""

import logging
import warnings
from functools import lru_cache
from importlib.resources import files

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import astropy.units as u
from astropy.coordinates import AltAz, SkyCoord, get_body
from astropy.time import Time

log = logging.getLogger(__name__)

SIZE = (1422, 1411)                 # Skywatch's JPEG size (w, h)
BRIGHT_STARS = 1.6                  # label stars at least this bright (V)
PLANETS = ("mercury", "venus", "mars", "jupiter", "saturn")


def alcor_wcs(when):
    """skycam_utils' raw-frame alt/az WCS for the calibration epoch nearest ``when``."""
    from skycam_utils.alcor import alcor_calibration
    from skycam_utils.alcor.wcs import build_alcor_wcs
    cal = alcor_calibration(Time(when))
    keys = ("xcen", "ycen", "rotation", "radial_coeffs", "horizon_radius",
            "tangential_coeffs", "axis_tilt")
    return build_alcor_wcs(**{k: cal[k] for k in keys}), cal


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                                  # Pillow < 10.1
        return ImageFont.load_default()


@lru_cache(maxsize=1)
def _named_stars():
    """(name, ra, dec, V) of the bright named stars shipped with skycam_utils."""
    import csv
    path = files("skycam_utils") / "data" / "star_names.csv"
    out = []
    with path.open() as f:
        for row in csv.DictReader(f):
            try:
                mag = float(row["Magnitude"])
            except ValueError:
                continue
            if mag <= BRIGHT_STARS:
                out.append((row["Star Name"], float(row["RA"]), float(row["Dec"]), mag))
    return out


class Projector:
    """Index maps from display images into the raw (1411, 1422) frame."""

    def __init__(self, wcs, shape=(1411, 1422), size=SIZE,
                 alt_min=-3.0, unwrap_alt=(-3.0, 42.0), unwrap_ppd=12.0):
        self.wcs = wcs
        self.shape = shape
        self.size = size
        w, h = size
        # fisheye: zenith at the centre, equidistant in zenith distance with
        # the whole circle down to alt_min inside the frame (as Skywatch drew
        # it), north up and east LEFT (a sky view from below)
        self.scale = (min(w, h) / 2.0 - 24) / (90.0 - alt_min)   # px per deg
        self.cx, self.cy = (w - 1) / 2.0, (h - 1) / 2.0
        yy, xx = np.mgrid[0:h, 0:w].astype(float)
        dx, dy = xx - self.cx, self.cy - yy               # dy up = north
        zd = np.hypot(dx, dy) / self.scale
        az = np.degrees(np.arctan2(-dx, dy)) % 360.0      # east to the left
        alt = 90.0 - zd
        self.sky_index, self.sky_valid = self._index(az, alt, alt >= alt_min)

        # panorama: azimuth N-E-S-W-N left to right, altitude up
        a0, a1 = unwrap_alt
        nw, nh = int(round(360 * unwrap_ppd)), int(round((a1 - a0) * unwrap_ppd))
        az = (np.arange(nw) + 0.5) / unwrap_ppd
        alt = a1 - (np.arange(nh) + 0.5) / unwrap_ppd
        AZ, ALT = np.meshgrid(az, alt)
        self.unwrap_alt = unwrap_alt
        self.unwrap_ppd = unwrap_ppd
        self.pano_index, self.pano_valid = self._index(AZ, ALT, ALT >= alt_min)

        # zenith column of the raw frame, for the keogram (as alcor_keogram)
        zx, _ = wcs.world_to_pixel_values(0.0, 90.0)
        self.zcol = int(round(float(zx)))

    def _index(self, az, alt, keep):
        # only invert the WCS where it is defined: far below the horizon the
        # SIP inversion diverges
        x = np.full(az.shape, np.nan)
        y = np.full(az.shape, np.nan)
        with warnings.catch_warnings():
            # the iterative SIP inversion may not converge at the very edge of
            # the field; those points fail the round trip below and are dropped
            warnings.simplefilter("ignore")
            x[keep], y[keep] = self.wcs.world_to_pixel_values(az[keep], alt[keep])
            ok = keep & np.isfinite(x) & np.isfinite(y)
            az2, alt2 = self.wcs.pixel_to_world_values(np.where(ok, x, 0), np.where(ok, y, 0))
        dalt = np.abs(alt2 - alt)
        daz = np.abs((az2 - az + 180) % 360 - 180) * np.cos(np.radians(alt))
        ok &= (dalt < 0.05) & (daz < 0.05)
        x, y = np.rint(x), np.rint(y)
        ny, nx = self.shape
        ok &= (x >= 0) & (x < nx) & (y >= 0) & (y < ny)
        flat = np.where(ok, y * nx + x, 0).astype(np.int64)
        return flat, ok

    def to_display(self, az, alt):
        """Display (x, y) of a sky direction on the fisheye image."""
        zd = 90.0 - np.asarray(alt, float)
        r = zd * self.scale
        a = np.radians(az)
        return self.cx - r * np.sin(a), self.cy - r * np.cos(a)

    def sample(self, rgb, which="sky"):
        """``rgb`` is (ny, nx, 3) uint8 in raw-frame orientation."""
        index, valid = ((self.sky_index, self.sky_valid) if which == "sky"
                        else (self.pano_index, self.pano_valid))
        flat = rgb.reshape(-1, 3)
        out = flat[index]
        out[~valid] = 0
        return out


def display_rgb(cube):
    """skycam_utils' display stretch (bias-subtracted, per-channel balance), as uint8."""
    from skycam_utils.alcor.display import _alcor_display_rgb
    rgb = _alcor_display_rgb(cube)
    return (np.clip(rgb, 0, 1) * 255).astype(np.uint8)


def _box(draw, xy, text, font, anchor="la", fill="white"):
    x0, y0, x1, y1 = draw.textbbox(xy, text, font=font, anchor=anchor)
    draw.rectangle((x0 - 4, y0 - 3, x1 + 4, y1 + 3), fill="black", outline="white")
    draw.text(xy, text, font=font, anchor=anchor, fill=fill)


def _altaz(coord, when, location):
    aa = coord.transform_to(AltAz(obstime=Time(when), location=location))
    return aa.az.deg, aa.alt.deg


def render_allsky(proj, rgb, info, location, telescope=None):
    """
    The annotated north-up all-sky JPEG image (a PIL Image).

    ``info`` carries the frame metadata (``local``, ``exposure``, ``gain``,
    ``fratio``, ``sun_alt``, ``count`` and the board's ``temp_ext``,
    ``rh_ext``, ``heater_on``); ``telescope`` an optional ``(az, alt, fresh)``.
    """
    w, h = proj.size
    img = Image.fromarray(proj.sample(rgb, "sky").reshape(h, w, 3))
    draw = ImageDraw.Draw(img)
    small, big = _font(18), _font(24)
    when = info["time"]

    # altitude rings and the N-S / E-W lines
    for alt in range(0, 90, 10):
        r = (90 - alt) * proj.scale
        draw.ellipse((proj.cx - r, proj.cy - r, proj.cx + r, proj.cy + r),
                     outline=(160, 160, 160))
        if 10 <= alt <= 80:
            x, y = proj.to_display(135.0, alt + 1.5)
            draw.text((x, y), f"{alt}°", font=small, fill=(200, 200, 200), anchor="mm")
    r = 90 * proj.scale
    draw.line((proj.cx, proj.cy - r, proj.cx, proj.cy + r), fill=(160, 160, 160))
    draw.line((proj.cx - r, proj.cy, proj.cx + r, proj.cy), fill=(160, 160, 160))
    for label, az in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        x, y = proj.to_display(az, -1.0)
        draw.ellipse((x - 17, y - 17, x + 17, y + 17), fill="black", outline="white")
        draw.text((x, y), label, font=big, fill="white", anchor="mm")

    # bright stars and planets
    try:
        stars = _named_stars()
        coords = SkyCoord([s[1] for s in stars] * u.deg, [s[2] for s in stars] * u.deg)
        az, alt = _altaz(coords, when, location)
        for (name, *_), a, e in zip(stars, az, alt):
            if e > 5:
                x, y = proj.to_display(a, e)
                draw.ellipse((x - 3, y - 3, x + 3, y + 3), outline=(255, 120, 120))
                draw.text((x + 6, y), name, font=small, fill=(255, 140, 140), anchor="lm")
        for body, color in [(p, (255, 220, 90)) for p in PLANETS] + [("moon", (255, 255, 255))]:
            a, e = _altaz(get_body(body, Time(when), location), when, location)
            if e > 0:
                x, y = proj.to_display(a, e)
                draw.ellipse((x - 5, y - 5, x + 5, y + 5), outline=color)
                draw.text((x + 8, y), body.capitalize(), font=small, fill=color, anchor="lm")
    except Exception as err:                                  # noqa: BLE001
        log.debug("star overlay skipped: %s", err)

    if telescope is not None:
        a, e, fresh = telescope
        if e > -2:
            x, y = proj.to_display(a, e)
            color = (80, 255, 80) if fresh else (255, 60, 60)
            draw.line((x - 18, y, x + 18, y), fill=color, width=3)
            draw.line((x, y - 18, x, y + 18), fill=color, width=3)
            draw.text((x + 20, y - 20), "MMT", font=small, fill=color)

    local = info["local"]
    _box(draw, (6, h - 8), f"LT {local:%Y-%m-%d %H:%M:%S}", big, anchor="lb")
    _box(draw, (6, h - 48), f"{info.get('count', 0)}", small, anchor="lb")
    exp = info["exposure"]
    exp_s = f"{exp:.0f}s" if exp >= 1 else f"{exp * 1000:.1f}ms"
    _box(draw, (w - 6, h - 8),
         f"f/{info.get('fratio') or 0:.1f}  Gain={info['gain']}  {exp_s}", big, anchor="rb")
    if info.get("temp_ext") is not None:
        heater = "ON" if info.get("heater_on") else "off"
        _box(draw, (w - 6, h - 48),
             f"{info['temp_ext']:.1f}°C {info['rh_ext']:.0f}%RH [{heater}]", small, anchor="rb")
    _box(draw, (6, 8), f"Sun {info.get('sun_alt', 0):+.1f}°", small)
    return img


def render_unwrap(proj, rgb):
    """The az x alt panorama with cardinal markers along the bottom."""
    nh = int(round((proj.unwrap_alt[1] - proj.unwrap_alt[0]) * proj.unwrap_ppd))
    nw = int(round(360 * proj.unwrap_ppd))
    img = Image.fromarray(proj.sample(rgb, "pano").reshape(nh, nw, 3))
    draw = ImageDraw.Draw(img)
    font = _font(20)
    y0 = (proj.unwrap_alt[1] - 0.0) * proj.unwrap_ppd        # the horizon row
    draw.line((0, y0, nw, y0), fill=(220, 30, 30), width=2)
    for label, az in (("N", 0), ("E", 90), ("S", 180), ("W", 270), ("N", 360)):
        x = min(max(az * proj.unwrap_ppd, 16), nw - 17)
        draw.ellipse((x - 15, nh - 34, x + 15, nh - 4), fill="black", outline=(220, 30, 30))
        draw.text((x, nh - 19), label, font=font, fill="white", anchor="mm")
    return img


class Keogram:
    """
    The day so far as a strip: the zenith column of every frame, placed on a
    fixed noon-to-noon time axis (``width`` columns for 24 h), each column
    repeated until the next frame -- Skywatch's "24h overview".
    """

    def __init__(self, height=1411, width=2880):
        self.height = height
        self.width = width
        self.day = None
        self.times = []          # seconds since local noon
        self.columns = []

    def add(self, day, seconds_since_noon, rgb, zcol):
        if day != self.day:
            self.day, self.times, self.columns = day, [], []
        self.times.append(float(seconds_since_noon))
        self.columns.append(rgb[::-1, zcol, :].copy())     # north at the top

    def image(self):
        if not self.columns:
            return None
        strip = np.zeros((self.columns[0].shape[0], self.width, 3), np.uint8)
        x = (np.arange(self.width) + 0.5) * 86400.0 / self.width
        idx = np.searchsorted(self.times, x, side="right") - 1
        # fill from the first frame to the newest one, not into the future
        live = (idx >= 0) & (x <= self.times[-1] + 86400.0 / self.width)
        cols = np.stack(self.columns, axis=1)
        strip[:, live] = cols[:, idx[live]]
        img = Image.fromarray(strip)
        if img.height != self.height:
            img = img.resize((self.width, self.height), Image.NEAREST)
        return img
