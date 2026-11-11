"""Polls auto-mode targets for new media and uploads it to TeraBox.

Bot-watch targets (created by /bot) poll the owner's private chat with the
watched bot and upload only media sent by that bot, showing a live progress
bar in the bot chat.
"""
from __future__ import annotations

import asyncio
import logging

from pyrogram import Client

import config
import db
from services.batch_engine import fetch_messages, latest_message_id
from services.pipeline import PipelineError, pick_media, process_message, resolve_client
from services.userbot import session_dead
from services.story import (
    StoryError,
    active_story_snapshot,
    fetch_pinned_stories,
    full_story_item,
    max_story_id,
    pending_story_ids,
    process_story,
    resolve_story_client,
    story_chat_ref,
    story_link,
)
from utils.telegram import safe_edit

log = logging.getLogger("auto")

_task: asyncio.Task | None = None
_locks: dict[int, asyncio.Lock] = {}
_cycle = 0
# Owners already told about their dead session (avoid 20s spam).
_dead_notified: set[int] = set()


async def _notify_session_dead(app: Client, user_id: int) -> None:
    """Tell the owner once that their session died; a live session resets it."""
    if not session_dead(user_id):
        _dead_notified.discard(int(user_id))
        return
    if int(user_id) in _dead_notified:
        return
    _dead_notified.add(int(user_id))
    try:
        await app.send_message(
            int(user_id),
            "⚠️ **Aapka Telegram user session expire ho gaya hai.**\n\n"
            "Auto story/post download isi session se hota hai.\n"
            "1️⃣ `/login` karo (phone + OTP)\n"
            "2️⃣ sab targets apne aap resume ho jayenge",
        )
    except Exception as exc:  # noqa: BLE001 - notice is best-effort
        log.debug("Session-dead notice failed: %r", exc)


def start(app: Client) -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(app), name="auto-engine")
    log.info("Auto engine started (poll every %ss)", config.AUTO_POLL_SECONDS)


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    await asyncio.gather(_task, return_exceptions=True)
    _task = None


async def _loop(app: Client) -> None:
    while True:
        try:
            await poll_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the poller must survive one bad target
            log.exception("Auto poll failed")
        await asyncio.sleep(config.AUTO_POLL_SECONDS)


async def poll_once(app: Client) -> None:
    global _cycle
    _cycle += 1
    # Profile posts change rarely: only check them every Nth cycle.
    check_posts = _cycle % config.POST_POLL_EVERY == 0
    for target in db.list_auto_targets():
        try:
            await _process_target(app, target, check_posts=check_posts)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Auto target %s failed", target["id"])


async def _process_target(app: Client, target: dict, *, check_posts: bool = False) -> None:
    lock = _locks.setdefault(int(target["id"]), asyncio.Lock())
    if lock.locked():
        return

    async with lock:
        if str(target.get("chat_type") or "") == "story":
            await _process_story_target(app, target, check_posts=check_posts)
            return

        user_id = int(target["user_id"])
        chat_id = int(target["chat_id"])
        bot_watch = bool(target.get("bot_watch"))
        client = await resolve_client(
            app, user_id, chat_id, username=target.get("username")
        )
        latest = await latest_message_id(client, chat_id)

        last = int(target["last_seen_id"] or 0)
        if last <= 0:
            last = int(target["start_message_id"]) - 1
        if latest <= last:
            return

        cursor = last
        while cursor < latest:
            chunk_end = min(cursor + config.BATCH_CHUNK_SIZE, latest)
            messages = await fetch_messages(
                client, chat_id, list(range(cursor + 1, chunk_end + 1))
            )
            for message in messages:
                cursor = int(message.id)
                if getattr(message, "empty", False) or pick_media(message) is None:
                    continue
                if bot_watch:
                    # Only media sent by the watched bot itself (chat_id is its
                    # user id); skip anything the owner sent to that bot.
                    sender = getattr(message, "from_user", None)
                    if sender is None or int(sender.id) != chat_id:
                        continue

                status = None
                if bot_watch:
                    status = await _try_status(app, user_id, target)
                try:
                    result = await process_message(
                        app,
                        user_id,
                        chat_id,
                        message.id,
                        target=target,
                        status_message=status,
                        activity_source="bot-watch" if bot_watch else "auto",
                    )
                except PipelineError as exc:
                    db.bump_target(target["id"], failed=1)
                    log.warning(
                        "Auto upload failed for %s/%s: %s", chat_id, message.id, exc
                    )
                    if status is not None:
                        await safe_edit(status, f"❌ {exc}")
                    continue
                except Exception:  # noqa: BLE001
                    db.bump_target(target["id"], failed=1)
                    log.exception(
                        "Auto upload crashed for %s/%s", chat_id, message.id
                    )
                    if status is not None:
                        await safe_edit(status, "❌ Upload fail hua. Thodi der baad dobara try hoga.")
                    continue

                if result.status == "uploaded":
                    if status is not None:
                        await safe_edit(status, _bot_done_text(target, result))
                    else:
                        await _notify(app, target, result)
            if cursor < chunk_end:
                cursor = chunk_end
            db.set_last_seen(target["id"], cursor)


