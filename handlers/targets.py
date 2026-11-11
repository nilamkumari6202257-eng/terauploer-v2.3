"""Target management: /set, /targets dashboard, links list and TXT export."""
from __future__ import annotations

import asyncio
import io
import logging
import math
import re

from pyrogram import Client, enums, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import db
from handlers import state
from services import story as story_svc
from services.batch_engine import latest_message_id
from services.pipeline import resolve_client
from services.userbot import (
    get_user_client,
    session_dead,
    warm_peer_cache,
)
from utils.formatting import bar, safe_name, short
from utils.links import message_link, parse_telegram_link
from utils.telegram import safe_edit

log = logging.getLogger("handlers.targets")


def _parse_chat_ref(text: str):
    link = parse_telegram_link(text)
    if link is not None:
        return link.chat

    value = (text or "").strip()
    if value.startswith("@"):
        return value
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if "t.me/" in value:
        tail = value.rstrip("/").split("/")[-1]
        if tail and not tail.startswith("+"):
            return tail
    return value


def _resolve_hint(uid: int, ref, exc) -> str:
    """Build a specific, actionable error for a failed chat resolve."""
    if session_dead(uid):
        return (
            "⚠️ **Aapka Telegram user session expire ho gaya hai** — "
            "yahi wajah hai ki ID resolve nahi hui.\n\n"
            "1️⃣ `/login` bhejo (phone + OTP + 2FA agar hai)\n"
            "2️⃣ phir `/set " + f"{ref}` dobara — ab resolve ho jayegi\n\n"
            f"Original error: `{exc}`"
        )
    if re.fullmatch(r"-?\d+", str(ref)):
        return (
            f"❌ ID `{ref}` resolve nahi hui: `{exc}`\n\n"
            "**Numeric ID Telegram me tab tak nahi chalti jab tak aapka "
            "/login account us user/channel se *mila* na ho** — ye "
            "Telegram ka rule hai, har bot me same.\n\n"
            "**Permanent fix — koi ek karo:**\n"
            "• @username ya t.me/username link bhejo — **100% chalta hai**\n"
            "• `/login` wale account se us user ko **ek DM bhejo** "
            "(sirf 1 message) — uske baad `/set <id>` chalega\n"
            "• Agar us user ne is bot ko kabhi /start kiya hai to ID "
            "turant chalegi (bot ne usse *mila* hai)\n\n"
            "💡 Story/post target ke liye user session bhi zaroori hai "
            "(`/login`), bot accounts stories access nahi kar sakte."
        )
    return (
        f"❌ Chat resolve nahi hua: `{exc}`\n\n"
        "Ye try karo:\n"
        "• @username ya t.me/username link\n"
        "• Group/channel ke liye bot ko us chat me add karo"
    )


async def _resolve_chat(app: Client, uid: int, ref):
    """Resolve a chat ref (id/@username/link) for the owner.

    Chain: owner user session → one dialog-scan warmup retry → bot.
    A dead/expired session is reported with a clear /login instruction
    instead of a confusing PEER_ID_INVALID from the bot fallback.
    """
    user_client = await get_user_client(uid)
    if user_client is not None:
        try:
            return await user_client.get_chat(ref)
        except Exception:  # noqa: BLE001 - first attempt on a cold session
            if await warm_peer_cache(user_client):
                try:
                    return await user_client.get_chat(ref)
                except Exception:  # noqa: BLE001
                    pass

    # Bot fallback: works when the peer has met the bot (/start) or the
    # bot is a member of that group/channel.
    try:
        return await app.get_chat(ref)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(_resolve_hint(uid, ref, exc)) from exc


def _mode_label(enabled) -> str:
    return "🟢 ON" if enabled else "🔴 OFF"


