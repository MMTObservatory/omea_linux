"""
The Alcor sensor board on the OMEA's RS232 link (115200 8N1).

It reports outdoor and in-camera temperature/humidity, runs the dome heater
(autonomously, from thresholds it stores), and drives the lens focus and iris.
The protocol follows Alcor's own Linux test program (Lazarus ``envi/Main.pas``,
firmware 2.x, and ``serial_weather.c``):

    request:  0x1D  n  cmd [args...]  checksum      n = len(cmd + args) + 1
    reply:    0x1F  n  data...        checksum      n = len(data) + 1

The checksum is the low byte of the sum of every preceding byte of the frame.
Sensor values are 16-bit big-endian sums of 16 10-bit ADC samples of a 0-5 V
input: temperature = (V - 0.5) * 100, RH = (V / 5 - offset) * factor.
"""

import logging
import math
import threading
import time
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

START = 0x1D
REPLY = 0x1F

REQ_VERSION = 0x9C
REQ_GET_DATA = 0x9D
REQ_SET_CONFIG = 0x9E
REQ_GET_CONFIG = 0x9F
REQ_ENABLE_HEATER = 0xA0
GET_LENS_INFO = 0xA3
SET_IRIS = 0xA4
SET_FOCUS = 0xA5
REQ_GET_DATAEXT = 0xA6

CAMERA_TYPES = {0x02: "IS", 0x03: "Moravian", 0x05: "EUDA 383L", 0x06: "Seeing monitor",
                0x07: "IS Pises", 0x08: "EUDA G2 8300", 0x09: "EUDA Ascent",
                0x10: "MiniCyclop", 0x11: "OMEA ext", 0x13: "IS ext v2",
                0x14: "OMEA 2", 0x15: "OMEA 8"}

# Lens iris: f-ratio -> raw code (Main.pas FD_Ratio / FD_Raw)
FRATIOS = {2.8: 0, 3.2: 4, 3.5: 8, 4.0: 12, 4.5: 15, 5.0: 18, 5.6: 20, 6.3: 23,
           7.1: 26, 8.0: 28, 9.0: 31, 10.0: 34, 11.0: 36, 13.0: 39, 14.0: 42,
           16.0: 44, 18.0: 47, 20.0: 50, 22.0: 52}

FOCUS_HOME_STEPS = -2500    # drives the focus ring onto its mechanical stop

ADC_FULL = 16 * 1024        # 16 summed 10-bit samples
VREF = 5.0


class BoardError(RuntimeError):
    pass


def checksum(data):
    return sum(data) & 0xFF


def encode(payload):
    frame = bytes([START, len(payload) + 1]) + bytes(payload)
    return frame + bytes([checksum(frame)])


def decode(frame):
    """Validate a reply frame and return its data bytes."""
    if len(frame) < 3 or frame[0] != REPLY:
        raise BoardError(f"bad reply header {frame[:2].hex()}")
    if frame[1] != len(frame) - 2:
        raise BoardError(f"reply length {frame[1]} != {len(frame) - 2}")
    if checksum(frame[:-1]) != frame[-1]:
        raise BoardError("reply checksum mismatch")
    return frame[2:-1]


def adu_to_temp(value):
    return (value / ADC_FULL * VREF - 0.5) * 100.0


def temp_to_adu(temp):
    return round(((temp / 100.0) + 0.5) / VREF * ADC_FULL)


def adu_to_rh(value, factor, offset):
    # the sensor reads past 100 % when saturated; Skywatch clamped it
    return min(max((value / ADC_FULL - offset) * factor, 0.0), 100.0)


def rh_to_adu(rh, factor, offset):
    return round((rh / factor + offset) * ADC_FULL)


def dew_point(temp, rh):
    """Alcor's dew-point approximation (utilities.pas ComputeDewPoint)."""
    if not (rh > 0):
        return float("nan")
    return (rh / 100.0) ** 0.125 * (112 + 0.9 * temp) + 0.1 * temp - 112


