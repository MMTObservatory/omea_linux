"""
RAW16 sensor frame -> the (3, 1411, 1422) int16 cube Skywatch saved.

Every calibration in skycam_utils (WCS epochs, bad-pixel and horizon masks,
photometric zeropoints, the 32767 saturation level) is defined in Skywatch's
output frame, so this reproduces it:

1. crop the 4144 x 2822 sensor to columns 700..3543 (Skywatch X1=701..X2=3544),
2. bilinear demosaic, channel by channel,
3. average each 2 x 2 block (2844 x 2822 -> 1422 x 1411),
4. clip to the int16 ceiling 32767,
5. mirror in x.

Steps 2-3 were inferred from Skywatch frames (docs/skywatch.md): the channels
share no noise, and the noise correlation between neighbouring output pixels
matches a bilinear demosaic followed by a 2 x 2 mean (R 0.34 vs 0.30 model,
G 0.15 vs 0.16). The scale -- the 2 x 2 mean of RAW16, i.e. the SUM of the true
14-bit values -- reproduces the ~1930 ADU bias at offset 30, and is the reason
saturated pixels pile up at exactly 32767: Skywatch discards the top half of
the RAW16 range.
"""

import numpy as np

# Position of the red pixel within a 2x2 cell for each Bayer pattern.
_RED_SITE = {"RGGB": (0, 0), "GRBG": (0, 1), "GBRG": (1, 0), "BGGR": (1, 1)}


def _red_site(pattern, crop_y, crop_x):
    try:
        ry, rx = _RED_SITE[pattern.upper()]
    except KeyError:
        raise ValueError(f"unknown Bayer pattern {pattern!r}") from None
    return (ry + crop_y) % 2, (rx + crop_x) % 2


def _bin_rggb(raw):
    """
    Bilinear demosaic followed by a 2x2 mean, for an RGGB mosaic of even size.

    Because both steps are linear the result is a fixed weighting of the raw
    pixels around each 2x2 cell, computed here directly by slicing. For the cell
    whose red pixel is R[i, j] (raw[2i, 2j]):

        R = (9 R[i,j] + 3 R[i,j+1] + 3 R[i+1,j] + R[i+1,j+1]) / 16
        B = (9 B[i,j] + 3 B[i-1,j] + 3 B[i,j-1] + B[i-1,j-1]) / 16
        G = (6 (G1 + G2) + the four G pixels just outside the cell) / 16

    Edges reflect, which keeps the Bayer parity.
    """
    p = np.pad(raw.astype(np.float32), 2, mode="reflect")
    ny, nx = raw.shape

    def s(dy, dx):
        return p[2 + dy:2 + dy + ny:2, 2 + dx:2 + dx + nx:2]

    r = (9 * s(0, 0) + 3 * s(0, 2) + 3 * s(2, 0) + s(2, 2)) / 16
    b = (9 * s(1, 1) + 3 * s(-1, 1) + 3 * s(1, -1) + s(-1, -1)) / 16
    g = (6 * (s(0, 1) + s(1, 0))
         + s(-1, 0) + s(0, -1) + s(2, 1) + s(1, 2)) / 16
    return np.stack([r, g, b])


def raw_to_cube(raw, crop_x=700, crop_y=0, crop_width=2844, crop_height=2822,
                bayer="RGGB", mirror_x=True, mirror_y=False, adu_scale=1.0,
                clip=32767):
    """
    Convert a full-sensor RAW16 frame into a Skywatch-format int16 RGB cube.

    Parameters mirror :class:`omea.config.ImageConfig`. Returns an ``int16``
    array of shape ``(3, crop_height // 2, crop_width // 2)``.
    """
    if crop_width % 2 or crop_height % 2:
        raise ValueError("the crop must have even width and height")
    raw = np.asarray(raw)
    if crop_y + crop_height > raw.shape[0] or crop_x + crop_width > raw.shape[1]:
        raise ValueError(f"crop does not fit in a {raw.shape} frame")
    crop = raw[crop_y:crop_y + crop_height, crop_x:crop_x + crop_width]

    # Flip the crop so its red pixel is at (0, 0); a bilinear demosaic is
    # symmetric under flips, so flipping back afterwards is exact.
    ry, rx = _red_site(bayer, crop_y, crop_x)
    if ry:
        crop = crop[::-1]
    if rx:
        crop = crop[:, ::-1]
    cube = _bin_rggb(crop)
    if ry:
        cube = cube[:, ::-1]
    if rx:
        cube = cube[:, :, ::-1]

    if adu_scale != 1.0:
        cube *= adu_scale
    np.clip(cube, -32768, clip, out=cube)
    if mirror_x:
        cube = cube[:, :, ::-1]
    if mirror_y:
        cube = cube[:, ::-1, :]
    return np.ascontiguousarray(np.rint(cube).astype(np.int16))


def cube_to_raw(cube, sensor_shape=(2822, 4144), crop_x=700, crop_y=0,
                bayer="RGGB", mirror_x=True, mirror_y=False, adu_scale=1.0,
                fill=None):
    """
    Approximate inverse of :func:`raw_to_cube`, for the simulator and tests.

    Each output pixel becomes one 2x2 Bayer cell holding its channel values (so
    the geometry round-trips exactly, while the demosaic smoothing does not).
    The rest of the sensor is ``fill`` (default: the cube's corner median).
    """
    cube = np.asarray(cube, dtype=np.float32) / adu_scale
    if mirror_x:
        cube = cube[:, :, ::-1]
    if mirror_y:
        cube = cube[:, ::-1, :]
    _, h, w = cube.shape
    if fill is None:
        fill = float(np.median(cube[:, :10, :10]))
    raw = np.full(sensor_shape, fill, dtype=np.float32)
    ry, rx = _red_site(bayer, crop_y, crop_x)
    cell = np.empty((2 * h, 2 * w), dtype=np.float32)
    cell[ry::2, rx::2] = cube[0]
    cell[1 - ry::2, 1 - rx::2] = cube[2]
    cell[ry::2, 1 - rx::2] = cube[1]
    cell[1 - ry::2, rx::2] = cube[1]
    raw[crop_y:crop_y + 2 * h, crop_x:crop_x + 2 * w] = cell
    return np.clip(np.rint(raw), 0, 65535).astype(np.uint16)


def meter_mask(shape, x, y, radius):
    """Boolean mask of the auto-exposure metering circle."""
    yy, xx = np.ogrid[:shape[0], :shape[1]]
    return (xx - x) ** 2 + (yy - y) ** 2 <= radius ** 2
