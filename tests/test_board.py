import pytest
import numpy as np

from omea import board
from omea.config import BoardConfig


class FakePort:
    """Answers board commands from a table, recording what was sent."""

    def __init__(self, replies):
        self.replies = replies
        self.sent = []
        self.buf = b""

    def reset_input_buffer(self):
        self.buf = b""

    def write(self, frame):
        self.sent.append(bytes(frame))
        data = self.replies[frame[2]]
        reply = bytes([board.REPLY, len(data) + 1]) + bytes(data)
        self.buf = reply + bytes([board.checksum(reply)])

    def read(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def close(self):
        pass


def test_encode_matches_alcor_sample():
    # serial_weather.c: version request is 1D 02 9C checksum(1D+02+9C)
    assert board.encode([board.REQ_VERSION]) == bytes([0x1D, 0x02, 0x9C, (0x1D + 0x02 + 0x9C) & 0xFF])


def test_decode_rejects_bad_checksum():
    frame = bytes([board.REPLY, 3, 1, 2])
    frame += bytes([board.checksum(frame)])
    assert board.decode(frame) == bytes([1, 2])
    with pytest.raises(board.BoardError):
        board.decode(frame[:-1] + bytes([frame[-1] ^ 1]))


def test_conversions_round_trip():
    for t in (-20.0, 0.0, 7.0, 35.5):
        assert board.adu_to_temp(board.temp_to_adu(t)) == pytest.approx(t, abs=0.05)
    f, o = 157.232704402516, 0.1515
    for rh in (5.0, 50.0, 90.0):
        assert board.adu_to_rh(board.rh_to_adu(rh, f, o), f, o) == pytest.approx(rh, abs=0.1)


def _word(v):
    return [v >> 8, v & 0xFF]


def test_read_and_iris():
    cfg = BoardConfig()
    t_ext = board.temp_to_adu(11.3)
    rh_ext = board.rh_to_adu(88.0, cfg.rh_factor, cfg.rh_offset)
    data = (_word(t_ext) + _word(rh_ext) + _word(board.temp_to_adu(16.7))
            + [0, 1, 50] + [0, 0, 0, 0] + _word(board.rh_to_adu(45.0, cfg.rh_factor, cfg.rh_offset))
            + _word(board.temp_to_adu(12.3)) + [0, 0, 0, 0])
    port = FakePort({board.REQ_GET_DATA: data, board.SET_IRIS: [1],
                     board.REQ_VERSION: [2, 20, 0x15]})
    b = board.AlcorBoard(cfg, port=port)
    assert b.version() == ("2.20", "OMEA 8")
    d = b.read()
    assert d.temp_ext == pytest.approx(11.3, abs=0.05)
    assert d.rh_ext == pytest.approx(88.0, abs=0.1)
    assert d.temp_int == pytest.approx(16.7, abs=0.05)
    assert d.rh_int == pytest.approx(45.0, abs=0.1)
    assert d.temp_hot == pytest.approx(12.3, abs=0.05)
    assert (d.heater_on, d.auto_heat, d.heater_power) == (False, True, 50)
    b.set_iris(16)
    # fully open first, then close to f/16's code (44)
    assert port.sent[-2][2:5] == bytes([board.SET_IRIS, 0xFF, 0])
    assert port.sent[-1][2:5] == bytes([board.SET_IRIS, 0x77, 44])
    with pytest.raises(ValueError):
        b.set_iris(12.0)


def test_focus_is_signed_big_endian():
    port = FakePort({board.SET_FOCUS: [1]})
    b = board.AlcorBoard(BoardConfig(), port=port)
    b.move_focus(-2500)
    assert port.sent[-1][2:6] == bytes([board.SET_FOCUS, 0x14]) + (-2500 & 0xFFFF).to_bytes(2, "big")


def test_dew_point():
    assert board.dew_point(20.0, 100.0) == pytest.approx(20.0, abs=0.01)
    assert board.dew_point(20.0, 50.0) == pytest.approx(9.3, abs=0.5)


def test_heater_settings_written_only_when_changed():
    from omea.config import Config
    from omea.monitor import Monitor
    cfg = Config()
    cfg.board.driver = "sim"
    mon = Monitor(cfg, publish=False)
    mon.board = board.open_board(cfg.board)
    calls = []
    real = mon.board.set_config
    mon.board.set_config = lambda *a: (calls.append(a), real(*a))
    mon._configure_heater()
    assert calls == [(10.0, 80.0, True, 50)]
    data = mon.board.read()
    assert data.auto_heat and data.heater_power == 50
    mon._configure_heater()                 # already set: no second write
    assert len(calls) == 1
    cfg.board.heater_mode = "on"
    mon._configure_heater()
    assert calls[-1][2] is False and mon.board.read().heater_on


def test_cooler_ramps_down_and_up_gently():
    from omea.camera import SimCamera
    from omea.config import Config
    from omea.monitor import Monitor
    cfg = Config()
    mon = Monitor(cfg, publish=False)
    cam = mon.camera = SimCamera(cfg.camera, cfg.image)
    cam._temp = 20.0
    t = [0.0]
    import omea.monitor as m
    real = m.time.monotonic
    m.time.monotonic = lambda: t[0]
    try:
        targets = []
        for _ in range(30):                              # night, one minute apart
            mon._manage_cooling(-20)
            targets.append(mon._cooler_setpoint)
            t[0] += 60
        steps = np.diff(targets)
        assert steps.min() >= -cfg.camera.cooling_ramp - 1e-9
        assert targets[-1] == cfg.camera.target_temperature
        # sunrise: warms at the ramp rate, then switches off when idle
        warm = []
        cam.cooler_power = lambda: 0.0 if mon._cooler_setpoint and mon._cooler_setpoint > 10 else 40.0
        for _ in range(30):
            mon._manage_cooling(+5)
            warm.append(mon._cooler_setpoint)
            t[0] += 60
            if mon._cooler_setpoint is None:
                break
        ramp = [w for w in warm if w is not None]
        assert np.diff(ramp).max() <= cfg.camera.cooling_ramp + 1e-9
        assert len(ramp) > 5 and warm[-1] is None and not cam._cooling
    finally:
        m.time.monotonic = real
