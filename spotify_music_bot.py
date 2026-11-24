import asyncio
import re
import traceback

from pyrogram import Client, filters, idle
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ParseMode, ButtonStyle

import config
import mongodb


SPOTIFY_RE = re.compile(
    r"https?://open\\.spotify\\.com/(track|playlist|album)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)


async def log_new_user(bot: Client, user) -> None:
    if not getattr(config, "LOG_CHANNEL", None):
        return

    name = user.first_name or "Unknown"
    if user.last_name:
        name += f" {user.last_name}"

    username = f"@{user.username}" if user.username else "<i>None</i>"
    text = (
        "<blockquote>🆕 <b>New User</b>\n\n"
        f"<b>Name:</b> {name}\n"
        f"<b>ID:</b> <code>{user.id}</code>\n"
        f"<b>Username:</b> {username}</blockquote>"
    )

    try:
        photos = []
        async for photo in bot.get_chat_photos(user.id, limit=1):
            photos.append(photo)

        if photos:
            await bot.send_photo(
                config.LOG_CHANNEL,
                photos[0].file_id,
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                config.LOG_CHANNEL,
                text,
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        traceback.print_exc()


async def log_download(bot: Client, user, name: str) -> None:
    if not getattr(config, "LOG_CHANNEL", None):
        return

    full_name = user.first_name or "Unknown"
    if user.last_name:
        full_name += f" {user.last_name}"

    tag = f"@{user.username}" if user.username else f"<code>{user.id}</code>"
    text = (
        "<blockquote>🎵 <b>Track Delivered</b>\n\n"
        f"<b>User:</b> {full_name} ({tag})\n"
        f"<b>ID:</b> <code>{user.id}</code>\n"
        f"<b>Track:</b> {name}</blockquote>"
    )
    try:
        await bot.send_message(config.LOG_CHANNEL, text, parse_mode=ParseMode.HTML)
    except Exception:
        traceback.print_exc()


async def cmd_start(bot: Client, msg: Message) -> None:
    user = msg.from_user
    if user is None:
        return

    try:
        if await mongodb.is_new_user(user.id):
            await mongodb.add_user(
                user_id=user.id,
                first_name=user.first_name,
                username=user.username,
                dc_id=user.dc_id,
            )
            await log_new_user(bot, user)
    except Exception:
        # Database logging should not prevent the bot from answering /start.
        traceback.print_exc()

    buttons = []
    dev_url = getattr(config, "DEV_URL", None)
    if dev_url:
        buttons.append(
            InlineKeyboardButton("Dev", url=dev_url, style=ButtonStyle.PRIMARY)
        )
    buttons.append(
        InlineKeyboardButton(
            "Credits", callback_data="credits", style=ButtonStyle.PRIMARY
        )
    )

    await msg.reply_text(
        "<blockquote><b>Hey 👋</b>\n"
        "<b>Send me a Spotify track, playlist, or album link.</b>\n\n"
        "<i>Link recognition is enabled. Audio delivery needs an authorized "
        "audio provider.</i></blockquote>",
        reply_markup=InlineKeyboardMarkup([buttons]),
        parse_mode=ParseMode.HTML,
    )


async def cb_credits(_: Client, cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        await cb.message.reply_text(
            "<blockquote><b>Credits</b>\n<i>Spotify Music Bot.</i></blockquote>",
            parse_mode=ParseMode.HTML,
        )


async def handle_message(_: Client, msg: Message) -> None:
    text = (msg.text or "").strip()
    match = SPOTIFY_RE.search(text)

    if not match:
        await msg.reply_text("That doesn't look like a supported Spotify link.")
        return

    link_type = match.group(1).lower()
    spotify_url = match.group(0)

    # The previous third-party scraper depended on a fabricated CAPTCHA token.
    # This safe version recognizes links but does not claim to download audio.
    await msg.reply_text(
        "<b>Spotify link detected.</b>\n\n"
        f"Type: <code>{link_type}</code>\n"
        f'<a href="{spotify_url}">Open in Spotify</a>\n\n'
        "Audio downloading is not configured. Connect an authorized audio "
        "source or use audio files you own or have permission to distribute.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def main() -> None:
    bot = Client(
        "spoti_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
    )

    bot.add_handler(
        MessageHandler(cmd_start, filters.command("start") & filters.private)
    )
    bot.add_handler(
        CallbackQueryHandler(cb_credits, filters.regex(r"^credits$"))
    )
    bot.add_handler(
        MessageHandler(
            handle_message,
            filters.text & filters.private & ~filters.command(["start"]),
        )
    )

    await mongodb.connect()
    try:
        await bot.start()
        print("[bot] running — waiting for messages...", flush=True)
        await idle()
    finally:
        if bot.is_connected:
            await bot.stop()
        await mongodb.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
    
