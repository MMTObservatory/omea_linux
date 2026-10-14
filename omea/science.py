"""
Real-time science on each saved frame, through skycam_utils.

Runs in worker processes. For a night frame it does what ``alcor_process_night``
does per frame -- star photometry into ``<frame>_phot.csv`` (which that driver
then REUSES rather than re-measures, since a non-empty CSV marks the frame done)
and the calibrated surface brightness in the fixed ``ALCOR_SB_TARGETS`` cones
plus the darkest cone -- and returns the numbers for redis and the live log.

The surface-brightness helpers are skycam_utils internals (``_``-prefixed);
they are imported only here, so an API change there breaks one module.
"""

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_GEOMETRY = {}          # per-process: WCS key -> _Geometry


class _Geometry:
    """Everything about the sampling cones that depends only on the WCS."""

    def __init__(self, wcs, shape, time):
        from skycam_utils.alcor import load_alcor_horizon_mask
        from skycam_utils.alcor.skybright import (_alcor_best_cone_targets,
                                                  _alcor_cone_indices)
        try:
            horizon, _ = load_alcor_horizon_mask(time)
        except Exception as err:                              # noqa: BLE001
            log.warning("no horizon mask (%s); cones are unmasked", err)
            horizon = None
        self.cones = _alcor_cone_indices(wcs, shape, exclude=horizon)
        self.best_targets = _alcor_best_cone_targets()
        self.best = _alcor_cone_indices(wcs, shape, targets=self.best_targets,
                                        exclude=horizon)
        # unit vectors of every usable sky pixel, so a cone about an arbitrary
        # direction (the MMT pointing) is one dot product per frame
        ny, nx = shape
        yy, xx = np.mgrid[0:ny, 0:nx]
        az, alt = wcs.pixel_to_world_values(xx.astype(float), yy.astype(float))
        valid = np.isfinite(alt)
        if horizon is not None:
            valid &= ~np.asarray(horizon, dtype=bool)
        self.sky = np.flatnonzero(valid)
        a, e = np.radians(az.ravel()[self.sky]), np.radians(alt.ravel()[self.sky])
        self.vec = np.stack([np.cos(e) * np.sin(a), np.cos(e) * np.cos(a), np.sin(e)])

    def cone(self, az, alt, radius):
        """Flat indices of usable sky pixels within ``radius`` deg of (az, alt)."""
        a, e = np.radians(az), np.radians(alt)
        t = np.array([np.cos(e) * np.sin(a), np.cos(e) * np.cos(a), np.sin(e)])
        return self.sky[t @ self.vec >= np.cos(np.radians(radius))]


def _geometry(wcs, shape, time):
    key = (tuple(np.round(wcs.wcs.crpix, 3)), tuple(np.round(wcs.wcs.crval, 5)))
    if key not in _GEOMETRY:
        _GEOMETRY[key] = _Geometry(wcs, shape, time)
    return _GEOMETRY[key]


def _extinction_summary(phot):
    """Median G-band extinction and star counts from one frame's photometry."""
    from skycam_utils.alcor.config import ALCOR_EXT_MAG_WINDOW, ALCOR_EXT_MIN_ALTITUDE
    out = {"nstars": int(len(phot)), "ndetected": 0, "ext_g": float("nan"),
           "ext_g_all": float("nan")}
    if not len(phot) or "mag_g" not in phot:
        return out
    out["ndetected"] = int(np.isfinite(phot["mag_g"]).sum())
    # every detected calibration star: biased low under thick cloud (the most
    # extinguished stars drop out), but still says "cloudy" when the strict
    # window below is empty
    finite = np.isfinite(phot.get("ext_g", np.nan))
    if finite.sum() >= 5:
        out["ext_g_all"] = float(np.median(phot.loc[finite, "ext_g"]))
    sel = (np.isfinite(phot.get("ext_g", np.nan))
           & (phot["altitude"] >= ALCOR_EXT_MIN_ALTITUDE)
           & (phot["mag_g"] >= ALCOR_EXT_MAG_WINDOW[0])
           & (phot["mag_g"] <= ALCOR_EXT_MAG_WINDOW[1]))
    if "variable" in phot:
        sel &= ~phot["variable"].astype(bool)
    if sel.sum() >= 5:
        out["ext_g"] = float(np.median(phot.loc[sel, "ext_g"]))
    return out


