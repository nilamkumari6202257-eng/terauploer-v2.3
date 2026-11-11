"""Telegram user-session login (phone + OTP + 2FA) and client cache."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from pyrogram import Client
from pyrogram.errors import (
    AuthKeyDuplicated,
    AuthKeyUnregistered,
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionExpired,
    SessionPasswordNeeded,
    SessionRevoked,
    Unauthorized,
)

import config
import db

log = logging.getLogger("userbot")


@dataclass
class LoginFlow:
    user_id: int
    phone: str
    client: Client
    phone_code_hash: str
    created_at: float = field(default_factory=time.time)


_flows: dict[int, LoginFlow] = {}
_clients: dict[int, Client] = {}
_warmed: set[str] = set()
# Owners whose stored session was probed and found dead this process.
# A PFS-expired key connects fine but fails on the first API call, so the
# probe in get_user_client() is the only reliable way to know.
_dead_sessions: set[int] = set()
_lock = asyncio.Lock()

# Telegram permanently rejects these keys: re-login is the only fix.
_AUTH_DEAD_ERRORS = (
    AuthKeyUnregistered,
    AuthKeyDuplicated,
    Unauthorized,
    SessionRevoked,
    SessionExpired,
)


def session_dead(user_id: int) -> bool:
    """True when the stored session was found dead (needs re-/login)."""
    return int(user_id) in _dead_sessions


def mark_session_dead(user_id: int) -> None:
    _dead_sessions.add(int(user_id))


def clear_dead_mark(user_id: int) -> None:
    _dead_sessions.discard(int(user_id))


async def warm_peer_cache(client: Client) -> bool:
    """One-time full dialog scan so bare numeric ids / odd usernames resolve.

    A fresh string session carries no peer cache, so ``get_chat(12345)``
    fails with PEER_ID_INVALID even for chats the owner actually has. One
    ``get_dialogs()`` pass caches every peer the account knows; after that
    numeric ids resolve normally.

    Returns True only when a scan actually ran now, so callers can retry the
    failed resolve exactly once.
    """
    key = getattr(client, "name", None) or repr(client)
    if key in _warmed:
        return False
    try:
        async for _ in client.get_dialogs():
            pass
    except Exception as exc:  # noqa: BLE001 - warmup is best-effort
        log.warning("Peer cache warmup failed: %r", exc)
        return False
    _warmed.add(key)
    log.info("Peer cache warmed for %s", key)
    return True


def normalize_phone(raw: str) -> str:
    value = re.sub(r"[\s\-()]", "", (raw or "").strip())
    if not value.startswith("+"):
        value = "+" + value.lstrip("+")
    return value


def flow_step(user_id: int) -> str | None:
    flow = _flows.get(user_id)
    return "code" if flow else None


def has_session(user_id: int) -> bool:
    return db.get_session(user_id) is not None


async def begin_login(user_id: int, phone: str) -> None:
    """Connect a temporary client and request the login code."""
    phone = normalize_phone(phone)
    if not re.fullmatch(r"\+\d{7,15}", phone):
        raise ValueError("Phone number galat lag raha hai. Country code ke saath bhejo, jaise +919876543210.")

    await cancel_login(user_id)
    client = Client(
        f"login_{user_id}_{int(time.time())}",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        in_memory=True,
        no_updates=True,
        sleep_threshold=30,
    )
    await client.connect()
    try:
        sent = await client.send_code(phone)
    except Exception:
        await _safe_disconnect(client)
        raise

    _flows[user_id] = LoginFlow(
        user_id=user_id,
        phone=phone,
        client=client,
        phone_code_hash=sent.phone_code_hash,
    )
    log.info("Login code requested for user %s", user_id)


async def submit_code(user_id: int, code: str) -> str:
    """Return ``"ok"`` when signed in, ``"password"`` when 2FA is required."""
    flow = _flows.get(user_id)
    if flow is None:
        raise ValueError("Login session expire ho gaya. /login dobara karo.")

    code = re.sub(r"\s+", "", code or "")
    try:
        await flow.client.sign_in(flow.phone, flow.phone_code_hash, code)
    except SessionPasswordNeeded:
        return "password"
    except PhoneCodeInvalid as exc:
        raise ValueError("OTP galat hai. Dobara bhejo.") from exc
    except PhoneCodeExpired as exc:
        raise ValueError("OTP expire ho gaya. /login dobara karo.") from exc
    except FloodWait as exc:
        raise ValueError(f"Telegram ne {exc.value}s wait karne ko kaha. Thodi der baad try karo.") from exc

    await _finish_login(flow)
    return "ok"


async def submit_password(user_id: int, password: str) -> None:
    flow = _flows.get(user_id)
    if flow is None:
        raise ValueError("Login session expire ho gaya. /login dobara karo.")
    try:
        await flow.client.check_password(password)
    except PasswordHashInvalid as exc:
        raise ValueError("2FA password galat hai.") from exc
    except FloodWait as exc:
        raise ValueError(f"Telegram ne {exc.value}s wait karne ko kaha.") from exc
    await _finish_login(flow)


async def _finish_login(flow: LoginFlow) -> None:
    session = await flow.client.export_session_string()
    db.save_session(flow.user_id, session, flow.phone)
    await _safe_disconnect(flow.client)
    _flows.pop(flow.user_id, None)
    await close_user_client(flow.user_id)
    _dead_sessions.discard(flow.user_id)
    log.info("User session stored for %s", flow.user_id)


async def cancel_login(user_id: int) -> None:
    flow = _flows.pop(user_id, None)
    if flow is not None:
        await _safe_disconnect(flow.client)


async def get_user_client(user_id: int) -> Client | None:
    """Return a connected user client for this owner, or None."""
    async with _lock:
        # Probed-dead sessions short-circuit: no reconnect spam every poll
        # cycle. Re-login clears the mark via _finish_login().
        if int(user_id) in _dead_sessions:
            return None
        cached = _clients.get(user_id)
        if cached is not None:
            return cached

        session = db.get_session(user_id)
        if not session:
            return None

        client = Client(
            f"user_{user_id}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session,
            no_updates=True,
            sleep_threshold=30,
            max_concurrent_transmissions=config.TELEGRAM_TOTAL_DOWNLOAD_WORKERS,
        )
        try:
            await client.connect()
            # Health probe: an expired PFS key connects silently and only
            # fails on the first API call. Probing once here means every
            # caller (resolve, stories, dialogs) gets a clear "re-login"
            # answer instead of cryptic PEER_ID_INVALID fallbacks.
            await client.get_me()
        except _AUTH_DEAD_ERRORS as exc:
            log.warning("Stored user session is dead (%r) — /login needed", exc)
            _dead_sessions.add(user_id)
            await _safe_disconnect(client)
            return None
        except Exception as exc:  # noqa: BLE001 - network hiccups are not fatal
            log.warning("Stored user session could not connect: %r", exc)
            await _safe_disconnect(client)
            return None

        _dead_sessions.discard(user_id)
        _clients[user_id] = client
        return client


async def close_user_client(user_id: int) -> None:
    async with _lock:
        client = _clients.pop(user_id, None)
    if client is not None:
        _warmed.discard(getattr(client, "name", None) or repr(client))
        await _safe_disconnect(client)


async def close_all() -> None:
    for user_id in list(_clients):
        await close_user_client(user_id)
    for user_id in list(_flows):
        await cancel_login(user_id)


async def _safe_disconnect(client: Client) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


__all__ = [
    "PhoneNumberInvalid",
    "begin_login",
    "cancel_login",
    "clear_dead_mark",
    "close_all",
    "close_user_client",
    "flow_step",
    "get_user_client",
    "has_session",
    "mark_session_dead",
    "normalize_phone",
    "session_dead",
    "submit_code",
    "submit_password",
    "warm_peer_cache",
]
