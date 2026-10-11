"""
Automatic exposure.

The sky signal scales with exposure x gain factor x aperture area, so the
controller works in "total" units of seconds at gain 0 (``exposure *
10**(gain/200)``) and splits that into an exposure and a gain afterwards.

Day: exposure first, at the minimum gain; gain only rises once the exposure is
at its maximum (twilight), as Skywatch does.

Night: the gain is FIXED at ``night_gain`` and only the exposure moves. This is
a deliberate change from Skywatch, which held ~20 s and dropped the gain under
a bright Moon (as low as 60 at full Moon) -- skycam_utils normalises sky
brightness and photometry by exposure only, so a varying gain silently breaks
both. A dark night still pins at 20 s / gain 250, exactly as before.
"""

import math
from dataclasses import dataclass

import numpy as np

from .camera import gain_factor
from .imaging import meter_mask


@dataclass
class Setting:
    exposure: float
    gain: int
    fratio: float


def sky_level(cube, mask, clip=32767):
    """(bias, median sky above bias) in the metering circle, from the G channel."""
    g = cube[1]
    bias = float(np.median(np.concatenate([g[:10, :10].ravel(), g[:10, -10:].ravel(),
                                           g[-10:, :10].ravel(), g[-10:, -10:].ravel()])))
    level = float(np.median(g[mask])) - bias
    return bias, level


class ExposureController:
    def __init__(self, cfg, image_cfg, shape=(1411, 1422)):
        self.cfg = cfg
        self.clip = image_cfg.clip
        self.mask = meter_mask(shape, cfg.meter_x, cfg.meter_y, cfg.meter_radius)
        self.total = None               # set by the first setting() call

    def _split(self, total, night):
        c = self.cfg
        if night:
            gain = c.night_gain
            exposure = total / gain_factor(gain)
        else:
            exposure = total / gain_factor(c.min_gain)
            gain = c.min_gain
            if exposure > c.max_exposure:
                gain = 200 * math.log10(total / c.max_exposure)
                gain = int(round(min(max(gain, c.min_gain), c.max_gain)))
                exposure = total / gain_factor(gain)
        exposure = min(max(exposure, c.min_exposure), c.max_exposure)
        return exposure, int(gain)

    def setting(self, night, fratio):
        if self.total is None:          # first frame: dark-sky values at night
            c = self.cfg
            self.total = (c.max_exposure * gain_factor(c.night_gain) if night
                          else c.start_exposure * gain_factor(c.min_gain))
        exposure, gain = self._split(self.total, night)
        # keep self.total consistent with what the camera can actually do
        self.total = exposure * gain_factor(gain)
        return Setting(exposure, gain, fratio)

    def change_aperture(self, old_fratio, new_fratio):
        """Feed-forward an iris change: f/16 -> f/2.8 is (16/2.8)^2 = 33x more light."""
        if old_fratio and new_fratio and self.total is not None:
            self.total *= (new_fratio / old_fratio) ** 2

    def update(self, cube, exposure, gain, night):
        """Adjust from a frame taken at ``exposure``/``gain``; returns the sky level."""
        c = self.cfg
        bias, level = sky_level(cube, self.mask, self.clip)
        span = self.clip - bias
        target = (c.night_target if night else c.day_target) * span
        if level >= 0.95 * span:            # saturated: we cannot tell by how much
            factor = 0.1
        else:
            factor = target / max(level, 1.0)
        factor = min(max(factor, 0.05), 20.0)
        # small corrections are damped so the exposure does not hunt with clouds
        if 0.7 < factor < 1.4:
            factor = factor ** 0.5
        self.total = exposure * gain_factor(gain) * factor
        return level