def _target_text(target: dict) -> str:
    uploaded = db.count_uploads(target["id"])
    total = int(target["total_messages"] or 0)
    failed = int(target["failed_count"] or 0)
    skipped = int(target["skipped_count"] or 0)
    processed = uploaded + failed + skipped
    if total:
        processed = min(total, processed)
    pending = max(0, total - processed) if total else 0

    if story_svc.is_story_target(target):
        start_link = (
            story_svc.story_link(target["username"], target["start_message_id"])
            or "— abhi tak koi story nahi"
        )
        username_line = f"👤 @{target['username']}\n" if target.get("username") else ""
        lines = [
            f"👤 **{target['title']}** (user target)",
            f"🆔 `{target['chat_id']}`",
        ]
        if username_line:
            lines.append(username_line.rstrip("\n"))
        lines += [
            f"📸 Story Mode: **{_mode_label(target['story_mode'])}**",
            f"🎬 Post Mode: **{_mode_label(target['post_mode'])}**",
            f"🤖 Auto Mode: **{_mode_label(target['auto_mode'])}**",
            "",
            f"🔗 Latest seen: {start_link}",
            f"📊 ✅ {uploaded} | ♻️ {skipped} | ❌ {failed}",
            "",
            "📸 Story Mode = 24h wali stories",
            "🎬 Post Mode = profile ke Posts tab (pinned posts)",
            "🤖 Auto Mode = nayi story/post khud upload hogi",
        ]
        return "\n".join(lines)

    auto = "ON 🟢" if target["auto_mode"] else "OFF 🔴"
    start_link = target["start_link"] or message_link(
        target["chat_id"], target["start_message_id"], target["username"]
    )

    lines = [
        f"🎯 **{target['title']}**",
        f"🆔 `{target['chat_id']}`",
        f"🔗 Start: {start_link}",
        f"🤖 Auto mode: **{auto}**",
        "",
        f"📊 Total: {total or '—'} | ✅ {uploaded} | ❌ {failed} | ⏳ {pending}",
    ]
    if total:
        lines.append(bar(processed, total))
    return "\n".join(lines)


def _target_keyboard(target: dict) -> InlineKeyboardMarkup:
    tid = target["id"]

    if story_svc.is_story_target(target):
        story_label = "📸 Story: 🟢 ON" if target["story_mode"] else "📸 Story: 🔴 OFF"
        post_label = "🎬 Post: 🟢 ON" if target["post_mode"] else "🎬 Post: 🔴 OFF"
        auto_label = "🤖 Auto: 🟢 ON" if target["auto_mode"] else "🤖 Auto: 🔴 OFF"
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(story_label, callback_data=f"t:sm:{tid}"),
                    InlineKeyboardButton(post_label, callback_data=f"t:pm:{tid}"),
                ],
                [InlineKeyboardButton(auto_label, callback_data=f"t:a:{tid}")],
                [
                    InlineKeyboardButton("▶️ Download Stories", callback_data=f"b:t:{tid}"),
                    InlineKeyboardButton("⬇️ Download Posts", callback_data=f"t:dp:{tid}"),
                ],
                [InlineKeyboardButton("🚀 Download All (Stories + Posts)", callback_data=f"t:da:{tid}")],
                [
                    InlineKeyboardButton("🔗 Links List", callback_data=f"t:l:{tid}:0"),
                    InlineKeyboardButton("🗑 Remove", callback_data=f"t:r:{tid}"),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data="t:list")],
            ]
        )

    auto_label = "🔄 Auto OFF" if target["auto_mode"] else "🔄 Auto ON"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔗 Links List", callback_data=f"t:l:{tid}:0"),
                InlineKeyboardButton("▶️ Batch Start", callback_data=f"b:t:{tid}"),
            ],
            [
                InlineKeyboardButton(auto_label, callback_data=f"t:a:{tid}"),
                InlineKeyboardButton("🗑 Remove", callback_data=f"t:r:{tid}"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="t:list")],
        ]
    )


async def _refresh_total(app: Client, uid: int, target: dict) -> dict:
    if int(target["total_messages"] or 0) > 0:
        return target
    if story_svc.is_story_target(target):
        return target
    try:
        client = await resolve_client(
            app, uid, target["chat_id"], username=target.get("username")
        )
        latest = await latest_message_id(client, target["chat_id"])
        total = max(0, latest - int(target["start_message_id"]) + 1)
        db.set_target_total(target["id"], total)
        return db.get_target(target["id"]) or target
    except Exception as exc:  # noqa: BLE001 - display still works without total
        log.debug("Could not refresh total for %s: %r", target["id"], exc)
        return target


