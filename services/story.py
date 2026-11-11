"""Telegram story (t.me/<user>/s/<id>) download -> TeraBox upload."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid

from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.raw import functions as raw_functions
from pyrogram.raw import types as raw_types
from pyrogram.types import Story

import config
import db
import tgdl
from services import activity_log
from services.progress import ProgressReporter
from services.userbot import (
    get_user_client,
    session_dead,
    warm_peer_cache,
)
from terabox import TeraBox, TeraboxError
from utils.async_helpers import to_thread_wait_on_cancel
from utils.formatting import human, safe_name
from utils.telegram import safe_edit

log = logging.getLogger("story")

STORY_TARGET_TYPE = "story"

STAGE_TEXT = {
    "hash": "🔐 File hash ban raha hai...",
    "precreate": "📡 TeraBox upload session ban raha hai...",
    "upload": "⬆️ TeraBox par upload ho raha hai...",
    "finish": "🔗 Share link ban raha hai...",
}


class StoryError(Exception):
    """User-facing story failure."""


def _story_media(story: Story):
    """Return the pyrogram media object of a parsed story."""
    for attr in ("video", "photo", "animation", "document"):
        media = getattr(story, attr, None)
        if media is not None:
            return media
    return None


async def _fetch_story(client: Client, chat, story_id: int) -> Story:
    """Fetch one story via GetStoriesByID with FloodWait retry."""
    last_exc: BaseException | None = None
    for attempt in range(4):
        try:
            peer = await client.resolve_peer(chat)
            result = await client.invoke(
                raw_functions.stories.GetStoriesByID(peer=peer, id=[int(story_id)])
            )
            users = {u.id: u for u in result.users}
            chats = {c.id: c for c in result.chats}

            for item in result.stories:
                if isinstance(item, raw_types.StoryItemDeleted):
                    continue
                story = await Story._parse(client, item, peer, users, chats)
                if story is not None and not story.deleted:
                    return story

            raise StoryError("Story nahi mili — expired ya deleted ho gayi hai.")
        except FloodWait as exc:
            last_exc = exc
            wait = min(int(exc.value) + 2, 150)
            log.warning("Story fetch flood wait %ss (%s), retrying", exc.value, story_id)
            activity_log.record_network_error(
                f"Telegram FloodWait {exc.value}s while fetching story — retrying",
                error=exc,
                source="telegram",
            )
            await asyncio.sleep(wait)
    raise StoryError(
        f"Story fetch flood-limit me atki ({last_exc}). Thodi der baad try karo."
    )


def _peer_for_chat(chat_key: int):
    if int(chat_key) > 0:
        return raw_types.PeerUser(user_id=int(chat_key))
    return raw_types.PeerChannel(channel_id=-int(chat_key) - 1_000_000_000_000)


async def _parse_story_item(
    client: Client, item, chat_key: int, users: dict, chats: dict
) -> Story:
    """Parse a full raw StoryItem (from GetPeerStories) without extra API calls.

    The peer must be the *resolved* InputPeer (with access_hash). The
    stories endpoints return the story OWNER only as a stub or omit it
    from the users vector entirely (measured live: GetPinnedStories for a
    user target returned only forwarded-from authors). kurigram's
    Story._parse fallback then fetches the owner via users.GetUsers,
    which needs a valid InputUser: a bare PeerUser yields UserEmpty and
    parsing crashes with KeyError on the chats dict (100/100 posts
    failed because of this).
    """
    try:
        peer = await client.resolve_peer(int(chat_key))
    except Exception:  # noqa: BLE001 - cold cache: fall back to a raw peer
        peer = _peer_for_chat(chat_key)
    for attempt in range(4):
        try:
            return await Story._parse(client, item, peer, users, chats)
        except FloodWait as exc:
            wait = min(int(exc.value) + 2, 150)
            log.warning("Story parse flood wait %ss, retrying", exc.value)
            await asyncio.sleep(wait)
    raise StoryError("Story parse nahi ho payi (flood wait). Thodi der baad try karo.")


async def resolve_story_client(app: Client | None, user_id: int, chat) -> Client:
    """Stories need a user session; bots rarely have story access."""
    user_client = await get_user_client(user_id)
    if user_client is not None:
        try:
            await user_client.resolve_peer(chat)
            return user_client
        except Exception as exc:  # noqa: BLE001
            log.info("User session cannot resolve %s (%r); warming peer cache", chat, exc)
            # Fresh string sessions have no peer cache: one dialog scan makes
            # numeric ids resolvable for every chat the owner actually has.
            if await warm_peer_cache(user_client):
                try:
                    await user_client.resolve_peer(chat)
                    return user_client
                except Exception as exc2:  # noqa: BLE001
                    log.info("Warm resolve also failed for %s: %r", chat, exc2)

    if session_dead(user_id):
        raise StoryError(
            "⚠️ Aapka Telegram user session **expire ho gaya hai** — stories "
            "isi session se download hoti hain. /login dobara karo "
            "(phone + OTP), phir try karo."
        )

    if app is None:
        raise StoryError(
            "Story access ke liye Telegram user session chahiye. /login se connect karo."
        )

    try:
        await app.resolve_peer(chat)
        return app
    except Exception as exc:  # noqa: BLE001
        if not db.get_session(user_id):
            raise StoryError(
                "Story access ke liye **Telegram user session chahiye** "
                "(/login karo — phone + OTP). Bot accounts stories "
                "access nahi kar sakte."
            ) from exc
        raise StoryError(
            f"Story owner '{chat}' ko access nahi mila ({exc}). "
            "User ko /login session me apne chats ka hona chahiye, ya "
            "bot ko public channel me add karo."
        ) from exc


def is_story_target(target: dict | None) -> bool:
    return str((target or {}).get("chat_type") or "") == STORY_TARGET_TYPE


def story_chat_ref(target: dict) -> str | int:
    """Prefer the username for peer resolution.

    In-memory session strings do not carry a peer cache, so resolving a bare
    numeric user id fails with PeerIdInvalid on a fresh process; usernames
    resolve via an API call and cache the peer. The numeric id still works for
    dedupe keys and Peer construction.
    """
    username = (target or {}).get("username")
    return username or int(target["chat_id"])


def story_link(username: str | None, story_id: int) -> str | None:
    if username and story_id:
        return f"https://t.me/{username}/s/{int(story_id)}"
    return None


async def active_story_snapshot(client: Client, chat):
    """Return (raw StoryItems, users, chats) of a peer's active stories."""
    peer = await client.resolve_peer(chat)
    result = await client.invoke(raw_functions.stories.GetPeerStories(peer=peer))
    items = getattr(result, "stories", None)
    if items is not None and not isinstance(items, (list, tuple)):
        # Newer layers wrap the inner peer-stories object.
        items = getattr(items, "stories", None)
    users = {u.id: u for u in (getattr(result, "users", None) or [])}
    chats = {c.id: c for c in (getattr(result, "chats", None) or [])}
    return list(items or []), users, chats