def process_frame(path, photometry=True, sun_alt_max=-12.0, target=None):
    """
    Measure one saved frame. Returns a dict of ``allsky_mv_*`` surface
    brightnesses (V mag/arcsec^2) -- the fixed cones, the darkest cone and its
    position, and ``allsky_mv_target`` in the same 5 deg cone about ``target``
    (the MMT's (az, alt) at mid-exposure; NaN without one) -- plus photometry
    counts and single-frame extinction. Raises on a frame it cannot read.
    """
    from skycam_utils.alcor import alcor_star_photometry, load_alcor_fits
    from skycam_utils.alcor.config import ALCOR_SB_APERTURE_RADIUS, ALCOR_SB_TARGETS
    from skycam_utils.alcor.skybright import _alcor_sky_brightness_map, _cone_median
    from skycam_utils.alcor.timeutils import _alcor_frame_time, _read_frame_exposure

    path = Path(path)
    cube, wcs, mask = load_alcor_fits(path)              # bad pixels repaired
    time = _alcor_frame_time(path)
    exposure = _read_frame_exposure(path)
    geom = _geometry(wcs, cube.shape[1:], time)

    mu, _ = _alcor_sky_brightness_map(cube, wcs, time, exposure)
    result = {name: _cone_median(mu, geom.cones[name]) for name in ALCOR_SB_TARGETS}
    best = best_az = best_alt = float("nan")
    for name, idx in geom.best.items():
        value = _cone_median(mu, idx)
        if np.isfinite(value) and (not np.isfinite(best) or value > best):
            best, (best_az, best_alt) = value, geom.best_targets[name]
    result.update(allsky_mv_best=best, best_az=best_az, best_alt=best_alt)
    result["allsky_mv_target"] = float("nan")
    if target is not None:
        result["allsky_mv_target"] = _cone_median(
            mu, geom.cone(target[0], target[1], ALCOR_SB_APERTURE_RADIUS))
        result["target_az"], result["target_alt"] = target

    if photometry:
        phot, csv = alcor_star_photometry(path, frame=(cube, wcs, mask),
                                          sun_alt_max=sun_alt_max)
        result.update(_extinction_summary(phot))
        result["phot_csv"] = str(csv) if csv else ""
    return result


def censored_median(measured, nlost):
    """
    Median extinction when ``nlost`` stars were not detected at all.

    A lost star's extinction is only a lower limit, but it is certainly larger
    than that of any star still measured near it, so all lost stars rank above
    every measured value. If the median lands among them (over half the stars
    are lost), the true median is unknown but at least the largest measured
    value, which is returned as a floor. NaN when nothing was measured.
    """
    values = np.sort(np.asarray(measured, dtype=float))
    values = values[np.isfinite(values)]
    n = len(values) + int(nlost)
    if not len(values):
        return float("nan")
    upper = n // 2                       # index of the upper middle element
    lower = (n - 1) // 2
    if upper >= len(values):             # the median is a lost star: floor
        return float(values[-1])
    return float((values[lower] + values[upper]) / 2)


def extinction_map(day_dir, output_file, nframes=10, opaque_fraction=0.9,
                   opaque_value=10.0):
    """
    Cloud-extinction map from the newest ``nframes`` photometry CSVs of a night:
    a running average, since a single frame's star magnitudes scatter by ~0.3
    mag (undersampled PSF x intra-pixel sensitivity x Bayer matrix).

    Returns None until there are ``nframes`` frames, else a dict with the map
    ``path``, its mid-time ``tmid`` (ISO UT), ``median`` -- the
    :func:`censored_median` of the map's stars, counting the undetected ones
    rather than averaging only over the clear holes the map can still see --
    and ``lost_fraction``, the share of those stars that were not detected.

    When more than ``opaque_fraction`` of the stars are lost (fog, thick
    cloud), the few still measured say nothing about the sky -- a censored
    median would be the largest of them, even a negative one -- so the result
    is ``opaque_value`` with ``opaque`` True and no map (``path`` None).
    """
    import pandas as pd
    from astropy.io import fits
    from astropy.time import Time
    from skycam_utils.alcor import (alcor_extinction_fits, alcor_extinction_stars,
                                    collect_alcor_photometry)
    csvs = sorted(Path(day_dir).glob("*_phot.csv"))[-nframes:]
    if len(csvs) < nframes:
        return None
    df = collect_alcor_photometry(csvs)
    measured, lost = alcor_extinction_stars(df)
    total = len(measured) + len(lost)
    if not total:
        return None
    lost_fraction = len(lost) / total
    if lost_fraction > opaque_fraction:
        stamps = pd.to_datetime(df["OBSTIME"].unique())
        tmid = Time(np.median(Time(stamps.to_pydatetime()).mjd), format="mjd").isot
        return {"path": None, "tmid": tmid, "median": float(opaque_value),
                "lost_fraction": lost_fraction, "opaque": True}
    output_file = Path(output_file)
    tmp = output_file.with_name(f".{output_file.name}.tmp.fits")
    alcor_extinction_fits(df, output_file=tmp, latest=nframes, nframes=nframes,
                          overwrite=True)
    with fits.open(tmp) as hdul:
        tmid = hdul[0].header.get("TMID", "")
        stars = hdul["STARS"].data
        detected = np.asarray(stars["detected"], dtype=bool)
        ext = np.asarray(stars["ext"], dtype=float)
    tmp.replace(output_file)
    nlost = int((~detected).sum())
    return {"path": str(output_file), "tmid": tmid,
            "median": censored_median(ext[detected], nlost),
            "lost_fraction": nlost / len(detected) if len(detected) else float("nan"),
            "opaque": False}
