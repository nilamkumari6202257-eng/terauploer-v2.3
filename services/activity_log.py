"""In-process activity event store powering the web dashboard.

Har file upload aur network error yahan record hota hai. Built-in web
dashboard (services/web_dashboard.py) inhi events ko ``/api/logs`` par serve
karta hai, aur optional webhook forwarder unhe ek deployed TeraFast website
par push kar deta hai.

Design notes:
- ``record_event`` KABHI raise nahi karta — dashboard ki wajah se bot ka
  behaviour kabhi change nahi hoga.
- Ring buffer (max ``MAX_EVENTS``) — memory bounded.
- Network errors par ek chhota rate limiter hai taaki ek bure network phase
  me (jaise host down, saare 4 MiB parts fail) dashboard flood na ho.
- Webhook forwarder apne daemon thread me chalta hai, failures silent
  re-queue hote hain (max ``FORWARD_QUEUE_MAX`` tak).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import requests

import config

log = logging.getLogger("activity")

EVENT_TYPES = ("upload", "upload_failed", "network_error", "info")
MAX_EVENTS = 1000

# Webhook forwarder
FORWARD_INTERVAL_SEC = 3.0
FORWARD_TIMEOUT_SEC = 10.0
FORWARD_QUEUE_MAX = 2000

# Network-error rate limit: MAX_BURST events per BURST_WINDOW_SEC.
NETWORK_BURST_MAX = 6
NETWORK_BURST_WINDOW_SEC = 10.0

# Exceptions jo network error count hote hain (pyrogram import se bachne ke
# liye FloodWait class-name se match hota hai).
_NETWORK_MARKERS = (
    "timed out",
    "timeout",
    "connection",
    "reset",
    "unreachable",
    "name or service",
    "temporary failure",
    "network",
    "broken pipe",
    "refused",
    "ssl",
    "handshake",
    "floodwait",
    "too many requests",
    "server disconnected",
    "read error",
    "peer closed",
)

_lock = threading.Lock()
_events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
_counters: dict[str, int] = {t: 0 for t in EVENT_TYPES}
_next_id = 1
_last_event_at: str | None = None
_started_at = time.time()

# Rate limiter state
_net_times: deque[float] = deque(maxlen=NETWORK_BURST_MAX)
_net_suppressed = 0

# Forwarder state
_pending: deque[dict[str, Any]] = deque(maxlen=FORWARD_QUEUE_MAX)
_forwarder_lock = threading.Lock()
_forwarder_thread: threading.Thread | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_network_exception(exc: BaseException) -> bool:
    """Classify an exception as network-ish for the dashboard."""
    if isinstance(
        exc, (requests.Timeout, requests.ConnectionError, ConnectionError, TimeoutError)
    ):
        return True
    name = type(exc).__name__
    if name in ("FloodWait", "ReadTimeout", "ConnectTimeout", "ConnectionResetError"):
        return True
    if isinstance(exc, requests.RequestException):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _NETWORK_MARKERS)


def record_event(
    event_type: str,
    message: str,
    *,
    file_name: str | None = None,
    file_size: int | None = None,
    link: str | None = None,
    source: str | None = None,
    target: str | None = None,
    duration_sec: float | None = None,
    error: str | None = None,
) -> int | None:
    """Append one activity event. Returns the event id, or None on bad input."""
    global _next_id, _last_event_at, _net_suppressed

    if event_type not in EVENT_TYPES:
        return None
    message = str(message or "").strip()
    if not message:
        return None

    try:
        with _lock:
            now = time.monotonic()
            if event_type == "network_error":
                # Purane burst-window timestamps hatao.
                while _net_times and now - _net_times[0] > NETWORK_BURST_WINDOW_SEC:
                    _net_times.popleft()
                if len(_net_times) >= NETWORK_BURST_MAX:
                    _net_suppressed += 1
                    return None
                _net_times.append(now)
                if _net_suppressed:
                    message = f"{message} (+{_net_suppressed} more suppressed)"
                    _net_suppressed = 0

            event = {
                "id": _next_id,
                "type": event_type,
                "message": message[:500],
                "file_name": (file_name or None) and str(file_name)[:300],
                "file_size": int(file_size) if file_size else None,
                "link": (link or None) and str(link)[:1000],
                "source": (source or None) and str(source)[:60],
                "target": (target or None) and str(target)[:200],
                "duration_sec": round(float(duration_sec), 1)
                if duration_sec is not None
                else None,
                "error": (error or None) and str(error)[:1000],
                "created_at": _now_iso(),
            }
            _next_id += 1
            _events.append(event)
            _counters[event_type] += 1
            _last_event_at = event["created_at"]

            if config.WEB_DASHBOARD_WEBHOOK_URL:
                _pending.append(dict(event, type=event_type))
                _ensure_forwarder()
        return event["id"]
    except Exception:  # noqa: BLE001 - dashboard must never break the bot
        log.debug("activity event dropped", exc_info=True)
        return None


def record_upload(
    name: str,
    size: int,
    link: str,
    duration_sec: float,
    *,
    source: str = "direct",
    target: str | None = None,
) -> int | None:
    size_mb = size / (1024 * 1024) if size else 0
    return record_event(
        "upload",
        f"Uploaded {name} ({human_size(size)}) to TeraBox",
        file_name=name,
        file_size=size,
        link=link,
        source=source,
        target=target,
        duration_sec=duration_sec,
    )


def record_upload_failed(
    name: str | None,
    size: int | None,
    error: str,
    *,
    source: str = "direct",
    target: str | None = None,
    duration_sec: float | None = None,
) -> int | None:
    return record_event(
        "upload_failed",
        f"Upload failed: {name or 'file'}",
        file_name=name,
        file_size=size,
        source=source,
        target=target,
        duration_sec=duration_sec,
        error=str(error)[:1000],
    )


def record_network_error(
    message: str,
    error: BaseException | str | None = None,
    *,
    source: str | None = None,
    target: str | None = None,
    file_name: str | None = None,
    file_size: int | None = None,
) -> int | None:
    return record_event(
        "network_error",
        message,
        file_name=file_name,
        file_size=file_size,
        source=source,
        target=target,
        error=str(error)[:1000] if error else None,
    )


def record_failure(
    exc: BaseException,
    *,
    name: str | None = None,
    size: int | None = None,
    source: str = "direct",
    target: str | None = None,
    duration_sec: float | None = None,
) -> int | None:
    """Record a pipeline/upload failure, auto-classifying network errors."""
    if is_network_exception(exc):
        return record_network_error(
            f"Network error while uploading {name or 'file'}: {exc}",
            error=exc,
            source=source,
            target=target,
            file_name=name,
            file_size=size,
        )
    else:
        return record_upload_failed(
            name, size, str(exc), source=source, target=target,
            duration_sec=duration_sec,
        )


def human_size(size: int | float | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def query_events(
    event_type: str | None = None,
    limit: int = 100,
    after: int = 0,
) -> list[dict[str, Any]]:
    """Newest-first events, optionally filtered by type and id watermark."""
    limit = max(1, min(int(limit), 200))
    with _lock:
        events = list(_events)
    out = []
    for event in reversed(events):  # newest first
        if event_type and event["type"] != event_type:
            continue
        if after and event["id"] <= after:
            continue
        out.append(event)
        if len(out) >= limit:
            break
    return out


def stats() -> dict[str, Any]:
    with _lock:
        counters = dict(_counters)
        last = _last_event_at
        pending = len(_pending)
    return {
        "uploads": counters["upload"],
        "upload_failed": counters["upload_failed"],
        "network_errors": counters["network_error"],
        "infos": counters["info"],
        "total": sum(counters.values()),
        "last_event_at": last,
        "uptime_sec": int(time.time() - _started_at),
        "webhook_pending": pending,
        "webhook_url": bool(config.WEB_DASHBOARD_WEBHOOK_URL),
    }


def api_payload(
    event_type: str | None = None,
    limit: int = 100,
    after: int = 0,
) -> dict[str, Any]:
    """JSON payload for GET /api/logs — same shape as the website backend."""
    return {
        "ok": True,
        "events": query_events(event_type, limit, after),
        "stats": stats(),
        "server_time": _now_iso(),
    }


def _ensure_forwarder() -> None:
    """Start the webhook forwarder thread once, on first use."""
    global _forwarder_thread
    with _forwarder_lock:
        if _forwarder_thread is not None and _forwarder_thread.is_alive():
            return
        _forwarder_thread = threading.Thread(
            target=_forward_loop, name="activity-webhook", daemon=True
        )
        _forwarder_thread.start()


def _forward_loop() -> None:
    while True:
        time.sleep(FORWARD_INTERVAL_SEC)
        try:
            _forward_batch()
        except Exception:  # noqa: BLE001 - keep the loop alive no matter what
            log.debug("webhook forward failed", exc_info=True)


def _forward_batch() -> None:
    url = config.WEB_DASHBOARD_WEBHOOK_URL
    if not url:
        with _lock:
            _pending.clear()
        return
    if not url.startswith(("http://", "https://")):
        return

    with _lock:
        batch = list(_pending)
    if not batch:
        return

    headers = {"Content-Type": "application/json"}
    if config.WEB_DASHBOARD_WEBHOOK_TOKEN:
        headers["x-dashboard-token"] = config.WEB_DASHBOARD_WEBHOOK_TOKEN

    try:
        response = requests.post(
            url,
            data=json.dumps({"events": batch}),
            headers=headers,
            timeout=FORWARD_TIMEOUT_SEC,
        )
        if response.status_code in (200, 201, 204):
            with _lock:
                # Sirf wahi events hatao jo is batch me the.
                sent_ids = {e["id"] for e in batch}
                remaining = deque(maxlen=FORWARD_QUEUE_MAX)
                for event in _pending:
                    if event["id"] not in sent_ids:
                        remaining.append(event)
                _pending.clear()
                _pending.extend(remaining)
            log.debug("Forwarded %s activity events to %s", len(batch), url)
        else:
            log.warning(
                "Activity webhook %s returned HTTP %s", url, response.status_code
            )
    except requests.RequestException as exc:
        log.debug("Activity webhook unreachable: %r", exc)
