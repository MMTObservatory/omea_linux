"""Wall clock, and a compressed simulation clock for dry runs."""

import threading
import time
from datetime import datetime, timedelta, timezone


class Clock:
    """Real time. ``sleep`` returns True early if ``event`` gets set."""

    speed = 1.0

    def now(self):
        return datetime.now(timezone.utc)

    def sleep(self, seconds, event=None):
        if seconds <= 0:
            return bool(event and event.is_set())
        if event is None:
            time.sleep(seconds)
            return False
        return event.wait(seconds)


class SimClock(Clock):
    """Starts at ``start`` (UTC) and runs ``speed`` times faster than real time."""

    def __init__(self, start=None, speed=1.0):
        self.start = start or datetime.now(timezone.utc)
        self.speed = float(speed)
        self._t0 = time.monotonic()
        self._lock = threading.Lock()

    def now(self):
        with self._lock:
            elapsed = time.monotonic() - self._t0
        return self.start + timedelta(seconds=elapsed * self.speed)

    def sleep(self, seconds, event=None):
        return super().sleep(seconds / self.speed, event)
