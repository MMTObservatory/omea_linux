"""
Proactive dome-heater control.

The board's own automatic mode heats only once the outside humidity is above
a fixed threshold, and the dome can fog well before that: under a clear sky it
radiates below the air temperature (the unheated ring reads 1-2 C under it).
:class:`DewGuard` decides from the board readings instead. It takes the heater
over when

* the outside humidity is above ``dew_rh``, or
* the unheated heater ring is within ``dew_margin`` of the dew point, or
* the humidity has risen faster than ``dew_rise`` per hour over the last hour
  and is above ``dew_rise_rh``, or
* the camera sees fog on the dome (``fog``, from the monitor's fog index),

and hands it back to the board once the humidity has stayed ``dew_hysteresis``
below ``dew_rh`` for ``SETTLE_WINDOW`` seconds (one low reading is often just
noise), is no longer rising and it has had it for at least ``dew_hold``
seconds. The heater is switched off before the board gets it back: its
automatic mode leaves a heater it finds on running.

While it has the heater it regulates rather than heating flat out: the inside
of the housing (a good proxy for the dome) is held ``dew_offset_night`` above
the outside air (``dew_offset_day`` with the Sun up), within
``dew_offset_band`` either way. Sunshine alone usually warms the inside past
that, so the heater mostly stays off by day. It is also off

* once the heater ring reaches ``dew_ring_max``, until it is below
  ``dew_ring_resume`` (the firmware drops a forced heater for good at ~35 C), and
* while the inside or the camera sensor is above ``dew_temp_max``, until both
  are back 1 C below it.

Off here means forced off with the board in manual mode, so its automatic
mode can't heat behind the guard's back.
"""

import math

TREND_WINDOW = 3600.0       # s
TREND_MIN_SPAN = 1800.0     # s of readings needed before trusting a trend
TREND_MIN_POINTS = 10
TEMP_RESUME = 1.0           # C below dew_temp_max to heat again
SETTLE_WINDOW = 600.0       # s the humidity must stay low before handing back


def rh_trend(rows, now, window=TREND_WINDOW):
    """
    Outside humidity rate of change (% per hour): a least-squares slope over
    the ``rows`` (dicts with ``time`` and ``rh_ext``) of the last ``window``
    seconds, or None with too little history.
    """
    pts = [(r["time"], r["rh_ext"]) for r in rows
           if now - window <= r.get("time", -math.inf) <= now
           and math.isfinite(r.get("rh_ext", math.nan))]
    if len(pts) < TREND_MIN_POINTS or pts[-1][0] - pts[0][0] < TREND_MIN_SPAN:
        return None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mh = sum(h for _, h in pts) / n
    var = sum((t - mt) ** 2 for t, _ in pts)
    if var <= 0:
        return None
    return 3600.0 * sum((t - mt) * (h - mh) for t, h in pts) / var


