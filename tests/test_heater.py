from omea import board
from omea.config import BoardConfig
from omea.heater import DewGuard, rh_trend


def reading(t, temp, rh, hot=None, heater=False, inside=3.0):
    return board.BoardData(time=t, temp_ext=temp, rh_ext=rh, temp_int=temp + inside,
                           rh_int=50.0, temp_hot=temp - 1.5 if hot is None else hot,
                           heater_on=heater, auto_heat=True, heater_power=50,
                           dew_point=board.dew_point(temp, rh))


def test_trend_needs_half_an_hour():
    rows = [{"time": 30.0 * i, "rh_ext": 30 + 0.1 * i} for i in range(40)]
    assert rh_trend(rows, rows[-1]["time"]) is None             # 20 min
    rows = [{"time": 30.0 * i, "rh_ext": 30 + 0.1 * i} for i in range(120)]
    assert abs(rh_trend(rows, rows[-1]["time"]) - 12.0) < 1e-6  # 0.1 % per 30 s


def test_rising_humidity_heats_early_and_stops_late():
    """Tonight's case: humidity climbing ~25 %/h at 7-9 C; the dome fogged near 78 %."""
    cfg = BoardConfig()
    guard = DewGuard(cfg)
    rows, switches = [], []
    for i in range(240):                    # 2 h at 30 s
        t = 30.0 * i
        rh = 25 + 55 * i / 240
        data = reading(t, 8.0, rh, heater=guard.on)
        rows.append({"time": t, "rh_ext": rh})
        s = guard.update(data, rows)
        if s is not None:
            switches.append((t, s, rh))
    assert switches and switches[0][1] == "on"
    assert switches[0][2] < 50              # well before the 80 % board threshold
    assert "rising" in guard.reason
    # now drying out: stays on through the hold, then hands back
    t0 = rows[-1]["time"]
    for i in range(1, 240):
        t = t0 + 30.0 * i
        rh = max(30.0, 80 - i)
        rows.append({"time": t, "rh_ext": rh})
        s = guard.update(reading(t, 8.0, rh, heater=guard.on), rows)
        if s is not None:
            switches.append((t, s, rh))
    assert [s for _, s, _ in switches] == ["on", "auto"]
    assert switches[1][2] < cfg.dew_rh - cfg.dew_hysteresis


def test_cold_dome_near_dew_point_heats():
    guard = DewGuard(BoardConfig())
    # steady 55 %, but the ring has radiated down to within 4 C of the dew point
    data = reading(0.0, 5.0, 55.0, hot=-0.5)
    assert guard.update(data, []) == "on"
    assert "heater ring" in guard.reason


def test_dry_steady_night_stays_off():
    guard = DewGuard(BoardConfig())
    rows = [{"time": 30.0 * i, "rh_ext": 30.0} for i in range(120)]
    for r in rows:
        assert guard.update(reading(r["time"], 8.0, 30.0), rows) is None


def test_dropped_heater_is_forced_again_once_the_ring_cools():
    guard = DewGuard(BoardConfig())
    assert guard.update(reading(0.0, 8.0, 99.0), []) == "on"
    assert not guard.dropped(reading(30.0, 8.0, 99.0, hot=20.0, heater=True))
    assert not guard.dropped(reading(60.0, 8.0, 99.0, hot=35.2))   # firmware cut-out
    assert guard.dropped(reading(90.0, 8.0, 99.0, hot=12.0))
    # the reason logged on re-forcing is the current humidity, not the switch-on one
    assert guard.update(reading(120.0, 8.0, 76.0, hot=12.0), []) is None
    assert guard.reason == "inside +3.0 C on outside, target +7.0 C (outside humidity 76 % >= 60 %)"
    assert guard.update(reading(150.0, 8.0, 55.0, hot=12.0), []) is None
    assert guard.reason.endswith("(outside humidity 55 %)")


def test_regulates_inside_offset_day_and_night():
    guard = DewGuard(BoardConfig())
    assert guard.update(reading(0.0, 4.0, 92.0, inside=3.0), []) == "on"
    assert "target +7.0 C" in guard.reason
    assert guard.update(reading(30.0, 4.0, 92.0, inside=7.2, heater=True), []) is None  # in band
    assert guard.update(reading(60.0, 4.0, 92.0, inside=7.6, heater=True), []) == "off"
    assert guard.state == "manual"          # forced off, not handed to the board
    assert guard.update(reading(90.0, 4.0, 92.0, inside=6.8), []) is None
    assert guard.update(reading(120.0, 4.0, 92.0, inside=6.4), []) == "on"
    # Sun up: a 5 C target, and sunshine alone keeps the inside above it
    assert guard.update(reading(150.0, 4.0, 92.0, inside=6.4, heater=True), [], sun_up=True) == "off"
    assert guard.update(reading(180.0, 4.0, 92.0, inside=4.4), [], sun_up=True) == "on"


def test_ring_and_temperature_limits():
    guard = DewGuard(BoardConfig())
    assert guard.update(reading(0.0, 4.0, 92.0), []) == "on"
    assert guard.update(reading(30.0, 4.0, 92.0, hot=32.6, heater=True), []) == "off"
    assert "heater ring" in guard.reason
    assert guard.update(reading(60.0, 4.0, 92.0, hot=31.0), []) is None    # not cool enough yet
    assert not guard.dropped(reading(60.0, 4.0, 92.0, hot=12.0))
    assert guard.update(reading(90.0, 4.0, 92.0, hot=29.5), []) == "on"
    # a warm inside or camera sensor holds it off until 1 C below the limit
    assert guard.update(reading(120.0, 28.0, 92.0, inside=2.5, heater=True), []) == "off"
    assert guard.update(reading(150.0, 25.0, 92.0, inside=4.5), []) is None   # 29.5 C
    assert guard.update(reading(180.0, 25.0, 92.0, inside=3.5), []) == "on"   # 28.5 C
    assert guard.update(reading(210.0, 4.0, 92.0, heater=True), [], ccd=31.0) == "off"
    assert "camera sensor 31.0 C" in guard.reason