async def show_targets(app: Client, message, uid: int, *, edit: bool = False) -> None:
    targets = db.list_targets(uid)
    if not targets:
        text = "🎯 Koi target save nahi hai.\n\n/set se group/channel/user add karo."
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("➕ Add Target", callback_data="m:add")]]
        )
    else:
        buttons = []
        lines = [f"🎯 **Targets ({len(targets)})**", ""]
        for index, target in enumerate(targets, start=1):
            uploaded = db.count_uploads(target["id"])
            total = int(target["total_messages"] or 0)
            mark = "✅" if total and uploaded >= total else "⏳"
            icon = "👤" if story_svc.is_story_target(target) else "📢"
            label = f"{icon} {short(str(target['title']), 24)}"
            if story_svc.is_story_target(target):
                label += (
                    f" | 📸{'🟢' if target['story_mode'] else '🔴'}"
                    f" 🎬{'🟢' if target['post_mode'] else '🔴'}"
                    f" 🤖{'🟢' if target['auto_mode'] else '🔴'}"
                )
                lines.append(
                    f"{index}. {icon} **{short(str(target['title']), 26)}** — "
                    f"story {_mode_label(target['story_mode'])} · "
                    f"post {_mode_label(target['post_mode'])} · "
                    f"auto {_mode_label(target['auto_mode'])}"
                )
            else:
                label += f" | {uploaded}/{total or '?'} {mark}"
                lines.append(
                    f"{index}. {icon} **{short(str(target['title']), 26)}** — "
                    f"✅ {uploaded}/{total or '?'} {mark} · "
                    f"auto {_mode_label(target['auto_mode'])}"
                )
            buttons.append(
                [InlineKeyboardButton(label, callback_data=f"t:v:{target['id']}")]
            )
        buttons.append([InlineKeyboardButton("➕ Add Target", callback_data="m:add")])
        lines += [
            "",
            "📸 story mode · 🎬 post mode · 🤖 auto mode",
            "Kisi target par click karke modes change karo.",
        ]
        text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup(buttons)

    if edit:
        await safe_edit(message, text, keyboard)
    else:
        await message.reply_text(text, reply_markup=keyboard)


async def start_set_flow(app: Client, message, uid: int, *, edit: bool = False) -> None:
    state.set_flow[uid] = {"step": "chat"}
    text = (
        "🎯 **Naya target**\n\n"
        "Group/channel ka ID, @username ya link bhejo.\n\n"
        "**User target (stories + posts):** user ka @username, user ID ya link bhejo —\n"
        "• 📸 Story Mode — 24h stories\n"
        "• 🎬 Post Mode — profile ke Posts tab wale posts\n"
        "• 🤖 Auto Mode — nayi story/post khud upload\n\n"
        "Example: `@my_channel` ya `@some_user` ya `12345678`\n\n"
        "💡 **Numeric ID ka rule**: aapke /login account ne us user/channel se "
        "pehle *milna* chahiye (DM/group), warna Telegram ID reject karti hai — "
        "@username hamesha chalta hai.\n\n"
        "/cancel se ruk sakte ho."
    )
    if edit:
        await safe_edit(message, text)
    else:
        await message.reply_text(text)


