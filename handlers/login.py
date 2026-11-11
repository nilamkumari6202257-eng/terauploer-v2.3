"""Telegram user session login and logout."""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import db
from handlers import state
from services import userbot
from utils.telegram import safe_edit

log = logging.getLogger("handlers.login")


async def show_login(app: Client, message, uid: int, *, edit: bool = False) -> None:
    if db.get_session(uid):
        phone = db.get_session_phone(uid) or "connected"
        alive = await userbot.get_user_client(uid)
        if alive is not None:
            text = (
                f"✅ Telegram user session active hai ({phone}).\n\n"
                "Private groups/channels + user stories/posts isi session se "
                "download honge.\n"
                "/logout se remove kar sakte ho."
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🚪 Logout", callback_data="l:out")]]
            )
        else:
            text = (
                f"⚠️ Aapka session **expire ho gaya** hai ({phone}).\n\n"
                "Telegram ne purani key reject kar di — /login dobara karo:\n"
                "phone number → OTP → (2FA password agar hai).\n\n"
                "Naya login karne par purana session replace ho jayega."
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Re-login", callback_data="l:new")]]
            )
        if edit:
            await safe_edit(message, text, keyboard)
        else:
            await message.reply_text(text, reply_markup=keyboard)
        return

    state.login_flow[uid] = {"step": "phone"}
    text = (
        "👤 **Telegram login**\n\n"
        "Apna phone number country code ke saath bhejo, jaise `+919876543210`.\n\n"
        "⚠️ Ye session sirf private groups/channels download ke liye use hoga. "
        "Session encrypted save hoti hai.\n\n"
        "/cancel se ruk sakte ho."
    )
    if edit:
        await safe_edit(message, text)
    else:
        await message.reply_text(text)


async def _delete(message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def handle_text(app: Client, message) -> None:
    uid = message.from_user.id
    flow = state.login_flow.get(uid)
    if not flow:
        return

    text = (message.text or "").strip()
    step = flow.get("step")

    if step == "phone":
        try:
            await userbot.begin_login(uid, text)
        except Exception as exc:  # noqa: BLE001
            await message.reply_text(f"❌ Code send nahi hua: {exc}")
            return
        state.login_flow[uid] = {"step": "code"}
        await _delete(message)
        await app.send_message(uid, "📩 OTP aa gaya? Code bhejo (jaise `12345`).")
        return

    if step == "code":
        try:
            result = await userbot.submit_code(uid, text)
        except Exception as exc:  # noqa: BLE001
            await message.reply_text(f"❌ {exc}")
            return
        await _delete(message)
        if result == "password":
            state.login_flow[uid] = {"step": "password"}
            await app.send_message(uid, "🔐 2FA enabled hai. Password bhejo.")
        else:
            state.login_flow.pop(uid, None)
            await app.send_message(
                uid,
                "✅ **Login successful!**\n\n"
                "Ab numeric user IDs bhi resolve hongi, aur stories/posts "
                "download hongi. `/set @username` ya `/set <user id>` try karo.",
            )
        return

    if step == "password":
        try:
            await userbot.submit_password(uid, text)
        except Exception as exc:  # noqa: BLE001
            await message.reply_text(f"❌ {exc}")
            return
        await _delete(message)
        state.login_flow.pop(uid, None)
        await app.send_message(
            uid,
            "✅ **Login successful!**\n\n"
            "Ab numeric user IDs bhi resolve hongi, aur stories/posts "
            "download hongi. `/set @username` ya `/set <user id>` try karo.",
        )


async def do_logout(app: Client, message, uid: int) -> None:
    await userbot.close_user_client(uid)
    removed = db.delete_session(uid)
    await message.reply_text(
        "🚪 Logout ho gaya. Session delete kar diya."
        if removed
        else "Koi active session nahi tha."
    )


async def handle_callback(app: Client, query) -> None:
    data = query.data or ""
    if data == "l:out":
        await do_logout(app, query.message, query.from_user.id)
    elif data == "l:new":
        # Re-login button from the "session expired" screen.
        uid = query.from_user.id
        state.login_flow[uid] = {"step": "phone"}
        await safe_edit(
            query.message,
            "👤 **Telegram re-login**\n\n"
            "Apna phone number country code ke saath bhejo, jaise `+919876543210`.\n\n"
            "/cancel se ruk sakte ho.",
        )


def register(app: Client) -> None:
    @app.on_message(filters.command("login") & filters.private)
    async def login_cmd(client, message):
        await show_login(client, message, message.from_user.id)

    @app.on_message(filters.command("logout") & filters.private)
    async def logout_cmd(client, message):
        await do_logout(client, message, message.from_user.id)
