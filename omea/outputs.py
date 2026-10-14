"""
Things the rest of the MMTO already reads: redis keys, the weather CSV that
``tcs_logger.py`` parses, the latest-image files the web server serves, and the
telescope-position file ``mmt_position.py`` writes.
"""

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .fitsout import write_atomic

log = logging.getLogger(__name__)

WEATHER_HEADER = ("Date;Date as string;Ext. temp \xb0C;Int. temp \xb0C;Hot temp \xb0C;"
                  "Temp CCD \xb0C;RH ext as %;RH int as %;Active heater;Press as mb;"
                  "Windspeed km/h;Wind dir as \xb0\r\n")
_DELPHI_EPOCH = datetime(1899, 12, 30)


def delphi_date(local):
    """Days since 1899-12-30 local time (Delphi TDateTime), as Skywatch writes."""
    return (local.replace(tzinfo=None) - _DELPHI_EPOCH) / timedelta(days=1)


def weather_line(local, board, ccd_temp):
    """One ``DataOut_Weather.csv`` line, byte-compatible with Skywatch."""
    stamp = (f"{local.month}/{local.day}/{local.year} "
             f"{(local.hour % 12) or 12}:{local.minute:02d}:{local.second:02d} "
             f"{'AM' if local.hour < 12 else 'PM'}")

    def f1(v):
        return f"{v:.1f}" if v is not None and math.isfinite(v) else "-1.0"

    return (f"{delphi_date(local):.5f};{stamp};{f1(board.temp_ext)};{f1(board.temp_int)};"
            f"{f1(board.temp_hot)};{f1(ccd_temp)};{board.rh_ext:.0f};{board.rh_int:.0f};"
            f"{'TRUE' if board.heater_on else 'FALSE'};-1.00;-1.0;-1;\r\n")


def append_weather(path, line):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="latin-1", newline="") as f:
        if new:
            f.write(WEATHER_HEADER)
        f.write(line)


def read_weather(path, since, tz, max_bytes=1 << 20):
    """
    Board readings from the end of a ``DataOut_Weather.csv`` taken at or after
    ``since`` (unix time), as the web history rows. Only the last ``max_bytes``
    are read: the file grows for years. ``tz`` is the local zone of its dates.
    """
    from .board import dew_point
    rows = []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            lines = f.read().decode("latin-1").splitlines()[1:]   # first may be partial
    except OSError:
        return rows
    for line in lines:
        p = line.split(";")
        try:
            local = _DELPHI_EPOCH + timedelta(days=float(p[0]))
            t = local.replace(tzinfo=tz).timestamp()
            temp_ext, temp_int, temp_hot, ccd = (float(v) for v in p[2:6])
            rh_ext, rh_int = float(p[6]), float(p[7])
        except (ValueError, IndexError):
            continue
        if t < since:
            continue
        rows.append({"time": t, "temp_ext": temp_ext, "rh_ext": rh_ext,
                     "temp_int": temp_int, "rh_int": rh_int, "temp_hot": temp_hot,
                     "dew_point": dew_point(temp_ext, rh_ext), "heater_on": p[8] == "TRUE",
                     "ccd_temp": ccd if ccd != -1.0 else float("nan")})
    return rows


def append_csv(path, fields, row):
    """Append ``row`` to a CSV, writing the header when the file is new."""
    import csv
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: (f"{v:.5g}" if isinstance(v, float) else v) for k, v in row.items()})


def read_csv(path):
    """Rows of a CSV written by :func:`append_csv`, numbers as floats ('' -> NaN)."""
    import csv
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []

    def num(v):
        if v == "":
            return float("nan")
        try:
            return float(v)
        except ValueError:
            return v

    return [{k: num(v) for k, v in r.items()} for r in rows]


def save_jpeg(img, path, quality=95):
    """Write a PIL image as JPEG atomically (the web server may be reading)."""
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    write_atomic(path, buf.getvalue())


def read_telescope(path, max_age=10.0):
    """
    The telescope pointing from Skywatch's telescope-position file: line 1 is
    the (Delphi) date, then RA and Dec J2000 in radians. Returns ``(ra_deg,
    dec_deg, fresh)`` or None. ``fresh`` follows Skywatch: under ``max_age`` s.
    """
    try:
        lines = Path(path).read_text().split()
        stamp, ra, dec = float(lines[0]), float(lines[1]), float(lines[2])
    except (OSError, ValueError, IndexError):
        return None
    # mmt_position.py writes JD - 2415020 + 1.5 (UT), i.e. a Delphi date in UT
    when = _DELPHI_EPOCH.replace(tzinfo=timezone.utc) + timedelta(days=stamp)
    age = (datetime.now(timezone.utc) - when).total_seconds()
    return math.degrees(ra), math.degrees(dec), abs(age) < max_age


class Redis:
    """Publishes ``key -> value`` with SET + PUBLISH, the MMTO convention."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None
        if not cfg.enabled:
            return
        try:
            import redis
            self.client = redis.StrictRedis(host=cfg.host, port=cfg.port,
                                            password=os.environ.get(cfg.password_env) or None,
                                            db=0, socket_timeout=5)
        except Exception as err:                              # noqa: BLE001
            log.warning("redis disabled: %s", err)

    def publish(self, values):
        if self.client is None:
            return
        try:
            pipe = self.client.pipeline()
            for key, value in values.items():
                if isinstance(value, float) and not math.isfinite(value):
                    value = "nan"
                elif isinstance(value, (dict, list)):
                    value = json.dumps(value)
                pipe.set(key, value)
                pipe.publish(key, value)
            pipe.execute()
        except Exception as err:                              # noqa: BLE001
            log.warning("redis publish failed: %s", err)