async def _handle_story_chat(app: Client, message, chat) -> None:
    """A user chat: register it as a story target instead of a message target."""
    uid = message.from_user.id
    if not db.get_session(uid):
        state.set_flow.pop(uid, None)
        await message.reply_text(
            "❌ User ki stories/posts download karne ke liye Telegram user session chahiye.\n\n"
            "1️⃣ /login — apna phone number + OTP\n"
            "2️⃣ phir /set @username — story target add karo"
        )
        return

    chat_id = int(chat.id)
    chat_ref = chat.username or chat_id
    is_new = db.get_target_by_chat(uid, chat_id) is None

    status = None
    try:
        status = await message.reply_text(
            f"🔎 **{chat.first_name or chat_id}** ki stories/posts check ho rahi hain..."
        )
    except Exception:  # noqa: BLE001 - status text is optional
        pass

    baseline = 0
    post_baseline = 0
    client = None
    try:
        client = await story_svc.resolve_story_client(app, uid, chat_ref)
        items = await story_svc.fetch_active_stories(client, chat_ref)
        baseline = story_svc.max_story_id(items)
    except Exception as exc:  # noqa: BLE001 - target is still valid, auto waits
        log.info("Story baseline for %s failed: %r", chat_id, exc)
    try:
        client = client or await story_svc.resolve_story_client(app, uid, chat_ref)
        post_items, _, _ = await story_svc.fetch_pinned_stories(client, chat_ref)
        post_baseline = story_svc.max_story_id(post_items)
    except Exception as exc:  # noqa: BLE001
        log.info("Post baseline for %s failed: %r", chat_id, exc)

    title = (chat.first_name or chat.username or str(chat_id)).strip()
    username = chat.username
    start_link = story_svc.story_link(username, baseline) if baseline else None

    target_id = db.add_target(
        uid, chat_id, title, username, story_svc.STORY_TARGET_TYPE,
        baseline, start_link,
    )
    db.set_last_seen(target_id, baseline)
    db.set_last_post_seen(target_id, post_baseline)
    db.set_auto_mode(target_id, True)
    if is_new:
        # Fresh user target: stories and posts both ON by default.
        db.set_story_mode(target_id, True)
        db.set_post_mode(target_id, True)
    state.set_flow.pop(uid, None)

    target = db.get_target(target_id)
    if target is None:
        await message.reply_text("❌ Target save nahi hua. Dobara /set karo.")
        return

    text = (
        "✅ **Target save ho gaya!**\n\n"
        + _target_text(target)
        + "\n\n⚠️ Auto mode ON hai — ab se nayi stories/posts khud upload hongi. "
        "Abhi wali purani cheezein lene ke liye **🚀 Download All** button dabao "
        "(ya alag-alag Download Stories / Download Posts)."
    )
    if status is not None:
        try:
            await safe_edit(status, text, _target_keyboard(target))
        except Exception:  # noqa: BLE001
            await message.reply_text(text, reply_markup=_target_keyboard(target))
    else:
        await message.reply_text(text, reply_markup=_target_keyboard(target))


async def _handle_chat(app: Client, message, flow: dict, ref=None) -> None:
    uid = message.from_user.id
    raw = ref if ref is not None else (message.text or "")
    parsed = _parse_chat_ref(raw)

    try:
        chat = await _resolve_chat(app, uid, parsed)
    except Exception as exc:  # noqa: BLE001
        await message.reply_text(
            "❌ Chat resolve nahi hua: "
            f"`{exc}`"
        )
        return

    if getattr(chat, "type", None) == enums.ChatType.PRIVATE:
        await _handle_story_chat(app, message, chat)
        return

    title = chat.title or getattr(chat, "first_name", None) or str(chat.id)
    state.set_flow[uid] = {
        "step": "start_link",
        "chat_id": int(chat.id),
        "title": title,
        "username": chat.username,
        "chat_type": str(chat.type),
    }

    hint = (
        f"https://t.me/{chat.username}/42966"
        if chat.username
        else "https://t.me/c/3891933514/1428"
    )
    await message.reply_text(
        f"✅ Target mila: **{title}**\n🆔 `{chat.id}`\n\n"
        "Ab bhejo: **Download kahan se shuru karna hai?**\n"
        f"Us message ka link paste karo, jaise `{hint}`\n\n"
        "/cancel se ruk sakte ho."
    )


