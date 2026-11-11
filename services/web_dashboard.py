"""Built-in web dashboard: ek web page + /api/logs JSON endpoint.

Bot ke andar hi ek chhota HTTP server (Python stdlib, koi extra pip
dependency nahi) chalta hai jo ``web/dashboard.html`` serve karta hai.
Dashboard page har 5 second me ``/api/logs`` ko poll karke latest file
uploads aur network errors dikhata hai.

Env config (config.py):
- ``WEB_DASHBOARD_ENABLED``  (default: true)
- ``WEB_DASHBOARD_PORT``     (default: 8080)
- ``WEB_DASHBOARD_TOKEN``    (optional; set karo to / aur /api/* token maangte hain)

Server apne daemon thread me chalta hai — start/bind fail hone par sirf
warning log hoti hai, bot kabhi crash nahi hota.
"""
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
from services import activity_log

log = logging.getLogger("dashboard")

_PAGE_PATH = Path(__file__).resolve().parent.parent / "web" / "dashboard.html"
_MAX_BODY_HINT = 64 * 1024

_server: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None


class _DashboardHandler(BaseHTTPRequestHandler):
    server_version = "TeraFastDashboard/2.3"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- helpers
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        import json

        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        token = config.WEB_DASHBOARD_TOKEN
        if not token:
            return True
        header = self.headers.get("X-Dashboard-Token", "").strip()
        query = parse_qs(urlparse(self.path).query)
        supplied = (query.get("token", [""])[0] or "").strip()
        return supplied == token or header == token

    # ------------------------------------------------------------- routing
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-Dashboard-Token")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001 - ek bad request server nahi gira sakta
            log.exception("Dashboard request failed: %s", self.path)
            try:
                self._send_json({"ok": False, "error": "internal error"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._authorized():
            self._send_json({"ok": False, "error": "invalid or missing token"}, 401)
            return

        if path in ("/", "/index.html", "/dashboard"):
            self._serve_page()
        elif path == "/api/logs":
            params = parse_qs(parsed.query)
            event_type = (params.get("type", [None])[0] or None)
            if event_type not in (None, *activity_log.EVENT_TYPES):
                event_type = None
            try:
                limit = int(params.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            try:
                after = int(params.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._send_json(
                activity_log.api_payload(event_type, limit, after)
            )
        elif path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "service": "terafast-dashboard",
                    "version": "2.3",
                    "stats": activity_log.stats(),
                }
            )
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    def _serve_page(self) -> None:
        try:
            html = _PAGE_PATH.read_bytes()
        except OSError:
            html = (
                b"<!doctype html><html><body><h1>TeraFast Dashboard</h1>"
                b"<p>web/dashboard.html missing hai.</p></body></html>"
            )
        self._send(200, html, "text/html; charset=utf-8")


def start() -> bool:
    """Start the dashboard server in a background thread. Idempotent."""
    global _server, _thread
    if not config.WEB_DASHBOARD_ENABLED:
        log.info("Web dashboard disabled (WEB_DASHBOARD_ENABLED=false)")
        return False
    if _thread is not None and _thread.is_alive():
        return True

    try:
        _server = ThreadingHTTPServer(("0.0.0.0", config.WEB_DASHBOARD_PORT), _DashboardHandler)
    except OSError as exc:
        log.warning(
            "Web dashboard start nahi hua (port %s busy ya unavailable): %r",
            config.WEB_DASHBOARD_PORT, exc,
        )
        return False

    _server.daemon_threads = True
    _thread = threading.Thread(
        target=_server.serve_forever,
        name="web-dashboard",
        daemon=True,
    )
    _thread.start()
    log.info(
        "Web dashboard live: http://0.0.0.0:%s/ (poll /api/logs for activity)",
        config.WEB_DASHBOARD_PORT,
    )
    return True


def stop() -> None:
    """Best-effort shutdown (tests ke liye)."""
    global _server, _thread
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    _thread = None
