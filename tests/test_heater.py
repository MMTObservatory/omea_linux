from omea import board
from omea.config import BoardConfig
from omea.heater import DewGuard


def reading(t, temp, rh, hot=None, heater=False, inside=3.0):
    return board.BoardData(time=t, temp_ext=temp, rh_ext=rh, temp_int=temp + inside,
                           rh_int=50.0, temp_hot=temp - 1.5 if hot is None else hot,
                           heater_on=heater, auto_heat=True, heater_power=50,
                           dew_point=board.dew_point(temp, rh))


def test_humid_night_heats_then_hands_back_when_dry():
    cfg = BoardConfig()
    guard = DewGuard(cfg)
    rows, switches = [], []
    for i in range(240):                    # 2 h at 30 s, humidity climbing 25 -> 95 %
        t = 30.0 * i
        rh = 25 + 70 * i / 240
        rows.append({"time": t, "rh_ext": rh})
        s = guard.update(reading(t, 8.0, rh, heater=guard.on), rows)
        if s is not None:
            switches.append((t, s, rh))
    # taken over at 60 %, but the inside (11 C) is well above the dew point
    # (0.6 C) + 6 C: the heater only comes on once that reaches it, above 80 %
    assert switches[0][1] == "off" and cfg.dew_rh <= switches[0][2] < cfg.dew_rh + 1
    assert switches[1][1] == "on" and switches[1][2] > 80
    assert ">= 60 %" in guard.reason
    # now drying out: hands back once it has been below 60 % for 10 min
    t0 = rows[-1]["time"]
    for i in range(1, 240):
        t = t0 + 30.0 * i
        rh = max(30.0, 95 - i)
        rows.append({"time": t, "rh_ext": rh})
        s = guard.update(reading(t, 8.0, rh, heater=guard.on), rows)
        if s is not None:
            switches.append((t, s, rh))
    assert [s for _, s, _ in switches] == ["off", "on", "off", "auto"]
    assert switches[-1][2] <= cfg.dew_rh - 20   # 20 readings below 60 %


def test_cold_dome_near_dew_point_heats():
    guard = DewGuard(BoardConfig())
    # steady 55 %, but the ring has radiated down to within 4 C of the dew point
    data = reading(0.0, 5.0, 55.0, hot=-0.5, inside=-3.0)
    assert guard.update(data, []) == "on"
    assert "heater ring" in guard.reason
    # with the inside already 6 C above the dew point it takes the heater but holds it off
    guard = DewGuard(BoardConfig())
    assert guard.update(reading(0.0, 5.0, 55.0, hot=-0.5), []) == "off"
    assert guard.state == "manual"


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
    assert guard.update(reading(120.0, 8.0, 76.0, hot=12.0, inside=2.0), []) is None
    assert guard.reason == ("inside 10.0 C, target 10.0 C (dew point 4.0 C) "
                            "(outside humidity 76 % >= 60 %)")
    assert guard.update(reading(150.0, 8.0, 55.0, hot=12.0, inside=2.0), []) == "off"
    assert guard.reason.endswith("(outside humidity 55 %)")


def test_regulates_inside_above_dew_point_and_freezing():
    guard = DewGuard(BoardConfig())
    # 4 C, 92 %: dew point 2.8 C, so hold the inside at 8.8 C
    assert guard.update(reading(0.0, 4.0, 92.0, inside=3.0), []) == "on"
    assert "target 8.8 C (dew point 2.8 C)" in guard.reason
    assert guard.update(reading(30.0, 4.0, 92.0, inside=5.2, heater=True), []) is None  # in band
    assert guard.update(reading(60.0, 4.0, 92.0, inside=5.4, heater=True), []) == "off"
    assert guard.state == "manual"          # forced off, not handed to the board
    assert guard.update(reading(90.0, 4.0, 92.0, inside=4.4), []) is None
    assert guard.update(reading(120.0, 4.0, 92.0, inside=4.2), []) == "on"
    # a cold, dry winter night: dew point -25 C, but keep the inside above 1 C
    guard = DewGuard(BoardConfig())
    assert guard.update(reading(0.0, -10.0, 30.0, inside=3.0), []) == "on"
    assert guard.reason.startswith("outside -10.0 C < 1 C; inside -7.0 C, target 1.0 C")
    assert guard.update(reading(30.0, -10.0, 30.0, inside=11.6, heater=True), []) == "off"
    # it keeps the heater while it is freezing outside, however dry
    for i in range(2, 200):
        assert guard.update(reading(30.0 * i, -10.0, 30.0, inside=11.0), []) is None
    assert guard.state == "manual"


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
    """10-26 11:28 UT: one low reading among higher ones handed the heater back."""
    guard = DewGuard(BoardConfig())
    rows = []

    def feed(t, rh):
        rows.append({"time": t, "rh_ext": rh})
        return guard.update(reading(t, 6.0, rh, heater=guard.on), rows)

    assert feed(0.0, 70.0) is not None
    t = 0.0
    while t < 3600:                          # past the hold, humidity ~61 %
        t += 30.0
        feed(t, 61.0)
    assert feed(t + 30, 55.0) != "auto"      # one dip
    assert feed(t + 60, 61.0) != "auto"
    assert guard.state == "manual"
    handed = None
    for i in range(3, 40):                   # properly dry
        if feed(t + 30.0 * i, 55.0) == "auto":
            handed = 30.0 * i - 60
            break
    assert handed is not None and handed > 600   # 10 min after the last high reading


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
    assert guard.update(reading(0.0, 8.0, 30.0, inside=-8.0), [], fog=True) == "on"
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