async def _handle_start_link(app: Client, message, flow: dict) -> None:
    uid = message.from_user.id
    text = (message.text or "").strip()
    link = parse_telegram_link(text)

    if link is not None:
        message_id = link.message_id
        if isinstance(link.chat, int) and link.chat != int(flow["chat_id"]):
            await message.reply_text(
                "❌ Ye link is target ka nahi hai. Same group/channel ka message link bhejo."
            )
            return
        if (
            isinstance(link.chat, str)
            and flow.get("username")
            and link.chat.lower() != str(flow["username"]).lower()
        ):
            await message.reply_text(
                "❌ Ye link is target ka nahi hai. Same group/channel ka message link bhejo."
            )
            return
    elif text.isdigit():
        message_id = int(text)
    else:
        await message.reply_text(
            "❌ Valid message link bhejo, jaise `https://t.me/username/123`"
        )
        return

    target_id = db.add_target(
        uid,
        int(flow["chat_id"]),
        str(flow["title"]),
        flow.get("username"),
        flow.get("chat_type"),
        message_id,
        text if link is not None else None,
    )
    db.set_last_seen(target_id, message_id - 1)
    state.set_flow.pop(uid, None)

    target = db.get_target(target_id)
    if target is None:
        await message.reply_text("❌ Target save nahi hua. Dobara /set karo.")
        return

    await message.reply_text(
        f"✅ **Target save ho gaya!**\n\n{_target_text(target)}",
        reply_markup=_target_keyboard(target),
    )


async def handle_text(app: Client, message) -> None:
    uid = message.from_user.id
    flow = state.set_flow.get(uid)
    if not flow:
        return
    if flow.get("step") == "chat":
        await _handle_chat(app, message, flow)
    elif flow.get("step") == "start_link":
        await _handle_start_link(app, message, flow)


async def _show_target(app: Client, query, uid: int, target_id: int) -> None:
    target = db.get_target(target_id)
    if target is None or int(target["user_id"]) != int(uid):
        await safe_edit(query.message, "❌ Target nahi mila.")
        return
    target = await _refresh_total(app, uid, target)
    await safe_edit(query.message, _target_text(target), _target_keyboard(target))


async def _show_links(app: Client, query, uid: int, target_id: int, page: int) -> None:
    target = db.get_target(target_id)
    if target is None or int(target["user_id"]) != int(uid):
        await safe_edit(query.message, "❌ Target nahi mila.")
        return

    page_size = config.LINKS_PAGE_SIZE
    total = db.count_uploads(target_id)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(int(page), pages - 1))
    rows = db.list_uploads(target_id, page * page_size, page_size)

    lines = [f"**Uploaded TeraBox links** — {target['title']}", ""]
    if not rows:
        lines.append("Abhi koi link upload nahi hua.")
    for index, row in enumerate(rows, start=page * page_size + 1):
        lines.append(f"{index}. 📁 {row['file_name']}")
        lines.append(f"   {row['terabox_url']}")
        lines.append(f"   ↳ source: {row['source_link'] or '—'}")
        lines.append("")
    lines.append(f"Page {page + 1}/{pages} · {total} links")

    buttons = []
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Prev", callback_data=f"t:l:{target_id}:{page - 1}"
            )
        )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                "Next ➡️", callback_data=f"t:l:{target_id}:{page + 1}"
            )
        )
    if nav:
        buttons.append(nav)
    buttons.append(
        [InlineKeyboardButton("📄 Export TXT", callback_data=f"t:e:{target_id}")]
    )
    buttons.append(
        [InlineKeyboardButton("⬅️ Back", callback_data=f"t:v:{target_id}")]
    )

    await safe_edit(query.message, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def _export(app: Client, query, uid: int, target_id: int) -> None:
    target = db.get_target(target_id)
    if target is None or int(target["user_id"]) != int(uid):
        await safe_edit(query.message, "❌ Target nahi mila.")
        return

    rows = db.list_uploads(target_id, 0, 1_000_000)
    lines = [
        f"Uploaded TeraBox links — {target['title']}",
        f"Target: {target['chat_id']}",
        f"Total: {len(rows)}",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. 📁 {row['file_name']}")
        lines.append(f"   {row['terabox_url']}")
        lines.append(f"   ↳ source: {row['source_link'] or '—'}")
        lines.append("")

    data = "\n".join(lines).encode("utf-8")
    filename = f"{safe_name(str(target['title']))[:40]}_links.txt"
    try:
        await app.send_document(
            uid,
            io.BytesIO(data),
            file_name=filename,
            caption=f"📄 {len(rows)} links · {target['title']}",
        )
    except Exception as exc:  # noqa: BLE001
        await safe_edit(query.message, f"❌ Export fail hua: {exc}")
        return

    await query.answer("TXT bhej diya", show_alert=False)


async def _run_posts_backfill(app: Client, status, uid: int, target: dict) -> None:
    """Background task: upload all profile posts (pinned stories) of a user target."""
    try:
        summary = await story_svc.backfill_pinned_stories(
            app, uid, target, status_message=status
        )
    except story_svc.StoryError as exc:
        await safe_edit(status, f"❌ {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("Posts backfill crashed")
        await safe_edit(status, f"❌ Posts download fail hue: {exc}")
        return

    if summary["total"] == 0:
        await safe_edit(
            status,
            f"ℹ️ **{target['title']}** ke profile par koi post nahi hai.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data=f"t:v:{target['id']}")]]
            ),
        )
        return

    lines = [
        "🎬 **Posts download complete**",
        "",
        f"👤 {target['title']}",
        f"📊 Total: {summary['total']} | ✅ {summary['uploaded']} | "
        f"♻️ {summary['skipped']} | ❌ {summary['failed']}",
        "",
        "Saare links *Links List* me hain.",
    ]
    await safe_edit(
        status,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data=f"t:v:{target['id']}")]]
        ),
    )


