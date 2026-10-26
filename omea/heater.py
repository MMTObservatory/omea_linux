"""
Proactive dome-heater control.

The board's own automatic mode heats only once the outside humidity is above
a fixed threshold, and the dome can fog well before that: under a clear sky it
radiates below the air temperature (the unheated ring reads 1-2 C under it).
:class:`DewGuard` decides from the board readings instead, heating when

* the outside humidity is above ``dew_rh``, or
* the unheated heater ring (a proxy for the dome) is within ``dew_margin`` of
  the dew point, or
* the humidity has risen faster than ``dew_rise`` per hour over the last hour
  and is above ``dew_rise_rh``, or
* the camera sees fog on the dome (``fog``, from the monitor's fog index),

and stopping once the humidity is ``dew_hysteresis`` below ``dew_rh``, is no
longer rising and the heater has run for at least ``dew_hold`` seconds. With
the Sun up and the humidity falling (the morning dry-out) the humidity level
does not count, and the heater stops after ``dew_hold`` whatever the level.

Whatever the rules say, the heater is held off while the inside of the housing
is more than ``dew_warm`` above the outside air (sunshine, or the heater has
already warmed it), until it is back within ``dew_warm - dew_warm_hysteresis``.
Held off means forced off, not handed back: the board's own automatic mode
would otherwise heat regardless.

The board's firmware drops a forced heater for good once the ring reaches
about 35 C (every sunny afternoon, 10-15 to 10-18), so the guard asks for it to
be forced on again once the ring has cooled below ``RING_RESUME``.
"""

import math

TREND_WINDOW = 3600.0       # s
TREND_MIN_SPAN = 1800.0     # s of readings needed before trusting a trend
TREND_MIN_POINTS = 10
RING_RESUME = 30.0          # C, re-force a dropped heater once the ring is below this


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
    """Heater on/off decisions from board readings; see the module docstring."""

    def __init__(self, cfg):
        self.cfg = cfg                      # BoardConfig
        self.state = "auto"                 # "auto" (the board's own), "on" or "off" (forced)
        self.previous = "auto"              # to go back to if the switch fails
        self.since = -math.inf              # time of the last switch to "on"
        self.reason = ""

    def _switch(self, state, reason, now):
        self.previous, self.state, self.reason = self.state, state, reason
        if state == "on":
            self.since = now
        return state

    @property
    def on(self):
        return self.state == "on"

    def _reasons(self, data, trend, fog, drying):
        c = self.cfg
        why = []
        if fog:
            why.append("fog on the dome")
        if data.rh_ext >= c.dew_rh and not drying:
            why.append(f"outside humidity {data.rh_ext:.0f} % >= {c.dew_rh:.0f} %")
        # the ring only stands for the dome while it is not being heated
        if not data.heater_on and data.temp_hot - data.dew_point <= c.dew_margin:
            why.append(f"heater ring {data.temp_hot:.1f} C within {c.dew_margin:.1f} C "
                       f"of the dew point {data.dew_point:.1f} C")
        if trend is not None and trend >= c.dew_rise and data.rh_ext >= c.dew_rise_rh:
            why.append(f"outside humidity {data.rh_ext:.0f} % rising {trend:.0f} %/h")
        return why

    def dropped(self, data):
        """True when the heater should be on but the board has turned it off."""
        return self.on and not data.heater_on and data.temp_hot < RING_RESUME

    def update(self, data, rows, fog=False, sun_up=False):
        """
        Feed one reading (:class:`omea.board.BoardData`) and the recent history
        ``rows`` (and whether the camera sees dome fog, and the Sun is up);
        returns the new state ("on", "off" or "auto") when the heater should
        switch, else None.
        """
        c = self.cfg
        trend = rh_trend(rows, data.time)
        drying = sun_up and trend is not None and trend < 0
        why = self._reasons(data, trend, fog, drying)
        warmth = data.temp_int - data.temp_ext
        warm_txt = (f"inside {data.temp_int:.1f} C, {warmth:.1f} C above outside "
                    f"{data.temp_ext:.1f} C")
        if self.state == "off":
            if warmth > c.dew_warm - c.dew_warm_hysteresis:
                self.reason = f"held off, {warm_txt}"
                return None
            if not why:
                return self._switch("auto", warm_txt, data.time)
        elif warmth > c.dew_warm:
            return self._switch("off", f"{warm_txt} > {c.dew_warm:.1f} C", data.time)
        if self.state != "on":
            if why:
                return self._switch("on", "; ".join(why), data.time)
            return None
        settled = (data.time - self.since >= c.dew_hold
                   and (drying or (data.rh_ext < c.dew_rh - c.dew_hysteresis
                                   and (trend is None or trend < c.dew_rise / 2))))
        trend_txt = "" if trend is None else f", trend {trend:+.0f} %/h"
        if settled and not why:
            day = ", Sun up" if drying else ""
            return self._switch("auto", f"outside humidity {data.rh_ext:.0f} %{trend_txt}{day}",
                                data.time)
        # keep the reason current, for the log when the board drops the heater
        self.reason = "; ".join(why) or f"held on, outside humidity {data.rh_ext:.0f} %{trend_txt}"
        return None