async def fetch_active_stories(client: Client, chat) -> list:
    """Return raw StoryItem list of currently active stories of a user."""
    items, _, _ = await active_story_snapshot(client, chat)
    return items


async def fetch_pinned_stories(
    client: Client, chat, *, page_size: int = 100, max_items: int = 300
) -> tuple[list, dict, dict]:
    """Return (raw StoryItems, users, chats) of a peer's pinned stories.

    A user's profile **Posts tab** is exactly their pinned stories, so this
    is how profile posts are fetched. Paginates with offset_id (lowest id of
    the last page) until Telegram returns a short page.
    """
    peer = await client.resolve_peer(chat)
    all_items: list = []
    users: dict = {}
    chats: dict = {}
    offset = 0
    for _ in range(6):  # hard cap: 6 pages * page_size
        result = await client.invoke(
            raw_functions.stories.GetPinnedStories(
                peer=peer, offset_id=int(offset), limit=int(page_size)
            )
        )
        items = getattr(result, "stories", None)
        if items is not None and not isinstance(items, (list, tuple)):
            # Some layers wrap the inner stories object.
            items = getattr(items, "stories", None)
        users.update({u.id: u for u in (getattr(result, "users", None) or [])})
        chats.update({c.id: c for c in (getattr(result, "chats", None) or [])})
        items = list(items or [])
        if not items:
            break
        all_items.extend(items)
        if len(items) < page_size or len(all_items) >= max_items:
            break
        offset = min(int(getattr(it, "id", 0) or 0) for it in items)
    return all_items, users, chats


