from omea import board
from omea.config import BoardConfig
from omea.heater import DewGuard, rh_trend


def reading(t, temp, rh, hot=None, heater=False):
    return board.BoardData(time=t, temp_ext=temp, rh_ext=rh, temp_int=temp + 3,
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
    assert switches and switches[0][1] is True
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
    assert [s for _, s, _ in switches] == [True, False]
    assert switches[1][2] < cfg.dew_rh - cfg.dew_hysteresis


def test_cold_dome_near_dew_point_heats():
    guard = DewGuard(BoardConfig())
    # steady 55 %, but the ring has radiated down to within 4 C of the dew point
    data = reading(0.0, 5.0, 55.0, hot=-0.5)
    assert guard.update(data, []) is True
    assert "heater ring" in guard.reason


def test_dry_steady_night_stays_off():
    guard = DewGuard(BoardConfig())
    rows = [{"time": 30.0 * i, "rh_ext": 30.0} for i in range(120)]
    for r in rows:
        assert guard.update(reading(r["time"], 8.0, 30.0), rows) is None


def test_monitor_forces_heater_and_restores_auto():
    from omea.config import Config
    from omea.monitor import Monitor
    cfg = Config()
    cfg.board.driver = "sim"
    mon = Monitor(cfg, publish=False)
    mon.board = board.open_board(cfg.board)
    mon._configure_heater()
    mon._force_heater(True, "test")
    assert not mon.board.auto and mon.board.read().heater_on
    mon._force_heater(False, "test")
    assert mon.board.auto and mon.board.thresholds == (10.0, 80.0)


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
    flags = [mon._update_fog(v)["allsky_dome_fog"]
             for v in (0.0, 0.5, 0.0, 0.6, 0.7, math.nan, 0.15, 0.05, 0.05)]
    assert flags == [0, 0, 0, 1, 1, 1, 1, 1, 0]     # median of 3, on 0.2 / off 0.1
    mon.status.board = {"rh_ext": 30.0}            # dry: zenith cloud, not dome fog
    assert [mon._update_fog(1.0)["allsky_dome_fog"] for _ in range(3)] == [0, 0, 0]
    guard = DewGuard(BoardConfig())
    assert guard.update(reading(0.0, 8.0, 30.0), [], fog=True) is True
    assert guard.reason == "fog on the dome"


def test_csv_keeps_an_older_header(tmp_path):
    from omea import outputs
    path = tmp_path / "x.csv"
    outputs.append_csv(path, ["a", "b"], {"a": 1, "b": 2})
    outputs.append_csv(path, ["a", "b", "c"], {"a": 3, "b": 4, "c": 5})
    assert path.read_text().splitlines() == ["a,b", "1,2", "3,4"]
