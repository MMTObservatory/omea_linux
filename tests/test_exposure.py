import numpy as np
import pytest

from omea.camera import gain_factor
from omea.config import ExposureConfig, ImageConfig
from omea.exposure import ExposureController


def sky(level, bias=1930):
    cube = np.full((3, 1411, 1422), bias, np.float64)
    yy, xx = np.ogrid[:1411, :1422]
    inside = (xx - 696) ** 2 + (yy - 698) ** 2 < 650 ** 2
    cube[:, inside] += level
    return cube


def converge(ctrl, brightness, night, n=30):
    """``brightness``: sky ADU per (second x gain factor)."""
    s = ctrl.setting(night, 2.8)
    for _ in range(n):
        level = brightness * s.exposure * gain_factor(s.gain)
        ctrl.update(sky(min(level, 30000)), s.exposure, s.gain, night)
        s = ctrl.setting(night, 2.8)
    return s


def test_day_prefers_exposure_over_gain():
    cfg = ExposureConfig()
    s = converge(ExposureController(cfg, ImageConfig()), 1e6, night=False)
    assert s.gain == cfg.min_gain
    assert s.exposure == pytest.approx(cfg.day_target * (32767 - 1930) / 1e6, rel=0.1)


def test_twilight_raises_gain_once_exposure_is_maxed():
    s = converge(ExposureController(ExposureConfig(), ImageConfig()), 50.0, night=False)
    assert s.exposure == 20.0 and 0 < s.gain <= 250


def test_dark_night_pins_at_20s_gain_250():
    s = converge(ExposureController(ExposureConfig(), ImageConfig()), 0.5, night=True)
    assert (s.exposure, s.gain) == (20.0, 250)


def test_moonlit_night_shortens_exposure_not_gain():
    cfg = ExposureConfig()
    s = converge(ExposureController(cfg, ImageConfig()), 3000.0 / gain_factor(250), night=True)
    assert s.gain == 250
    assert s.exposure < 20.0
    assert s.exposure == pytest.approx(cfg.night_target * (32767 - 1930) / 3000.0, rel=0.1)


def test_iris_change_is_fed_forward():
    ctrl = ExposureController(ExposureConfig(), ImageConfig())
    ctrl.total = 3.0
    ctrl.change_aperture(16.0, 2.8)
    assert ctrl.total == pytest.approx(3.0 * (2.8 / 16) ** 2)


def test_cold_start_at_night_reaches_dark_sky_in_one_step():
    ctrl = ExposureController(ExposureConfig(), ImageConfig())
    s = ctrl.setting(True, 2.8)
    assert (s.exposure, s.gain) == (1.0, 250)
    # a dark sky (pins at 20 s): the next frame is already there
    ctrl.update(sky(0.5 * gain_factor(250) * 1.0 * 30), s.exposure, s.gain, True)
    assert ctrl.setting(True, 2.8).exposure == 20.0


def test_restart_in_twilight_starts_from_the_last_frame():
    # tonight's last day frame before a restart: 20 s, gain 30, f/16, 4493 ADU
    ctrl = ExposureController(ExposureConfig(), ImageConfig())
    ctrl.seed(20.0, 30, 16.0, 4493.0)
    s = ctrl.setting(True, 2.8)
    assert s.gain == 250
    expect = (20.0 * gain_factor(30) * (0.25 * 30767 / 4493) * (2.8 / 16) ** 2
              / gain_factor(250))
    assert s.exposure == pytest.approx(expect)
    assert s.exposure < 0.2                    # not 20 s: nothing saturates
