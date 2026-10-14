"""
Configuration, read from a TOML file.

Every default reproduces what the Windows Skywatch installation at the MMTO was
doing (its settings live in the registry under HKCU\\Software\\SkyWatch; see
docs/skywatch.md), so an empty config file gives Skywatch-compatible output.
"""

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SiteConfig:
    name: str = "MMTO"
    latitude: float = 31.6867       # deg; Skywatch registry: 0.55303666 rad
    longitude: float = -110.8842    # deg east; registry: 1.93529380 rad west
    elevation: float = 2600.0       # m
    timezone: str = "America/Phoenix"   # MST all year; file names are local time


@dataclass
class CameraConfig:
    driver: str = "zwo"             # "zwo" or "sim"
    sdk_library: str = ""           # libASICamera2.so; "" = <package>/../lib
    name: str = ""                  # substring of the camera name; "" = first camera
    offset: int = 30                # ASI_OFFSET, as Skywatch (OFFSET_E = 30)
    usb_bandwidth: int = 50         # ASI_BANDWIDTHOVERLOAD, as Skywatch
    readout_timeout: float = 30.0   # s beyond the exposure before giving up
    cooling: bool = True
    target_temperature: float = -5.0     # degC (Skywatch RequiredCCDTemperature)
    cooling_sun_altitude: float = 0.0    # cool only while the Sun is below this
    cooling_ramp: float = 2.0            # degC per minute towards the set point
    max_cooler_power: float = 95.0       # % before the set point is backed off
    # simulator: a directory of Skywatch frames to replay (``omea run
    # --simulate``; time compression is ``--speed``)
    sim_archive: str = ""


@dataclass
class ImageConfig:
    """How a RAW16 sensor frame becomes the saved (3, 1411, 1422) cube."""
    crop_x: int = 700               # 0-based; Skywatch X1 = 701
    crop_y: int = 0
    crop_width: int = 2844
    crop_height: int = 2822
    bayer: str = "RGGB"             # pattern at sensor (0, 0)
    mirror_x: bool = True           # Skywatch MX = Yes
    mirror_y: bool = False          # Skywatch MY = No
    adu_scale: float = 1.0          # output = adu_scale * 2x2 mean of RAW16
    clip: int = 32767               # int16 ceiling, as Skywatch


@dataclass
class ExposureConfig:
    min_exposure: float = 32e-6     # s
    max_exposure: float = 20.0      # s (Skywatch MaxExpo)
    min_gain: int = 0
    max_gain: int = 250             # Skywatch MaxGain
    night_gain: int = 250           # fixed at night so frames stay calibrated
    day_target: float = 0.15        # median sky, fraction of (clip - bias)
    night_target: float = 0.25
    # metering circle in output-frame pixels (Skywatch: cx=696 cy=698 r=596)
    meter_x: float = 696.0
    meter_y: float = 698.0
    meter_radius: float = 596.0
    interval_day: float = 15.0      # s between exposure starts
    interval_night: float = 35.0
    start_exposure: float = 0.01    # first exposure after start-up (s)


@dataclass
class ModeConfig:
    """Day/night switching."""
    night_sun_altitude: float = -6.0     # Skywatch SunHeightNightDay
    night_fratio: float = 2.8
    day_fratio: float = 16.0
    focus_position: int = 1320           # encoder steps after homing


@dataclass
class BoardConfig:
    """The Alcor sensor/heater/lens board on the RS232 link."""
    enabled: bool = True
    driver: str = "serial"          # "serial" or "sim"
    port: str = "/dev/ttyUSB0"
    poll_interval: float = 30.0     # s
    rh_factor: float = 157.232704402516
    rh_offset: float = 0.1515
    temp_ext_gain: float = 1.0
    temp_ext_offset: float = 0.0
    # dome heater, written to the board at start-up (it keeps running on them
    # without any software): "auto" heats below heater_temp and above heater_rh
    heater_mode: str = "auto"       # "auto", "on" or "off"
    heater_temp: float = 10.0       # degC, outside temperature
    heater_rh: float = 80.0         # %, outside humidity
    heater_power: int = 50          # %


@dataclass
class OutputConfig:
    data_dir: str = "/mnt/d/skycam"        # <data_dir>/<YYYY-MM-DD>/...
    write_fits: bool = True
    write_jpeg: bool = True
    write_unwrap: bool = True
    jpeg_quality: int = 98
    latest_dir: str = "/var/www/html/skycam"   # ImageLastFTP_AllSKY*.jpg
    latest_interval: float = 30.0
    weather_csv: str = "/mnt/c/Users/skycam/Documents/skywatch/Heater/DataOut_Weather.csv"
    telescope_file: str = "/mnt/d/skycam/mmt_position.txt"
    unwrap_scale: float = 1.5      # Skywatch Unwrap Scale = 150 (%)


@dataclass
class ScienceConfig:
    """Per-frame photometry and sky brightness through skycam_utils."""
    enabled: bool = True
    workers: int = 2
    sun_alt_max: float = -12.0
    photometry: bool = True
    # cloud-extinction map: a running average of the newest frames, rebuilt
    # after every frame (interval 0) or at most every extinction_interval s
    extinction_frames: int = 10
    extinction_interval: float = 0.0
    extinction_file: str = "/mnt/d/skycam/alcor_extinction_latest.fits"
    # over this share of stars lost (fog), publish extinction_opaque instead of
    # a median of the few left
    extinction_opaque_fraction: float = 0.9
    extinction_opaque: float = 10.0     # mag


@dataclass
class RedisConfig:
    enabled: bool = True
    host: str = "redis.mmto.arizona.edu"
    port: int = 6379
    password_env: str = "REDISPW"


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8081


@dataclass
class Config:
    site: SiteConfig = field(default_factory=SiteConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    exposure: ExposureConfig = field(default_factory=ExposureConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    board: BoardConfig = field(default_factory=BoardConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    science: ScienceConfig = field(default_factory=ScienceConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    web: WebConfig = field(default_factory=WebConfig)


def _fill(cls, values, where):
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(values) - set(known)
    if unknown:
        raise ValueError(f"unknown key(s) in [{where}]: {', '.join(sorted(unknown))}")
    kwargs = {}
    for name, value in values.items():
        default = getattr(cls(), name)
        if isinstance(default, float) and isinstance(value, int):
            value = float(value)
        elif type(default) is not type(value):
            raise ValueError(f"[{where}] {name} should be {type(default).__name__}, "
                             f"got {type(value).__name__}")
        kwargs[name] = value
    return cls(**kwargs)


def load_config(path=None):
    """Read ``path`` (TOML); missing sections and keys take their defaults."""
    if path is None:
        return Config()
    with open(Path(path), "rb") as f:
        raw = tomllib.load(f)
    sections = {f.name: f.type for f in dataclasses.fields(Config)}
    unknown = set(raw) - set(sections)
    if unknown:
        raise ValueError(f"unknown section(s): {', '.join(sorted(unknown))}")
    kwargs = {}
    for name in sections:
        cls = type(getattr(Config(), name))
        kwargs[name] = _fill(cls, raw.get(name, {}), name)
    return Config(**kwargs)
