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
longer rising and the heater has run for at least ``dew_hold`` seconds.

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
        self.on = False
        self.since = -math.inf              # time of the last switch
        self.reason = ""

    def _reasons(self, data, trend, fog):
        c = self.cfg
        why = []
        if fog:
            why.append("fog on the dome")
        if data.rh_ext >= c.dew_rh:
            why.append(f"humidity {data.rh_ext:.0f} % >= {c.dew_rh:.0f} %")
        # the ring only stands for the dome while it is not being heated
        if not data.heater_on and data.temp_hot - data.dew_point <= c.dew_margin:
            why.append(f"heater ring {data.temp_hot:.1f} C within {c.dew_margin:.1f} C "
                       f"of the dew point {data.dew_point:.1f} C")
        if trend is not None and trend >= c.dew_rise and data.rh_ext >= c.dew_rise_rh:
            why.append(f"humidity {data.rh_ext:.0f} % rising {trend:.0f} %/h")
        return why

    def dropped(self, data):
        """True when the heater should be on but the board has turned it off."""
        return self.on and not data.heater_on and data.temp_hot < RING_RESUME

    def update(self, data, rows, fog=False):
        """
        Feed one reading (:class:`omea.board.BoardData`) and the recent history
        ``rows`` (and whether the camera sees dome fog); returns True/False
        when the heater should switch, else None.
        """
        c = self.cfg
        trend = rh_trend(rows, data.time)
        why = self._reasons(data, trend, fog)
        if not self.on:
            if why:
                self.on, self.since, self.reason = True, data.time, "; ".join(why)
                return True
            return None
        settled = (data.rh_ext < c.dew_rh - c.dew_hysteresis
                   and (trend is None or trend < c.dew_rise / 2)
                   and data.time - self.since >= c.dew_hold)
        if settled and not why:
            trend_txt = "" if trend is None else f", trend {trend:+.0f} %/h"
            self.on, self.since = False, data.time
            self.reason = f"humidity {data.rh_ext:.0f} %{trend_txt}"
            return False
        return None
