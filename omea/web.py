"""
Read-only status pages: the latest images, camera/sensor state, the night's
plots and the log. Everything is controlled from the CLI and config file; the
web interface never changes anything.
"""

import json
import logging
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

log = logging.getLogger(__name__)


def _clean(value):
    """JSON has no NaN: send null instead."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def make_handler(status):
    page = (files("omea") / "static" / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, ctype, cache="no-store"):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(json.dumps(_clean(obj)).encode(), "application/json")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(status.snapshot())
            elif path == "/api/history":
                with status.lock:
                    frames = list(status.history)
                    weather = list(status.weather)
                    extinction = list(status.extinction)
                self._json({"frames": frames, "weather": weather, "extinction": extinction})
            elif path == "/api/log":
                with status.lock:
                    self._json(list(status.log))
            elif path.startswith("/image/") and path.endswith(".jpg"):
                name = path[len("/image/"):-4]
                with status.lock:
                    body = status.images.get(name)
                if body is None:
                    self.send_error(404)
                else:
                    self._send(body, "image/jpeg")
            else:
                self.send_error(404)

    return Handler


def serve(status, host, port):
    """Start the server on a daemon thread; returns the server."""
    server = ThreadingHTTPServer((host, port), make_handler(status))
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    log.info("web interface on http://%s:%d/", host, port)
    return server
