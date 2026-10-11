"""Sun/Moon altitude and Skywatch's local-time naming conventions."""

from datetime import timedelta
from zoneinfo import ZoneInfo

import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, get_body, get_sun
from astropy.time import Time


class Site:
    def __init__(self, cfg):
        self.cfg = cfg
        self.location = EarthLocation.from_geodetic(cfg.longitude * u.deg,
                                                    cfg.latitude * u.deg,
                                                    cfg.elevation * u.m)
        self.tz = ZoneInfo(cfg.timezone)

    def _altaz(self, body, when):
        t = Time(when)
        frame = AltAz(obstime=t, location=self.location)
        if body == "sun":
            coord = get_sun(t)
        else:
            coord = get_body(body, t, self.location)
        return coord.transform_to(frame)

    def sun_altitude(self, when):
        return float(self._altaz("sun", when).alt.deg)

    def moon_altaz(self, when):
        aa = self._altaz("moon", when)
        return float(aa.alt.deg), float(aa.az.deg)

    def local(self, when):
        return when.astimezone(self.tz)

    def day_dir(self, when):
        """
        The noon-to-noon directory name for ``when``: frames from local noon on
        2026-10-10 until 11:59:59 the next morning go in ``2026-10-10``.
        """
        local = self.local(when)
        if local.hour < 12:
            local -= timedelta(days=1)
        return local.strftime("%Y-%m-%d")

    def stem(self, when):
        """Skywatch's file stem: local time, ``YYYY_MM_DD__HH_MM_SS``."""
        return self.local(when).strftime("%Y_%m_%d__%H_%M_%S")


def dms(value, sign=True):
    """Skywatch's SITELAT/SITELONG format, e.g. ``+31:41:12:00``."""
    s = "-" if value < 0 else ("+" if sign else "")
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    sec = round(((v - d) * 60 - m) * 60)
    if sec == 60:
        m, sec = m + 1, 0
    if m == 60:
        d, m = d + 1, 0
    return f"{s}{d}:{m:02d}:{sec:02d}:00"

