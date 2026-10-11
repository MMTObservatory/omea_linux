"""
Check that frames from this software line up with Skywatch's.

Every skycam_utils calibration (WCS epochs, bad-pixel and horizon masks) is
pixel-exact in Skywatch's frame, so before trusting the new system compare a
frame it took with a Skywatch frame of the same scene. Use DAYTIME frames a few
minutes apart: the terrain and buildings are fixed, while at night the stars
move. Clouds hurt; a clear afternoon is best.

The comparison tries the four flips, phase-correlates the high-passed G
channels for each, and reports the best orientation, the residual shift, and
the bias and gain-normalised signal levels.
"""

import numpy as np
from astropy.io import fits
from scipy.ndimage import gaussian_filter

from .camera import gain_factor

FLIPS = {"none": (False, False), "flip x": (True, False),
         "flip y": (False, True), "flip x+y": (True, True)}


def _load(path):
    with fits.open(path) as hdul:
        return hdul[0].data.astype(np.float64), hdul[0].header


def _structure(g):
    """Log image with the smooth sky removed: edges of terrain, buildings, rod."""
    img = np.log10(np.clip(g, 1, None))
    hp = img - gaussian_filter(img, 8)
    hp -= hp.mean()
    yy, xx = np.ogrid[:g.shape[0], :g.shape[1]]
    cy, cx = g.shape[0] / 2, g.shape[1] / 2
    window = np.exp(-(((yy - cy) / (0.45 * g.shape[0])) ** 8
                      + ((xx - cx) / (0.45 * g.shape[1])) ** 8))
    return hp * window


def _phase_correlate(a, b):
    fa, fb = np.fft.fft2(a), np.fft.fft2(b)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-12
    corr = np.fft.ifft2(r).real
    iy, ix = np.unravel_index(np.argmax(corr), corr.shape)
    peak = corr[iy, ix] / (corr.std() + 1e-12)
    dy = iy if iy <= a.shape[0] // 2 else iy - a.shape[0]
    dx = ix if ix <= a.shape[1] // 2 else ix - a.shape[1]
    return dx, dy, peak


def compare_frames(new_path, ref_path):
    new, hn = _load(new_path)
    ref, hr = _load(ref_path)
    lines = [f"new:       {new_path}", f"reference: {ref_path}"]
    if new.shape != ref.shape:
        return "\n".join(lines + [f"SHAPE MISMATCH {new.shape} vs {ref.shape}"])

    sref = _structure(ref[1])
    results = []
    for name, (fx, fy) in FLIPS.items():
        g = new[1][:, ::-1] if fx else new[1]
        g = g[::-1] if fy else g
        dx, dy, peak = _phase_correlate(_structure(g), sref)
        results.append((peak, name, dx, dy))
    results.sort(reverse=True)
    lines.append("orientation   shift(dx, dy)   correlation peak (sigma)")
    for peak, name, dx, dy in results:
        lines.append(f"  {name:10s}  ({dx:+5d}, {dy:+5d})   {peak:8.1f}")
    peak, name, dx, dy = results[0]
    second = results[1][0]

    def levels(cube, h):
        bias = np.median(cube[:, :10, :10], axis=(1, 2))
        sky = np.median(cube[:, 500:900, 500:900], axis=(1, 2)) - bias
        norm = float(h.get("EXPOSURE", 1)) * gain_factor(int(h.get("GAIN_ELE", 0)))
        return bias, sky / norm

    bn, sn = levels(new, hn)
    br, sr = levels(ref, hr)
    lines.append(f"bias R/G/B   new {np.round(bn)}  reference {np.round(br)}")
    lines.append(f"signal / (exposure x gain factor), new/reference R/G/B: "
                 f"{np.round(sn / sr, 3)}  (iris new f/{hn.get('FRATIO', '?')}, "
                 f"reference {[c for c in hr.get('COMMENT', []) if 'Iris' in str(c)]})")
    if peak < 3 * second:
        verdict = "INCONCLUSIVE: no orientation stands out (use clear daytime frames)"
    elif name == "none" and dx == 0 and dy == 0:
        verdict = "OK: same orientation, no shift -- calibrations apply unchanged"
    else:
        verdict = (f"MISMATCH: best match is '{name}' shifted ({dx:+d}, {dy:+d}) px; "
                   "adjust [image] crop_x/crop_y/mirror_x/mirror_y")
    lines.append(verdict)
    return "\n".join(lines)