def full_story_item(items: list, story_id: int):
    """Return the full raw StoryItem for an id, or None (deleted/skipped/missing)."""
    for item in items:
        if int(getattr(item, "id", 0) or 0) == int(story_id):
            if isinstance(item, (raw_types.StoryItemDeleted, raw_types.StoryItemSkipped)):
                return None
            return item
    return None


def max_story_id(items: list) -> int:
    best = 0
    for item in items:
        sid = int(getattr(item, "id", 0) or 0)
        if sid > best:
            best = sid
    return best


def pending_story_ids(items: list, last_seen: int) -> list[int]:
    """Sorted non-deleted story ids greater than last_seen."""
    ids: list[int] = []
    for item in items:
        if isinstance(item, raw_types.StoryItemDeleted):
            continue
        sid = int(getattr(item, "id", 0) or 0)
        if sid > int(last_seen):
            ids.append(sid)
    return sorted(set(ids))


async def backfill_active_stories(
    app: Client | None,
    user_id: int,
    target: dict,
    status_message=None,
) -> dict:
    """Download + upload every active story of a story target.

    Already-uploaded stories are skipped via dedupe. ``last_seen_id`` is advanced
    so the auto engine only handles stories posted after this backfill.
    """
    chat = int(target["chat_id"])
    username = target.get("username")
    chat_ref = story_chat_ref(target)

    client = await resolve_story_client(app, user_id, chat_ref)
    items, users, chats = await active_story_snapshot(client, chat_ref)
    ids = pending_story_ids(items, 0)
    total = len(ids)

    summary = {"total": total, "uploaded": 0, "skipped": 0, "failed": 0}
    if total == 0:
        db.set_last_seen(int(target["id"]), max_story_id(items))
        return summary

    for index, sid in enumerate(ids, start=1):
        if status_message is not None:
            await safe_edit(
                status_message,
                f"📸 **{target['title']}** — stories {index}/{total} process ho rahi hain...",
            )
        try:
            result = await process_story(
                app,
                user_id,
                chat_ref,
                sid,
                status_message=status_message,
                source_link=story_link(username, sid),
                client=client,
                chat_key=chat,
                raw_item=full_story_item(items, sid),
                users=users,
                chats=chats,
                target_id=int(target["id"]),
            )
        except StoryError as exc:
            summary["failed"] += 1
            log.warning("Backfill story %s/%s failed: %s", chat, sid, exc)
            continue
        except Exception:  # noqa: BLE001
            summary["failed"] += 1
            log.exception("Backfill story %s/%s crashed", chat, sid)
            continue

        if result.status == "uploaded":
            summary["uploaded"] += 1
            db.bump_target(int(target["id"]), uploaded=1)
        elif result.status == "skipped":
            summary["skipped"] += 1
            db.bump_target(int(target["id"]), skipped=1)

        # Telegram story API flood-limits aggressively; stay gentle.
        await asyncio.sleep(1.5)

    db.set_last_seen(int(target["id"]), max_story_id(items) or max(ids))
    return summary


async def backfill_pinned_stories(
    app: Client | None,
    user_id: int,
    target: dict,
    status_message=None,
) -> dict:
    """Download + upload every profile post (pinned story) of a story target.

    Already-uploaded posts are skipped via dedupe. ``last_post_id`` is
    advanced so the auto engine only handles posts published after this.
    """
    chat = int(target["chat_id"])
    username = target.get("username")
    chat_ref = story_chat_ref(target)

    client = await resolve_story_client(app, user_id, chat_ref)
    items, users, chats = await fetch_pinned_stories(client, chat_ref)
    ids = pending_story_ids(items, 0)
    total = len(ids)

    summary = {"total": total, "uploaded": 0, "skipped": 0, "failed": 0}
    if total == 0:
        db.set_last_post_seen(int(target["id"]), max_story_id(items))
        return summary

    for index, sid in enumerate(ids, start=1):
        if status_message is not None:
            await safe_edit(
                status_message,
                f"🎬 **{target['title']}** — posts {index}/{total} process ho rahe hain...",
            )
        try:
            result = await process_story(
                app,
                user_id,
                chat_ref,
                sid,
                status_message=status_message,
                source_link=story_link(username, sid),
                client=client,
                chat_key=chat,
                raw_item=full_story_item(items, sid),
                users=users,
                chats=chats,
                target_id=int(target["id"]),
            )
        except StoryError as exc:
            summary["failed"] += 1
            log.warning("Backfill post %s/%s failed: %s", chat, sid, exc)
            continue
        except Exception:  # noqa: BLE001
            summary["failed"] += 1
            log.exception("Backfill post %s/%s crashed", chat, sid)
            continue

        if result.status == "uploaded":
            summary["uploaded"] += 1
            db.bump_target(int(target["id"]), uploaded=1)
        elif result.status == "skipped":
            summary["skipped"] += 1
            db.bump_target(int(target["id"]), skipped=1)

        await asyncio.sleep(1.5)

    db.set_last_post_seen(int(target["id"]), max_story_id(items) or max(ids))
    return summary