class DewGuard:
    """Heater decisions from board readings; see the module docstring."""

    def __init__(self, cfg):
        self.cfg = cfg                      # BoardConfig
        self.state = "auto"                 # "auto" (the board's own) or "manual" (regulating)
        self.heat = False                   # in manual: heater on
        self.since = -math.inf              # time the guard took the heater
        self.reason = ""
        self.ring_hot = False
        self.too_warm = False
        self._undo = ("auto", False)

    @property
    def on(self):
        return self.state == "manual" and self.heat

    @property
    def previous(self):
        """The state before the last switch."""
        return self._undo[0]

    def undo(self):
        """The last switch failed: go back, to try again at the next reading."""
        self.state, self.heat = self._undo

    def switch(self, state, heat, reason):
        """Record a switch; returns "on", "off" or "auto" for the board."""
        self._undo = (self.state, self.heat)
        self.state, self.heat, self.reason = state, heat, reason
        return "auto" if state == "auto" else ("on" if heat else "off")

    def _reasons(self, data, trend, fog):
        c = self.cfg
        why = []
        if fog:
            why.append("fog on the dome")
        if data.rh_ext >= c.dew_rh:
            why.append(f"outside humidity {data.rh_ext:.0f} % >= {c.dew_rh:.0f} %")
        # the ring only stands for the dome while it is not being heated
        if not data.heater_on and data.temp_hot - data.dew_point <= c.dew_margin:
            why.append(f"heater ring {data.temp_hot:.1f} C within {c.dew_margin:.1f} C "
                       f"of the dew point {data.dew_point:.1f} C")
        if trend is not None and trend >= c.dew_rise and data.rh_ext >= c.dew_rise_rh:
            why.append(f"outside humidity {data.rh_ext:.0f} % rising {trend:.0f} %/h")
        return why

    def _regulate(self, data, sun_up, ccd):
        """Whether the heater should be on while regulating, and why."""
        c = self.cfg
        if data.temp_hot >= c.dew_ring_max:
            self.ring_hot = True
        elif data.temp_hot < c.dew_ring_resume:
            self.ring_hot = False
        warmest = max(data.temp_int, ccd if math.isfinite(ccd) else -math.inf)
        if warmest > c.dew_temp_max:
            self.too_warm = True
        elif warmest <= c.dew_temp_max - TEMP_RESUME:
            self.too_warm = False
        offset = data.temp_int - data.temp_ext
        target = c.dew_offset_day if sun_up else c.dew_offset_night
        txt = f"inside {offset:+.1f} C on outside, target {target:+.1f} C"
        if self.too_warm:
            sensor = f", camera sensor {ccd:.1f} C" if math.isfinite(ccd) else ""
            return False, f"inside {data.temp_int:.1f} C{sensor}, limit {c.dew_temp_max:.0f} C"
        if self.ring_hot:
            return False, f"heater ring {data.temp_hot:.1f} C, limit {c.dew_ring_max:.1f} C"
        if offset < target - c.dew_offset_band:
            return True, txt
        if offset > target + c.dew_offset_band:
            return False, txt
        return self.heat, txt

    def dropped(self, data):
        """True when the heater should be on but the board has turned it off."""
        return self.on and not data.heater_on and data.temp_hot < self.cfg.dew_ring_resume

    def update(self, data, rows, fog=False, sun_up=False, ccd=math.nan):
        """
        Feed one reading (:class:`omea.board.BoardData`), the recent history
        ``rows``, whether the camera sees dome fog and the Sun is up, and the
        camera sensor temperature; returns "on", "off" or "auto" (hand back to
        the board) when the heater should switch, else None.
        """
        c = self.cfg
        trend = rh_trend(rows, data.time)
        why = self._reasons(data, trend, fog)
        if self.state == "auto":
            if not why:
                return None
            self.since = data.time
            heat, txt = self._regulate(data, sun_up, ccd)
            return self.switch("manual", heat, f"{'; '.join(why)}; {txt}")
        recent = [r["rh_ext"] for r in rows
                  if data.time - SETTLE_WINDOW <= r.get("time", -math.inf) <= data.time
                  and math.isfinite(r.get("rh_ext", math.nan))]
        settled = (data.time - self.since >= c.dew_hold
                   and max(recent + [data.rh_ext]) < c.dew_rh - c.dew_hysteresis
                   and (trend is None or trend < c.dew_rise / 2))
        trend_txt = "" if trend is None else f", trend {trend:+.0f} %/h"
        if settled and not why:
            return self.switch("auto", False,
                                f"outside humidity {data.rh_ext:.0f} %{trend_txt}")
        heat, txt = self._regulate(data, sun_up, ccd)
        demand = "; ".join(why) or f"outside humidity {data.rh_ext:.0f} %{trend_txt}"
        if heat != self.heat:
            return self.switch("manual", heat, f"{txt} ({demand})")
        self.reason = f"{txt} ({demand})"
        return None
