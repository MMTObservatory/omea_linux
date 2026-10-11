"""
FITS files in Skywatch's layout.

The header reproduces Skywatch 4.3.15's keywords, order and quirks so existing
readers keep working -- in particular ``DATE`` is the true UT (readout
complete) while ``DATE-OBS`` holds the LOCAL exposure start despite its "UT"
comment; skycam_utils relies on exactly that. New information goes in extra
keywords after Skywatch's (``DATE-BEG``/``DATE-END`` in UT, sensor cooling, the
board's sensors, Sun and Moon), never by changing the meaning of an old one.
"""

import bz2
import io
import os
from datetime import timedelta
from pathlib import Path

import numpy as np
from astropy.io import fits

from . import __version__
from .ephem import dms


def _local_iso(dt):
    return dt.replace(tzinfo=None).isoformat(timespec="microseconds")


def build_header(cube, frame, site, image_cfg, extra):
    """
    ``frame`` is the :class:`omea.camera.RawFrame`; ``extra`` a dict with
    ``fratio``, ``focus``, board readings (``temp_ext``, ``rh_ext``,
    ``dew_point``...), ``sun_alt``, ``moon_alt`` and ``moon_az`` where known.
    """
    ic = image_cfg
    h = fits.Header()

    def put(key, value, comment):
        h[key] = (value, comment)

    def num(key, default=0.0):
        """A header-safe number: FITS headers cannot hold NaN."""
        v = extra.get(key)
        return default if v is None or not np.isfinite(v) else float(v)

    utc_end = frame.end
    local_start = site.local(frame.start)
    put("DATE", utc_end.replace(tzinfo=None).isoformat(timespec="microseconds"),
        "Date of FITS file creation")
    put("SWCREATE", f"omea_linux {__version__}", "Software Name and version")
    put("ORIGIN", site.cfg.name, "Origin place of FITS image")
    put("OBSERVER", "omea", "Observer name")
    put("SITELAT", dms(site.cfg.latitude), "Latitude observatory")
    put("SITELONG", dms(site.cfg.longitude), "Longitude observatory")
    put("DATE-OBS", _local_iso(local_start), "LOCAL exposure start (as Skywatch)")
    put("UT", local_start.strftime("%H:%M:%S.") + f"{local_start.microsecond // 10000:02d}",
        "LOCAL exposure start (as Skywatch)")
    put("EXPOSURE", round(frame.exposure, 6), "Exposure time seconds")
    put("XPIXELSZ", 4.63, "X pixel size microns")
    put("CDELTM1", 0.00463, "Size of a pixel (x) in mm")
    put("XPIXSZ", 4.6, "X Binned pixel size as microns")
    put("YPIXELSZ", 4.63, "Y pixel size microns")
    put("CDELTM2", 0.00463, "Size of a pixel (y) in mm")
    put("YPIXSZ", 4.6, "Y Binned pixel size as microns")
    put("INSTRUME", frame.camera, "Camera which created data")
    put("CVF", 5.0, "Conversion factor (e-/adu)")
    put("READOUTT", round((frame.end - frame.start).total_seconds() - frame.exposure, 2),
        "Image readout time (sec)")
    put("X1", ic.crop_x + 1, "X1 image windowing")
    put("Y1", ic.crop_y + 1, "Y1 image windowing")
    put("X2", ic.crop_x + ic.crop_width, "X2 image windowing")
    put("Y2", ic.crop_y + ic.crop_height, "Y2 image windowing")
    put("COMPRESS", 0, "Number of compression")
    put("TELESCOP", "All SkyCamera", "Telescope")
    put("FOCAL", 1.8, "Focal length in mm")
    put("DIAMETER", 0.6, "Diameter in mm")
    put("CDELT1", 0.147377152272, "ScaleX in deg/pix")
    put("CDELT2", 0.147377152272, "ScaleY in deg/pix")
    put("DATAMAX", int(cube.max()), "Maximum data value")
    put("DATAMIN", int(cube.min()), "Minimum data value")
    put("CCD-TEMP", round(frame.sensor_temp, 1) if np.isfinite(frame.sensor_temp) else 0.0,
        "CCD temperature as Celsius degrees")
    put("FOCUSPOS", 0.0, "Focuser position as mm")
    put("FOCUSTMP", 0.0, "Focuser temperature as Celsius degrees")
    put("BITCAMPX", 15, "Native camera dynamic in bits per pixel")
    put("STACKNB", 1, "Amount of stacked images")
    put("STARCNT", 0, "Amount of extracted stars")
    put("GAIN_ELE", int(frame.gain), "Camera electronic gain (arbitrary units)")
    put("OFFSET_E", int(frame.offset), "Camera electronic offset (arbitrary units)")
    put("TEMPEXT", round(num("temp_ext"), 1), "External temperature, unit is degrees C")
    put("PRESSURE", 0.0, "Atm. Pressure, unit is Hpa")
    put("DEWPOINT", round(num("dew_point"), 1), "Dewpoint temperature, unit is degrees C")
    put("HUMIDITY", round(num("rh_ext"), 1), "Relative humidity, unit is percent")
    put("WINSPEED", 0.0, "Wind speed, unit is km/h")
    put("WINDIR", 0.0, "Wind direction, North 0deg, East 90d")
    put("IMGTYPE", "STD_IMAGE", "Image type, like flat, bias or dark")
    put("BAYERIND", 4, "Bayer color Index")
    # -- additions (not written by Skywatch) --------------------------------
    put("DATE-BEG", frame.start.replace(tzinfo=None).isoformat(timespec="microseconds"),
        "UT exposure start")
    put("DATE-END", (frame.start.replace(tzinfo=None)
                     + timedelta(seconds=frame.exposure)).isoformat(timespec="microseconds"),
        "UT exposure end")
    put("FRATIO", float(extra.get("fratio") or 0.0), "Lens iris f-ratio")
    put("FOCUSSTP", int(extra.get("focus") or 0), "Lens focus, encoder steps from home")
    put("COOLPWR", round(frame.cooler_power, 1) if np.isfinite(frame.cooler_power) else -1.0,
        "Sensor cooler power (%)")
    put("ADUSCALE", float(ic.adu_scale), "Output ADU per 2x2 mean of RAW16 ADU")
    put("TEMPINT", round(num("temp_int", -99.0), 1), "Camera interior temperature, degC")
    put("RHINT", round(num("rh_int", -1.0), 1), "Camera interior relative humidity, %")
    put("HEATER", bool(extra.get("heater_on", False)), "Dome heater on")
    put("SUNALT", round(num("sun_alt", -99.0), 2), "Sun altitude, deg")
    put("MOONALT", round(num("moon_alt", -99.0), 2), "Moon altitude, deg")
    put("MOONAZ", round(num("moon_az", -1.0), 2), "Moon azimuth, deg")
    h.add_comment(f"Focus pos (adu) : {int(extra.get('focus') or 0)}")
    h.add_comment(f"Iris F{float(extra.get('fratio') or 0.0):.1f}")
    h.add_comment(f"Gain={int(frame.gain)}")
    h.add_history("none")
    return h


def fits_bytes(cube, header):
    """Serialise to FITS (int16, the camera's native orientation)."""
    hdu = fits.PrimaryHDU(data=np.asarray(cube, dtype=np.int16), header=header)
    buf = io.BytesIO()
    hdu.writeto(buf)
    return buf.getvalue()


def write_atomic(path, data):
    """Write bytes to ``path`` via a temporary file, so readers never see half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_fits_bz2(path, cube, header, level=9):
    write_atomic(path, bz2.compress(fits_bytes(cube, header), compresslevel=level))
