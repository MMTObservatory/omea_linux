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

_GEOMETRY = {}          # per-process: WCS key -> (cones, best cones, best targets)


def _geometry(wcs, shape, time):
    from skycam_utils.alcor import load_alcor_horizon_mask
    from skycam_utils.alcor.skybright import (_alcor_best_cone_targets,
                                              _alcor_cone_indices)
    key = (tuple(np.round(wcs.wcs.crpix, 3)), tuple(np.round(wcs.wcs.crval, 5)))
    if key not in _GEOMETRY:
        try:
            horizon, _ = load_alcor_horizon_mask(time)
        except Exception as err:                              # noqa: BLE001
            log.warning("no horizon mask (%s); cones are unmasked", err)
            horizon = None
        cones = _alcor_cone_indices(wcs, shape, exclude=horizon)
        targets = _alcor_best_cone_targets()
        best = _alcor_cone_indices(wcs, shape, targets=targets, exclude=horizon)
        _GEOMETRY[key] = (cones, best, targets)
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


def process_frame(path, photometry=True, sun_alt_max=-12.0):
    """
    Measure one saved frame. Returns a dict of ``allsky_mv_*`` surface
    brightnesses (V mag/arcsec^2), the darkest cone's position, and photometry
    counts with the median extinction. Raises on a frame it cannot read.
    """
    from skycam_utils.alcor import alcor_star_photometry, load_alcor_fits
    from skycam_utils.alcor.config import ALCOR_SB_TARGETS
    from skycam_utils.alcor.skybright import _alcor_sky_brightness_map, _cone_median
    from skycam_utils.alcor.timeutils import _alcor_frame_time, _read_frame_exposure

    path = Path(path)
    cube, wcs, mask = load_alcor_fits(path)              # bad pixels repaired
    time = _alcor_frame_time(path)
    exposure = _read_frame_exposure(path)
    cones, best_cones, best_targets = _geometry(wcs, cube.shape[1:], time)

    mu, _ = _alcor_sky_brightness_map(cube, wcs, time, exposure)
    result = {name: _cone_median(mu, cones[name]) for name in ALCOR_SB_TARGETS}
    best = best_az = best_alt = float("nan")
    for name, idx in best_cones.items():
        value = _cone_median(mu, idx)
        if np.isfinite(value) and (not np.isfinite(best) or value > best):
            best, (best_az, best_alt) = value, best_targets[name]
    result.update(allsky_mv_best=best, best_az=best_az, best_alt=best_alt)

    if photometry:
        phot, csv = alcor_star_photometry(path, frame=(cube, wcs, mask),
                                          sun_alt_max=sun_alt_max)
        result.update(_extinction_summary(phot))
        result["phot_csv"] = str(csv) if csv else ""
    return result


def extinction_map(day_dir, output_file, nframes=10):
    """Cloud-extinction map from the newest ``nframes`` photometry CSVs of a night."""
    from skycam_utils.alcor import alcor_extinction_fits
    csvs = sorted(Path(day_dir).glob("*_phot.csv"))[-nframes:]
    if len(csvs) < nframes:
        return None
    tmp = Path(output_file).with_name(f".{Path(output_file).name}.tmp.fits")
    alcor_extinction_fits(csvs, output_file=tmp, latest=nframes, nframes=nframes,
                          overwrite=True)
    tmp.replace(output_file)
    return Path(output_file)