async def _process_story_target(
    app: Client, target: dict, *, check_posts: bool = False
) -> None:
    """Poll one story target: 24h stories and/or profile posts (pinned)."""
    user_id = int(target["user_id"])
    chat_id = int(target["chat_id"])
    chat_ref = story_chat_ref(target)
    story_mode = bool(int(target.get("story_mode") or 0))
    post_mode = bool(int(target.get("post_mode") or 0))

    if not story_mode and not post_mode:
        return

    try:
        client = await resolve_story_client(app, user_id, chat_ref)
    except Exception as exc:  # noqa: BLE001
        log.warning("Story target %s poll failed: %r", target["id"], exc)
        await _notify_session_dead(app, user_id)
        return

    if story_mode:
        await _poll_active_stories(app, target, client)
    if check_posts and post_mode:
        await _poll_story_posts(app, target, client)


async def _poll_active_stories(app: Client, target: dict, client) -> None:
    """Upload stories posted after last_seen_id."""
    user_id = int(target["user_id"])
    chat_id = int(target["chat_id"])
    username = target.get("username")
    chat_ref = story_chat_ref(target)

    try:
        items, users, chats = await active_story_snapshot(client, chat_ref)
    except Exception as exc:  # noqa: BLE001
        log.warning("Story target %s poll failed: %r", target["id"], exc)
        return

    last = int(target["last_seen_id"] or 0)
    ids = pending_story_ids(items, last)
    if not ids:
        return

    seen = last
    for sid in ids:
        try:
            result = await process_story(
                app,
                user_id,
                chat_ref,
                sid,
                source_link=story_link(username, sid),
                client=client,
                chat_key=chat_id,
                raw_item=full_story_item(items, sid),
                users=users,
                chats=chats,
                target_id=int(target["id"]),
            )
        except StoryError as exc:
            db.bump_target(target["id"], failed=1)
            log.warning("Auto story upload failed for %s/s/%s: %s", chat_id, sid, exc)
            continue
        except Exception:  # noqa: BLE001
            db.bump_target(target["id"], failed=1)
            log.exception("Auto story upload crashed for %s/s/%s", chat_id, sid)
            continue

        seen = max(seen, sid)
        db.set_last_seen(target["id"], seen)

        if result.status == "uploaded":
            db.bump_target(target["id"], uploaded=1)
            await _notify_story(app, target, result)
        elif result.status == "skipped":
            db.bump_target(target["id"], skipped=1)

    # Expired/deleted stories we never saw must not re-trigger later.
    db.set_last_seen(target["id"], max_story_id(items))


