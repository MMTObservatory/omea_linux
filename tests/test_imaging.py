import numpy as np
import pytest
from scipy.ndimage import convolve

from omea.imaging import _bin_rggb, cube_to_raw, meter_mask, raw_to_cube


def _reference(raw):
    """Textbook bilinear demosaic (masked convolution), then 2x2 mean."""
    r = np.zeros_like(raw)
    r[0::2, 0::2] = 1
    b = np.zeros_like(raw)
    b[1::2, 1::2] = 1
    g = 1 - r - b
    krb = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]]) / 4
    kg = np.array([[0, 1, 0], [1, 4, 1], [0, 1, 0]]) / 4
    full = np.stack([convolve(raw * r, krb, mode="mirror"),
                     convolve(raw * g, kg, mode="mirror"),
                     convolve(raw * b, krb, mode="mirror")])
    ny, nx = raw.shape
    return full.reshape(3, ny // 2, 2, nx // 2, 2).mean(axis=(2, 4))


def test_closed_form_equals_bilinear():
    raw = np.random.default_rng(1).integers(0, 16000, (36, 44)).astype(np.float32)
    assert np.allclose(_bin_rggb(raw), _reference(raw), atol=1e-3)


@pytest.mark.parametrize("pattern,dy,dx", [("GBRG", 1, 0), ("GRBG", 0, 1), ("BGGR", 1, 1)])
def test_bayer_phase(pattern, dy, dx):
    full = np.random.default_rng(2).integers(0, 4000, (44, 52)).astype(np.uint16)
    a = raw_to_cube(full, 4, 4, 40, 34, "RGGB", mirror_x=False)
    # the same pixels described from a frame starting dy/dx later
    b = raw_to_cube(full[dy:, dx:], 4 - dx, 4 - dy, 40, 34, pattern, mirror_x=False)
    assert np.array_equal(a, b)


def test_shape_mirror_and_clip():
    raw = np.full((2822, 4144), 1000, np.uint16)
    raw[:, 700:702] = 65535                     # first two crop columns: saturated
    cube = raw_to_cube(raw)
    assert cube.shape == (3, 1411, 1422) and cube.dtype == np.int16
    # mirrored: the saturated sensor columns end up at the right-hand edge
    assert cube[:, 700, -1].min() == 32767
    assert np.all(cube[:, 700, :-2] == 1000)


def test_flat_field_is_preserved():
    raw = np.full((2822, 4144), 7720, np.uint16)
    assert np.all(raw_to_cube(raw) == 7720)


def test_round_trip_geometry():
    cube = np.zeros((3, 1411, 1422), np.int16) + 2000
    cube[:, 300, 900] = 9000                    # a "star"
    back = raw_to_cube(cube_to_raw(cube))
    assert np.unravel_index(np.argmax(back[1]), back[1].shape) == (300, 900)


def test_meter_mask():
    m = meter_mask((1411, 1422), 696, 698, 596)
    assert m[698, 696] and not m[0, 0]
    assert abs(m.sum() - np.pi * 596 ** 2) / m.sum() < 0.01


def _keogram_batch(times, columns, width):
    """The original all-at-once keogram, as the reference for the painted strip."""
    strip = np.zeros((columns[0].shape[0], width, 3), np.uint8)
    x = (np.arange(width) + 0.5) * 86400.0 / width
    idx = np.searchsorted(times, x, side="right") - 1
    live = (idx >= 0) & (x <= times[-1] + 86400.0 / width)
    strip[:, live] = np.stack(columns, axis=1)[:, idx[live]]
    return strip


def test_keogram_paints_like_batch_and_survives_restart(tmp_path):
    from omea.render import Keogram
    rng = np.random.default_rng(3)
    width = 288                                           # 300 s columns
    times = np.sort(rng.uniform(3600, 50000, 120))
    times[10:13] = times[10] + np.array([0, 20, 40])     # several frames in one column
    times[50] += 4000                                    # and a gap
    times = np.sort(times)
    frames = [rng.integers(0, 255, (16, 5, 3), dtype=np.uint8) for _ in times]
    columns = [f[::-1, 2, :] for f in frames]
    keo = Keogram(height=16, width=width)
    for k, (t, f) in enumerate(zip(times, frames)):
        keo.add("2026-10-14", t, f, 2)
        if k == 60:                                       # restart half-way
            keo.save(tmp_path / "k.npz")
            keo = Keogram(height=16, width=width)
            assert not keo.load(tmp_path / "k.npz", "2026-10-15")      # another day
            assert keo.load(tmp_path / "k.npz", "2026-10-14")
    want = _keogram_batch(times, columns, width)
    assert np.array_equal(np.asarray(keo.image()), want)
