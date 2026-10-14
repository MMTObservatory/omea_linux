"""
Where the MMT is pointing, for the ``*_target`` redis values.

Read from the mount's redis keys exactly as skycam-utils'
``scripts/mmt_extinction_redis.py`` does: ``mount_mini_ra`` (hours) and
``mount_mini_declination`` (deg), with ``mount_mini_cat_ra2000 < 0`` meaning
the mount is not tracking a catalog position (parked), in which case there is
no target. Without redis, the telescope-position file ``mmt_position.py``
writes is used, but only while it is fresh.
"""

import json
import logging

import astropy.units as u
from astropy.coordinates import AltAz, Angle, SkyCoord
from astropy.time import Time

from .outputs import read_telescope

log = logging.getLogger(__name__)


def _value(client, key):
    raw = client.get(key)
    if raw is None:
        raise KeyError(key)
    return json.loads(raw)["value"]


def mmt_altaz(when, location, redis_client=None, telescope_file=None, parked=None):
    """
    ``(az, alt)`` in degrees at ``when``, or None when there is no target.
    A parked mount returns ``parked`` (the image overlay passes the zenith,
    where the parked MMT points, as mmt_position.py did).
    """
    if redis_client is not None:
        try:
            if float(_value(redis_client, "mount_mini_cat_ra2000")) < 0:
                return parked
            coord = SkyCoord(ra=Angle(_value(redis_client, "mount_mini_ra"), u.hourangle),
                             dec=Angle(_value(redis_client, "mount_mini_declination"), u.deg))
        except Exception as err:                              # noqa: BLE001
            log.debug("no mount position from redis: %s", err)
            return None
    elif telescope_file:
        pos = read_telescope(telescope_file)
        if pos is None or not pos[2]:
            return None
        coord = SkyCoord(ra=pos[0] * u.deg, dec=pos[1] * u.deg)
    else:
        return None
    aa = coord.transform_to(AltAz(obstime=Time(when), location=location))
    return float(aa.az.deg), float(aa.alt.deg)