async def _poll_story_posts(app: Client, target: dict, client) -> None:
    """Upload profile posts (pinned stories) newer than last_post_id."""
    user_id = int(target["user_id"])
    chat_id = int(target["chat_id"])
    username = target.get("username")
    chat_ref = story_chat_ref(target)

    try:
        items, users, chats = await fetch_pinned_stories(client, chat_ref)
    except Exception as exc:  # noqa: BLE001
        log.warning("Post target %s poll failed: %r", target["id"], exc)
        return

    last = int(target["last_post_id"] or 0)
    ids = pending_story_ids(items, last)
    if not ids:
        return

    seen = last
    for pid in ids:
        try:
            result = await process_story(
                app,
                user_id,
                chat_ref,
                pid,
                source_link=story_link(username, pid),
                client=client,
                chat_key=chat_id,
                raw_item=full_story_item(items, pid),
                users=users,
                chats=chats,
                target_id=int(target["id"]),
            )
        except StoryError as exc:
            db.bump_target(target["id"], failed=1)
            log.warning("Auto post upload failed for %s/s/%s: %s", chat_id, pid, exc)
            continue
        except Exception:  # noqa: BLE001
            db.bump_target(target["id"], failed=1)
            log.exception("Auto post upload crashed for %s/s/%s", chat_id, pid)
            continue

        seen = max(seen, pid)
        db.set_last_post_seen(target["id"], seen)

        if result.status == "uploaded":
            db.bump_target(target["id"], uploaded=1)
            await _notify_post(app, target, result)
        elif result.status == "skipped":
            db.bump_target(target["id"], skipped=1)

    # Posts that were pinned while we were busy must not re-trigger later.
    db.set_last_post_seen(target["id"], max_story_id(items))


async def _try_status(app: Client, user_id: int, target: dict):
    """Send the live progress message for a bot-watch upload."""
    try:
        return await app.send_message(
            int(user_id),
            f"🤖 **{target['title']}** ne media bheji — download shuru...",
        )
    except Exception as exc:  # noqa: BLE001 - progress is optional
        log.debug("Bot-watch status message failed: %r", exc)
        return None


def _bot_done_text(target: dict, result) -> str:
    lines = [
        "✅ **Bot video upload complete**",
        "",
        f"🤖 {target['title']}",
        f"📁 `{result.name}`",
        f"🔗 {result.link}",
    ]
    if result.source_link:
        lines.append(f"↳ source: {result.source_link}")
    return "\n".join(lines)


async def _notify_story(app: Client, target: dict, result) -> None:
    text = (
        "📸 **Story upload complete**\n\n"
        f"👤 {target['title']}\n"
        f"📁 `{result.name}`\n"
        f"🔗 {result.link}"
    )
    if result.source_link:
        text += f"\n↳ source: {result.source_link}"
    try:
        await app.send_message(int(target["user_id"]), text)
    except Exception as exc:  # noqa: BLE001
        log.warning("Story notification failed: %r", exc)


async def _notify_post(app: Client, target: dict, result) -> None:
    text = (
        "🎬 **Post upload complete**\n\n"
        f"👤 {target['title']}\n"
        f"📁 `{result.name}`\n"
        f"🔗 {result.link}"
    )
    if result.source_link:
        text += f"\n↳ source: {result.source_link}"
    try:
        await app.send_message(int(target["user_id"]), text)
    except Exception as exc:  # noqa: BLE001
        log.warning("Post notification failed: %r", exc)


async def _notify(app: Client, target: dict, result) -> None:
    text = (
        "🤖 **Auto upload complete**\n\n"
        f"📢 {target['title']}\n"
        f"📁 `{result.name}`\n"
        f"🔗 {result.link}\n"
        f"↳ source: {result.source_link}"
    )
    try:
        await app.send_message(int(target["user_id"]), text)
    except Exception as exc:  # noqa: BLE001
        log.warning("Auto notification failed: %r", exc)