@dataclass
class BoardData:
    time: float                     # unix time of the reading
    temp_ext: float                 # degC, outdoor probe
    rh_ext: float                   # %
    temp_int: float                 # degC, camera interior
    rh_int: float                   # %
    temp_hot: float                 # degC, dome heating ring
    heater_on: bool
    auto_heat: bool
    heater_power: int               # %, the stored setting (also reported when off)
    dew_point: float

    def as_dict(self):
        return asdict(self)


def _u16(data, i):
    return (data[i] << 8) | data[i + 1]


def parse_data(data, rh_factor, rh_offset, temp_gain=1.0, temp_offset=0.0, now=None):
    """Decode a REQ_GET_DATA reply (OMEA firmware 2.x, no external weather module)."""
    if len(data) < 17:
        raise BoardError(f"GET_DATA reply too short ({len(data)} bytes)")
    temp_ext = adu_to_temp(_u16(data, 0)) * temp_gain + temp_offset
    rh_ext = adu_to_rh(_u16(data, 2), rh_factor, rh_offset)
    return BoardData(
        time=time.time() if now is None else now,
        temp_ext=temp_ext,
        rh_ext=rh_ext,
        temp_int=adu_to_temp(_u16(data, 4)),
        rh_int=adu_to_rh(_u16(data, 13), rh_factor, rh_offset),
        temp_hot=adu_to_temp(_u16(data, 15)),
        heater_on=data[6] > 0,
        auto_heat=data[7] > 0,
        heater_power=data[8],
        dew_point=dew_point(temp_ext, rh_ext),
    )


class AlcorBoard:
    """Request/reply access to the board over a serial port (or a fake one)."""

    def __init__(self, cfg, port=None):
        self.cfg = cfg
        if port is None:
            import serial
            # exclusive: a second program (the CLI while the service runs) would
            # read the other's replies
            try:
                port = serial.Serial(cfg.port, 115200, bytesize=8, parity="N",
                                     stopbits=1, timeout=0.25, exclusive=True)
            except serial.SerialException as err:
                if "lock" in str(err):
                    raise BoardError(f"{cfg.port} is in use, probably by the omea service "
                                     "(systemctl --user stop omea)") from err
                raise
        self.port = port
        self.lock = threading.Lock()
        self.firmware = None
        self.camera_type = None
        self.iris = None
        self.focus = None

    def close(self):
        try:
            self.port.close()
        except Exception:                                    # noqa: BLE001
            pass

    def _read(self, n):
        data = self.port.read(n)
        if len(data) != n:
            raise BoardError(f"timeout: read {len(data)} of {n} bytes")
        return data

    def transact(self, payload, retries=2):
        """Send one command and return the reply's data bytes."""
        frame = encode(payload)
        with self.lock:
            for attempt in range(retries + 1):
                try:
                    self.port.reset_input_buffer()
                    self.port.write(frame)
                    head = self._read(2)
                    reply = head + self._read(head[1])
                    return decode(reply)
                except BoardError as err:
                    if attempt == retries:
                        raise BoardError(f"command 0x{payload[0]:02X}: {err}") from None
                    time.sleep(0.1)

    # -- commands ------------------------------------------------------------

    def version(self):
        data = self.transact([REQ_VERSION])
        self.firmware = f"{data[0]}.{data[1]}"
        self.camera_type = data[2] if len(data) > 2 else None
        return self.firmware, CAMERA_TYPES.get(self.camera_type, f"0x{self.camera_type or 0:02X}")

    def read(self):
        data = self.transact([REQ_GET_DATA])
        c = self.cfg
        return parse_data(data, c.rh_factor, c.rh_offset, c.temp_ext_gain, c.temp_ext_offset)

    def get_config(self):
        """The heater thresholds: (temperature degC, RH %)."""
        data = self.transact([REQ_GET_CONFIG])
        return (adu_to_temp(_u16(data, 0)),
                adu_to_rh(_u16(data, 2), self.cfg.rh_factor, self.cfg.rh_offset))

    def set_config(self, temp_threshold, rh_threshold, auto, power):
        """Store heater thresholds, automatic mode and power (%) in the board."""
        t = temp_to_adu(temp_threshold)
        h = rh_to_adu(rh_threshold, self.cfg.rh_factor, self.cfg.rh_offset)
        data = self.transact([REQ_SET_CONFIG, t >> 8, t & 0xFF, h >> 8, h & 0xFF,
                              int(bool(auto)), int(power)])
        if not data or data[0] != 0xCA:
            raise BoardError("SET_CONFIG not acknowledged")

    def set_heater(self, on):
        """Force the dome heater on/off (only meaningful with automatic mode off)."""
        data = self.transact([REQ_ENABLE_HEATER, int(bool(on))])
        return bool(data and data[0])

    def set_iris(self, fratio):
        """Open fully, then close to ``fratio`` (one of :data:`FRATIOS`)."""
        key = min(FRATIOS, key=lambda f: abs(f - fratio))
        if abs(key - fratio) > 0.05:
            raise ValueError(f"f/{fratio} is not an iris stop; choose from {sorted(FRATIOS)}")
        self.transact([SET_IRIS, 0xFF, 0])
        time.sleep(0.333)
        self.transact([SET_IRIS, 0x77, FRATIOS[key]])
        self.iris = key
        log.info("iris set to f/%.1f", key)

    def move_focus(self, steps):
        """Move the focus ring by ``steps`` encoder steps (signed, relative)."""
        s = int(steps) & 0xFFFF
        self.transact([SET_FOCUS, 0x14, s >> 8, s & 0xFF])

    def focus_to(self, position):
        """Home the focus ring on its stop, then move to ``position`` steps."""
        self.move_focus(FOCUS_HOME_STEPS)
        time.sleep(1.0)
        self.move_focus(position)
        self.focus = int(position)
        log.info("focus homed and set to %d steps", position)