async def _run_all_backfill(app: Client, status, uid: int, target: dict) -> None:
    """Background task: stories + profile posts, dono ek saath."""
    tid = int(target["id"])
    parts: list[str] = []
    stories_error: str | None = None

    # --- stories pehle (24h wali) ---
    try:
        stories = await story_svc.backfill_active_stories(
            app, uid, target, status_message=status
        )
        parts.append(
            f"📸 Stories — total {stories['total']} · ✅ {stories['uploaded']} · "
            f"♻️ {stories['skipped']} · ❌ {stories['failed']}"
        )
    except story_svc.StoryError as exc:
        stories_error = str(exc)
        log.warning("Download-All stories failed for %s: %s", tid, exc)
    except Exception as exc:  # noqa: BLE001
        stories_error = str(exc)
        log.exception("Download-All stories crashed for %s", tid)

    # --- phir profile posts ---
    posts_error: str | None = None
    try:
        posts = await story_svc.backfill_pinned_stories(
            app, uid, target, status_message=status
        )
        parts.append(
            f"🎬 Posts — total {posts['total']} · ✅ {posts['uploaded']} · "
            f"♻️ {posts['skipped']} · ❌ {posts['failed']}"
        )
    except story_svc.StoryError as exc:
        posts_error = str(exc)
        log.warning("Download-All posts failed for %s: %s", tid, exc)
    except Exception as exc:  # noqa: BLE001
        posts_error = str(exc)
        log.exception("Download-All posts crashed for %s", tid)

    lines = ["🚀 **Download All complete**", "", f"👤 {target['title']}", ""]
    lines.extend(parts)
    if stories_error:
        lines.append(f"⚠️ Stories error: {stories_error}")
    if posts_error:
        lines.append(f"⚠️ Posts error: {posts_error}")
    lines += ["", "Saare links *Links List* me hain."]
    await safe_edit(
        status,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data=f"t:v:{tid}")]]
        ),
    )