def test_monitor_forces_heater_and_restores_auto():
    from omea.config import Config
    from omea.monitor import Monitor
    cfg = Config()
    cfg.board.driver = "sim"
    mon = Monitor(cfg, publish=False)
    mon.board = board.open_board(cfg.board)
    mon._configure_heater()
    mon._force_heater("on", "test")
    assert not mon.board.auto and mon.board.read().heater_on
    mon._force_heater("off", "test")
    assert not mon.board.auto and not mon.board.read().heater_on
    mon._force_heater("on", "test")
    mon._force_heater("auto", "test")
    assert mon.board.auto and mon.board.thresholds == (10.0, 80.0)
    assert not mon.board.heater             # handed back switched off


def test_one_low_reading_does_not_hand_back():
    """10-26 11:28 UT: one 48 % reading among 51-57 % handed the heater back."""
    guard = DewGuard(BoardConfig())
    rows = []

    def feed(t, rh):
        rows.append({"time": t, "rh_ext": rh})
        return guard.update(reading(t, 6.0, rh, heater=guard.on), rows)

    assert feed(0.0, 70.0) == "on"
    t = 0.0
    while t < 3600:                          # past the hold, humidity flat ~55 %
        t += 30.0
        assert feed(t, 55.0) in (None, "on", "off")
    assert feed(t + 30, 48.0) != "auto"      # one dip
    assert guard.state == "manual"
    for i in range(2, 30):                   # properly dry for 15 min
        s = feed(t + 30.0 * i, 45.0)
        if s == "auto":
            break
    assert guard.state == "auto"


def _phot(high_ext, low_ext, high_lost=0):
    import numpy as np
    import pandas as pd
    rows = []
    for i in range(20):
        rows.append({"altitude": 70.0, "mag_g": np.nan if i < high_lost else -10.0,
                     "ext_g": np.nan if i < high_lost else high_ext + 0.01 * (i % 3)})
        rows.append({"altitude": 25.0, "mag_g": -10.0, "ext_g": low_ext})
        rows.append({"altitude": 45.0, "mag_g": -10.0, "ext_g": 5.0})     # ignored band
    return pd.DataFrame(rows)


def test_fog_index():
    from omea.science import fog_index
    assert abs(fog_index(_phot(0.0, 0.0))) < 0.02
    assert abs(fog_index(_phot(1.5, 0.1)) - 1.4) < 0.02
    # most zenith stars lost: the censored median floors at the worst measured
    assert fog_index(_phot(2.0, 0.0, high_lost=15)) >= 2.0


def test_dome_fog_flag_hysteresis_and_heater():
    import math
    from omea.config import Config
    from omea.monitor import Monitor
    cfg = Config()
    mon = Monitor(cfg, publish=False)
    mon.status.board = {"rh_ext": 78.0}
    assert [mon._update_fog(1.0)["allsky_dome_fog"] for _ in range(3)] == [0, 0, 0]  # no map yet
    mon._fog_recent.clear()
    mon._lost = 0.01
    flags = [mon._update_fog(v)["allsky_dome_fog"]
             for v in (0.0, 0.5, 0.0, 0.6, 0.7, math.nan, 0.15, 0.05, 0.05)]
    assert flags == [0, 0, 0, 1, 1, 1, 1, 1, 0]     # median of 3, on 0.2 / off 0.1
    mon.status.board = {"rh_ext": 30.0}            # dry: zenith cloud, not dome fog
    assert [mon._update_fog(1.0)["allsky_dome_fog"] for _ in range(3)] == [0, 0, 0]
    mon.status.board = {"rh_ext": 78.0}
    mon._lost = 0.3                                # passing cloud hides stars
    assert [mon._update_fog(1.0)["allsky_dome_fog"] for _ in range(3)] == [0, 0, 0]
    # ...unless the index stays up: fog thick enough to hide stars still counts
    mon._fog_recent.clear()
    mon._fog_high_since = None
    flags = [mon._update_fog(v, now=60.0 * i)["allsky_dome_fog"]
             for i, v in enumerate([1.0] * 20 + [0.15] + [math.nan] * 5 + [1.0] * 10)]
    assert flags.index(1) == 32                    # 30 min after the index reached 0.2
    guard = DewGuard(BoardConfig())
    assert guard.update(reading(0.0, 8.0, 30.0), [], fog=True) == "on"
    assert guard.reason.startswith("fog on the dome; ")


def test_csv_keeps_an_older_header(tmp_path):
    from omea import outputs
    path = tmp_path / "x.csv"
    outputs.append_csv(path, ["a", "b"], {"a": 1, "b": 2})
    outputs.append_csv(path, ["a", "b", "c"], {"a": 3, "b": 4, "c": 5})
    assert path.read_text().splitlines() == ["a,b", "1,2", "3,4"]


def test_history_rows_since_00_ut():
    from datetime import datetime, timezone
    from omea.monitor import _since
    rows = [{"time": "2026-10-14T23:59:00+00:00"}, {"time": "2026-10-15T00:00:00+00:00"},
            {"time": "2026-10-15T03:00:00.5Z"}, {"time": "2026-10-15T04:00:00"}, {"time": ""}]
    since = datetime(2026, 10, 15, tzinfo=timezone.utc)
    assert [r["time"][:13] for r in _since(rows, since)] == \
        ["2026-10-15T00", "2026-10-15T03", "2026-10-15T04"]