class SimBoard:
    """A stand-in that answers like the firmware, for dry runs and tests."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.firmware = "2.20"
        self.camera_type = 0x15
        self.iris = None
        self.focus = None
        self.heater = False
        self.thresholds = (7.0, 90.0)
        self.auto = True
        self.power = 40

    def close(self):
        pass

    def version(self):
        return self.firmware, CAMERA_TYPES[self.camera_type]

    def read(self):
        t = time.time()
        temp = 12 + 6 * math.sin(t / 43200 * math.pi)
        rh = 45 + 20 * math.cos(t / 43200 * math.pi)
        heater = (temp < self.thresholds[0] and rh > self.thresholds[1]) if self.auto \
            else self.heater
        return BoardData(time=t, temp_ext=temp, rh_ext=rh, temp_int=temp + 5,
                         rh_int=15.0, temp_hot=temp + (8 if heater else 1),
                         heater_on=heater, auto_heat=self.auto, heater_power=self.power,
                         dew_point=dew_point(temp, rh))

    def get_config(self):
        return self.thresholds

    def set_config(self, temp_threshold, rh_threshold, auto, power):
        self.thresholds = (temp_threshold, rh_threshold)
        self.auto, self.power = bool(auto), int(power)

    def set_heater(self, on):
        self.heater = bool(on)
        return self.heater

    def set_iris(self, fratio):
        self.iris = fratio
        log.info("(sim) iris set to f/%.1f", fratio)

    def focus_to(self, position):
        self.focus = position
        log.info("(sim) focus set to %d", position)


def open_board(cfg):
    if not cfg.enabled:
        return None
    if cfg.driver == "sim":
        return SimBoard(cfg)
    board = AlcorBoard(cfg)
    fw, kind = board.version()
    log.info("sensor board firmware %s, camera type %s", fw, kind)
    return board