def _story_name(story: Story, chat, story_id: int, media) -> str:
    file_name = getattr(media, "file_name", None)
    if file_name:
        return safe_name(file_name)

    owner = getattr(story, "from_user", None) or getattr(story, "sender_chat", None)
    owner_name = getattr(owner, "username", None) or getattr(owner, "title", None) or ""
    owner_name = safe_name(owner_name)[:32].strip(" ._")

    unique = getattr(media, "file_unique_id", None) or uuid.uuid4().hex[:10]
    if getattr(media, "file_name", None) is None and story.video:
        ext = ".mp4"
    elif story.photo:
        ext = ".jpg"
    else:
        ext = os.path.splitext(getattr(media, "file_name", "") or "")[1] or ".bin"

    base = f"story_{owner_name}_{story_id}" if owner_name else f"story_{story_id}"
    return safe_name(f"{base}_{unique[:8]}{ext}")


async def process_story(
    app: Client | None,
    user_id: int,
    chat,
    story_id: int,
    *,
    status_message=None,
    reporter: ProgressReporter | None = None,
    source_link: str | None = None,
    client: Client | None = None,
    chat_key: int | None = None,
    raw_item=None,
    users: dict | None = None,
    chats: dict | None = None,
    target_id: int | None = None,
):
    """Download one Telegram story and upload it to TeraBox.

    ``raw_item``/``users``/``chats`` let batch/auto callers reuse an already
    fetched StoryItem from GetPeerStories instead of one GetStoriesByID per
    story (the story API flood-limits aggressively). Mirrors
    ``pipeline.process_message``; returns ProcessResult-like status
    ("uploaded" | "skipped" | "no_media").
    """
    from services.pipeline import ProcessResult, upload_part_slots

    target_title: str | None = None
    if target_id:
        _target = db.get_target(int(target_id))
        if _target:
            target_title = _target.get("title")

    token = db.get_token(user_id)
    if not token:
        raise StoryError("TeraBox account bind nahi hai. /bind se apni cookie do.")

    client = client or await resolve_story_client(app, user_id, chat)

    # Resolve the story chat to a numeric id for dedupe bookkeeping.
    if chat_key is not None:
        chat_key = int(chat_key)
    else:
        try:
            resolved = await client.get_chat(chat)
            chat_key = int(resolved.id)
        except Exception:  # noqa: BLE001
            chat_key = abs(hash(str(chat))) % 10**12

    # Links List queries uploads by target_id, so every upload must carry
    # the owning target's DB row id. Callers with a known target pass it
    # explicitly; direct story links auto-attach the user's saved target
    # for this chat (if any) so the link still shows up in the list.
    target_id = int(target_id) if target_id else None
    if target_id is None:
        try:
            owner = db.get_target_by_chat(user_id, int(chat_key))
            if owner:
                target_id = int(owner["id"])
        except Exception:  # noqa: BLE001
            target_id = None

    existing = db.upload_exists(user_id, chat_key, int(story_id))
    if existing:
        return ProcessResult(
            "skipped", link=existing, source_link=source_link, message_id=story_id
        )

    loop = asyncio.get_running_loop()
    reporter = reporter or ProgressReporter(status_message, loop=loop)
    if reporter.loop is None:
        reporter.loop = loop

    try:
        await reporter.set_stage("⏳ Story fetch ho rahi hai...")

        if raw_item is not None:
            try:
                story = await _parse_story_item(
                    client, raw_item, chat_key, users or {}, chats or {}
                )
            except (KeyError, ValueError, TypeError):
                # Snapshot context missing this peer (or an unexpected shape):
                # re-fetch the single story; GetStoriesByID returns a complete
                # users vector, so parsing is reliable there.
                log.warning(
                    "Story %s/%s parse failed; falling back to GetStoriesByID",
                    chat_key,
                    story_id,
                    exc_info=True,
                )
                story = await _fetch_story(client, chat, story_id)
        else:
            story = await _fetch_story(client, chat, story_id)

        if getattr(story, "deleted", False) or getattr(story, "skipped", False):
            return ProcessResult(
                "no_media", source_link=source_link, message_id=story_id
            )

        media = _story_media(story)
        if media is None:
            return ProcessResult(
                "no_media", source_link=source_link, message_id=story_id
            )

        name = _story_name(story, chat, story_id, media)
        size = int(getattr(media, "file_size", 0) or 0)
        if size > config.MAX_FILE_MB * 1024 * 1024:
            raise StoryError(
                f"Story {human(size)} ki hai, limit {config.MAX_FILE_MB} MB hai."
            )

        os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
        local = os.path.join(
            config.DOWNLOAD_DIR,
            f"{user_id}_story_{abs(chat_key)}_{story_id}_{uuid.uuid4().hex[:10]}_{name}",
        )

        try:
            if size:
                usage = shutil.disk_usage(config.DOWNLOAD_DIR)
                free = usage.free
                buffer = min(64 * 1024 * 1024, max(8 * 1024 * 1024, size // 100))
                reserve = config.MIN_FREE_MB * 1024 * 1024
                if free < size + buffer + reserve:
                    raise StoryError(
                        f"Disk space kam hai. Available {human(free)}, "
                        f"required {human(size + buffer + reserve)}."
                    )

            await reporter.set_stage("⬇️ Story download ho rahi hai...")

            async def download_progress(current: int, total: int, rate: float) -> None:
                await reporter.update(
                    current,
                    total,
                    rate,
                    stage="⬇️ Story download ho rahi hai...",
                )

            await tgdl.download_media_parallel(
                client,
                media,
                local,
                size,
                download_progress,
                config.TELEGRAM_DOWNLOAD_WORKERS,
                range_limiter=None,
            )

            if not os.path.exists(local):
                raise StoryError("Story file disk par nahi mili.")

            actual_size = os.path.getsize(local)
            await reporter.set_stage(
                f"✅ Story download complete ({human(actual_size)})\n"
                "⬆️ TeraBox upload shuru...",
                force=True,
            )

            tb = TeraBox(token)
            await to_thread_wait_on_cancel(tb.ensure_login)

            started = time.monotonic()

            def upload_progress(current: int, total: int) -> None:
                rate = current / max(time.monotonic() - started, 0.001)
                reporter.thread_update(
                    current, total, rate, stage="⬆️ TeraBox par upload ho raha hai..."
                )

            def stage_callback(stage: str) -> None:
                text = STAGE_TEXT.get(stage)
                if text:
                    asyncio.run_coroutine_threadsafe(reporter.set_stage(text), loop)

            link = await to_thread_wait_on_cancel(
                tb.upload,
                local,
                name,
                config.REMOTE_FOLDER,
                upload_progress,
                stage_callback,
                config.TERABOX_UPLOAD_THREADS,
                upload_part_slots,
            )

            db.save_upload(
                user_id,
                target_id,
                chat_key,
                int(story_id),
                name,
                actual_size,
                link,
                source_link,
            )
            activity_log.record_upload(
                name,
                actual_size,
                link,
                time.monotonic() - started,
                source="story",
                target=target_title,
            )
            log.info("Uploaded story %s/%s -> %s", chat, story_id, link)
            return ProcessResult(
                "uploaded", link=link, name=name, source_link=source_link
            )
        finally:
            if os.path.exists(local):
                try:
                    os.remove(local)
                except OSError:
                    pass

    except TeraboxError as exc:
        detail = str(exc)
        activity_log.record_upload_failed(
            None, None, f"TeraBox (story): {detail}",
            source="story", target=target_title,
        )
        if any(
            marker in detail
            for marker in ("31045", "user not exists", "errno=-6", "not login", "4000023")
        ):
            raise StoryError(
                "TeraBox cookie expire/adhoori hai. Browser me dm.1024terabox.com par "
                "fresh login karke poori Cookie copy karo aur /bind karo."
            ) from exc
        raise StoryError(f"TeraBox error: {detail}") from exc
    except StoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Story upload failed")
        activity_log.record_failure(
            exc, source="story", target=target_title,
        )
        raise StoryError(f"Story upload fail hui: {exc}") from exc
