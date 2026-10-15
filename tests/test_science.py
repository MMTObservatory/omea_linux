import json
import math
from datetime import datetime, timezone

import numpy as np
import pytest

from omea import pointing
from omea.config import RedisConfig, SiteConfig
from omea.ephem import Site
from omea.outputs import Redis

skycam_utils = pytest.importorskip("skycam_utils")


def test_target_cone_matches_skycam_utils_cones():
    """The MMT-pointing cone must be the same statistic as allsky_mv_zenith & co."""
    from astropy.time import Time
    from skycam_utils.alcor.config import ALCOR_SB_APERTURE_RADIUS, ALCOR_SB_TARGETS
    from omea.render import alcor_wcs
    from omea.science import _geometry

    when = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
    wcs, _ = alcor_wcs(when)
    geom = _geometry(wcs, (1411, 1422), Time(when))
    for name, (az, alt) in ALCOR_SB_TARGETS.items():
        mine = geom.cone(az, alt, ALCOR_SB_APERTURE_RADIUS)
        theirs = geom.cones[name]
        # identical up to pixels sitting exactly on the cone edge (float rounding)
        assert len(np.setxor1d(mine, theirs)) <= 0.001 * len(theirs), name


class FakeRedis:
    def __init__(self, values):
        self.store = {k: json.dumps({"value": v}) for k, v in values.items()}
        self.published = []

    def get(self, key):
        return self.store.get(key)

    def pipeline(self):
        return self

    def set(self, key, value):
        self.store[key] = value

    def publish(self, key, value):
        self.published.append((key, value))

    def execute(self):
        pass


def test_pointing_from_mount_keys():
    site = Site(SiteConfig())
    when = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
    lst = Site(SiteConfig()).location
    # point at the meridian at dec = latitude -> the zenith
    from astropy.time import Time
    ha0 = Time(when).sidereal_time("apparent", longitude=lst.lon).hour
    client = FakeRedis({"mount_mini_cat_ra2000": 1.0, "mount_mini_ra": ha0,
                        "mount_mini_declination": site.cfg.latitude})
    az, alt = pointing.mmt_altaz(when, site.location, client)
    assert alt > 89.0
    client.store["mount_mini_cat_ra2000"] = json.dumps({"value": -1})
    assert pointing.mmt_altaz(when, site.location, client) is None    # parked
    assert pointing.mmt_altaz(when, site.location, client, parked=(0.0, 90.0)) == (0.0, 90.0)
    assert pointing.mmt_altaz(when, site.location, FakeRedis({})) is None


def test_publish_sends_nan_as_string():
    r = Redis(RedisConfig(enabled=False))
    r.client = FakeRedis({})
    r.publish({"allsky_mv_target": float("nan"), "allsky_mv_zenith": 21.5})
    assert ("allsky_mv_target", "nan") in r.client.published
    assert ("allsky_mv_zenith", 21.5) in r.client.published
    assert math.isnan(float(r.client.store["allsky_mv_target"]))


def test_censored_median():
    from omea.science import censored_median
    assert censored_median([0.1, 0.2, 0.3], 0) == pytest.approx(0.2)
    assert censored_median([0.1, 0.2, 0.3, 0.4], 0) == pytest.approx(0.25)
    # one lost star ranks above everything: median of [.1 .2 .3 lost] = (.2+.3)/2
    assert censored_median([0.1, 0.2, 0.3], 1) == pytest.approx(0.25)
    # thick cloud, as on 2026-10-09 21:00: 14 measured, 219 lost -> floor
    assert censored_median(np.linspace(-0.6, 1.7, 14), 219) == pytest.approx(1.7)
    assert math.isnan(censored_median([], 50))


def test_fog_reports_opaque_extinction(tmp_path):
    # thick fog: 40 stars, all lost but one spurious "detection" in every frame
    from omea.science import extinction_map
    for k in range(10):
        rows = ["name,altitude,azimuth,variable,flux_g,mag_g,ext_g,sat_g"]
        rows += [f"star{i},50,{9 * i},False,0.0,,,False" for i in range(39)]
        rows += ["spurious,30,300,False,8194.0,-10.0,-0.87,False"]
        (tmp_path / f"2026_10_13__20_00_{10 + 3 * k}_phot.csv").write_text("\n".join(rows) + "\n")
    result = extinction_map(tmp_path, tmp_path / "map.fits", nframes=10)
    assert result["opaque"] and result["path"] is None
    assert result["median"] == 10.0
    assert result["lost_fraction"] > 0.9
    assert result["tmid"].startswith("2026-10-14T03:00:")      # MST + 7 h


def test_display_badpix_repair():
    """The display products get the nearest-date mask's repair; the input is left raw."""
    from skycam_utils.alcor.masks import load_alcor_badpix_mask
    from omea.render import repair_badpix

    when = datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)
    mask, _ = load_alcor_badpix_mask(when.date())
    rng = np.random.default_rng(1)
    cube = rng.normal(1000, 30, mask.shape).astype(np.int16)
    cube[mask] = 30000
    out = repair_badpix(cube, when)
    assert out[mask].max() < 2000
    assert np.array_equal(out[~mask], cube[~mask])
    assert cube[mask].min() == 30000                    # input left alone