async def handle_callback(app: Client, query) -> None:
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    uid = query.from_user.id

    if action == "list":
        await show_targets(app, query.message, uid, edit=True)
    elif action == "v":
        await _show_target(app, query, uid, int(parts[2]))
    elif action == "a":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        turning_on = not bool(target["auto_mode"])
        if turning_on:
            # Auto mode watches *new* media; backfill is the buttons' job.
            try:
                if story_svc.is_story_target(target):
                    chat_ref = story_svc.story_chat_ref(target)
                    client = await story_svc.resolve_story_client(
                        app, uid, chat_ref
                    )
                    items = await story_svc.fetch_active_stories(client, chat_ref)
                    latest = story_svc.max_story_id(items)
                    if latest:
                        db.set_last_seen(target_id, latest)
                    post_items, _, _ = await story_svc.fetch_pinned_stories(
                        client, chat_ref
                    )
                    post_latest = story_svc.max_story_id(post_items)
                    if post_latest:
                        db.set_last_post_seen(target_id, post_latest)
                else:
                    client = await resolve_client(
                        app, uid, target["chat_id"], username=target.get("username")
                    )
                    latest = await latest_message_id(client, target["chat_id"])
                    if latest:
                        db.set_last_seen(target_id, latest)
            except Exception as exc:  # noqa: BLE001 - toggle still works
                log.debug("Could not set auto baseline for %s: %r", target_id, exc)
        db.set_auto_mode(target_id, turning_on)
        target = db.get_target(target_id)
        if story_svc.is_story_target(target):
            await query.answer(
                f"Auto mode {'ON' if turning_on else 'OFF'} — ab se nayi stories/posts "
                f"{'khud upload hongi' if turning_on else 'khud upload nahi hongi'}"
            )
        else:
            await query.answer(
                "Auto mode ON — ab se nayi media khud upload hogi"
                if turning_on
                else "Auto mode OFF"
            )
        await safe_edit(query.message, _target_text(target), _target_keyboard(target))
    elif action == "sm":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        turning_on = not bool(target["story_mode"])
        db.set_story_mode(target_id, turning_on)
        target = db.get_target(target_id)
        await query.answer(
            f"Story mode {'ON' if turning_on else 'OFF'} — 24h stories "
            f"{'download hongi' if turning_on else 'download nahi hongi'}"
        )
        await safe_edit(query.message, _target_text(target), _target_keyboard(target))
    elif action == "pm":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        turning_on = not bool(target["post_mode"])
        db.set_post_mode(target_id, turning_on)
        target = db.get_target(target_id)
        await query.answer(
            f"Post mode {'ON' if turning_on else 'OFF'} — profile posts "
            f"{'download honge' if turning_on else 'download nahi honge'}"
        )
        await safe_edit(query.message, _target_text(target), _target_keyboard(target))
    elif action == "dp":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        if not story_svc.is_story_target(target):
            await query.answer("Ye button sirf user targets ke liye hai", show_alert=True)
            return
        await query.answer("Posts download shuru...")
        await safe_edit(
            query.message,
            f"🎬 **{target['title']}** ke profile posts download ho rahe hain...",
        )
        asyncio.create_task(
            _run_posts_backfill(app, query.message, uid, target),
            name=f"posts-backfill-{target_id}",
        )
    elif action == "da":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        if not story_svc.is_story_target(target):
            await query.answer("Ye button sirf user targets ke liye hai", show_alert=True)
            return
        await query.answer("Stories + posts download shuru...")
        await safe_edit(
            query.message,
            f"🚀 **{target['title']}** ki stories + posts download ho rahe hain...",
        )
        asyncio.create_task(
            _run_all_backfill(app, query.message, uid, target),
            name=f"all-backfill-{target_id}",
        )
    elif action == "r":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        count = db.count_uploads(target_id)
        text = (
            "⚠️ **Remove target?**\n\n"
            f"📢 {target['title']}\n"
            f"🔗 {count} uploaded links bhi delete ho jayenge."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, remove", callback_data=f"t:rc:{target_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Cancel", callback_data=f"t:v:{target_id}"
                    )
                ],
            ]
        )
        await safe_edit(query.message, text, keyboard)
    elif action == "rc":
        target_id = int(parts[2])
        target = db.get_target(target_id)
        if target is None or int(target["user_id"]) != int(uid):
            await safe_edit(query.message, "❌ Target nahi mila.")
            return
        db.delete_target(target_id)
        await show_targets(app, query.message, uid, edit=True)
    elif action == "l":
        await _show_links(app, query, uid, int(parts[2]), int(parts[3]))
    elif action == "e":
        await _export(app, query, uid, int(parts[2]))


def register(app: Client) -> None:
    @app.on_message(filters.command("set") & filters.private)
    async def set_cmd(client, message):
        if len(message.command) > 1:
            await _handle_chat(client, message, {"step": "chat"}, " ".join(message.command[1:]))
            return
        await start_set_flow(client, message, message.from_user.id)

    @app.on_message(filters.command("targets") & filters.private)
    async def targets_cmd(client, message):
        await show_targets(client, message, message.from_user.id)
