import os
import re
import json
import asyncio
import base64
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.enums import ParseMode, ButtonStyle

import config
import mongodb


DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

SPOTI_BASE = "https://spotidown.app"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)
MAX_WORKERS = 5

SPOTIFY_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?"
    r"(track|playlist|album)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)


# ── Spotify provider session ──────────────────────────────────────────────────

def _make_session() -> requests.Session:
    """Create a session and read the provider's expected form token.

    The provider may change its page or require a valid challenge response.
    This function deliberately does not fabricate CAPTCHA tokens.
    """
    session = requests.Session()

    retry = requests.packages.urllib3.util.retry.Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": UA,
        "Referer": SPOTI_BASE + "/en2",
        "X-Requested-With": "XMLHttpRequest",
    })

    response = session.get(SPOTI_BASE + "/en2", timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    hidden = soup.find(
        "input",
        {
            "type": "hidden",
            "name": re.compile(r"^_"),
        },
    )

    if hidden is None:
        raise RuntimeError(
            "The audio provider did not return its expected hidden form field. "
            "The provider page may have changed or may require a challenge."
        )

    field_name = hidden.get("name")
    field_value = hidden.get("value")
    if not field_name or field_value is None:
        raise RuntimeError(
            "The audio provider returned an incomplete hidden form field."
        )

    session._csrf = {field_name: field_value}
    return session


def _fetch_action(session: requests.Session, spotify_url: str) -> str:
    """Ask the provider to process a Spotify URL using its normal form fields."""
    response = session.post(
        SPOTI_BASE + "/action",
        data={
            "url": spotify_url,
            **getattr(session, "_csrf", {}),
        },
        timeout=60,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "The audio provider returned a non-JSON response."
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError("The audio provider returned an unexpected response.")

    if payload.get("error"):
        raise RuntimeError(
            str(payload.get("message") or "The audio provider rejected the request.")
        )

    data = payload.get("data")
    if not isinstance(data, str) or not data.strip():
        raise RuntimeError("The audio provider returned no track data.")

    return data


def _parse_forms(html: str):
    soup = BeautifulSoup(html or "", "html.parser")
    forms = soup.find_all("form", {"name": "submitspurl"})

    result = []
    for form in forms:
        fields = {}
        for input_tag in form.find_all("input"):
            field_name = input_tag.get("name")
            if field_name:
                fields[field_name] = input_tag.get("value", "")
        if fields:
            result.append(fields)

    image_tag = soup.find("img")
    fallback_thumb = image_tag.get("src") if image_tag else None
    return result, fallback_thumb


def _download_thumb(url: str, name: str):
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return None

    try:
        safe = re.sub(r'[\\/*?:"<>|]', "", name)[:80] or "track"
        path = os.path.join(DOWNLOAD_DIR, f"{safe}_thumb.jpg")

        with requests.get(
            url,
            timeout=30,
            headers={"User-Agent": UA},
        ) as response:
            response.raise_for_status()
            with open(path, "wb") as file:
                file.write(response.content)

        return path if os.path.exists(path) and os.path.getsize(path) > 0 else None
    except Exception:
        traceback.print_exc()
        return None


def _download_file(url: str, name: str) -> str:
    if not url or not isinstance(url, str):
        raise RuntimeError("The provider did not return a download URL.")

    safe = re.sub(r'[\\/*?:"<>|]', "", name)[:100] or "track"
    path = os.path.join(DOWNLOAD_DIR, f"{safe}.mp3")

    with requests.get(
        url,
        stream=True,
        timeout=120,
        headers={"User-Agent": UA},
    ) as response:
        response.raise_for_status()
        with open(path, "wb") as file:
            for chunk in response.iter_content(128 * 1024):
                if chunk:
                    file.write(chunk)

    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError("The downloaded file is empty.")

    return path


def _fetch_one(
    session: requests.Session,
    form_data: dict,
    index: int,
    fallback_thumb: str | None = None,
):
    try:
        encoded_data = form_data.get("data", "")
        if not encoded_data:
            raise ValueError("track form has no data field")

        info = json.loads(base64.b64decode(encoded_data).decode("utf-8"))
        if not isinstance(info, dict):
            raise ValueError("track metadata is not an object")

        title = info.get("name") or f"Track {index + 1}"
        artist = info.get("artist") or ""
        name = f"{title} - {artist}" if artist else title
        thumb_url = (
            info.get("cover")
            or info.get("image")
            or info.get("thumb")
            or fallback_thumb
        )
    except Exception:
        title = f"Track {index + 1}"
        artist = ""
        name = title
        thumb_url = fallback_thumb

    try:
        response = session.post(
            SPOTI_BASE + "/action/track",
            data=form_data,
            timeout=60,
        )
        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError:
            return (
                index, name, title, artist, None, None,
                "provider returned a non-JSON track response",
            )

        if not isinstance(payload, dict):
            return (
                index, name, title, artist, None, None,
                "provider returned an unexpected track response",
            )

        if payload.get("error"):
            return (
                index, name, title, artist, None, None,
                str(payload.get("message") or "provider rejected this track"),
            )

        html = payload.get("data")
        if not isinstance(html, str) or not html.strip():
            return (
                index, name, title, artist, None, None,
                "provider returned no track page",
            )

        soup = BeautifulSoup(html, "html.parser")

        image_tag = soup.find("img")
        if image_tag and not thumb_url:
            thumb_url = image_tag.get("src")

        download_url = None
        anchor = soup.find(
            "a",
            href=re.compile(r"/dl\?token=|rapid\.spotidown", re.IGNORECASE),
        )

        if anchor:
            download_url = anchor.get("href")
            if download_url and download_url.startswith("/"):
                download_url = SPOTI_BASE + download_url
        else:
            for candidate in soup.find_all(
                "a",
                href=re.compile(r"^https?://", re.IGNORECASE),
            ):
                download_url = candidate.get("href")
                if download_url:
                    break

        if not download_url:
            return (
                index, name, title, artist, None, None,
                "provider response contained no download link",
            )

        local_path = _download_file(download_url, name)
        local_thumb = _download_thumb(thumb_url, name)

        return index, name, title, artist, local_path, local_thumb, None

    except Exception as exc:
        return (
            index, name, title, artist, None, None,
            f"{type(exc).__name__}: {exc}",
        )


def spotify_get_track(spotify_url: str):
    session = _make_session()
    html = _fetch_action(session, spotify_url)
    forms, fallback_thumb = _parse_forms(html)

    if not forms:
        raise RuntimeError(
            "No track form was returned by the provider. "
            "The provider may have changed its page or rejected the link."
        )

    (
        _,
        name,
        title,
        artist,
        local_path,
        thumb,
        error,
    ) = _fetch_one(session, forms[0], 0, fallback_thumb)

    if error:
        raise RuntimeError(error)

    return name, title, artist, local_path, thumb


def spotify_get_playlist(spotify_url: str, on_result=None):
    session = _make_session()
    html = _fetch_action(session, spotify_url)
    forms, fallback_thumb = _parse_forms(html)
    total = len(forms)

    if total == 0:
        raise RuntimeError(
            "No track forms were returned. The playlist may be private, empty, "
            "unsupported, or rejected by the provider."
        )

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one, session, form, index, fallback_thumb): index
            for index, form in enumerate(forms)
        }

        for future in as_completed(futures):
            index = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = (
                    index,
                    f"Track {index + 1}",
                    f"Track {index + 1}",
                    "",
                    None,
                    None,
                    f"{type(exc).__name__}: {exc}",
                )

            if on_result:
                on_result(*result[:1], total, *result[1:])


# ── helpers ───────────────────────────────────────────────────────────────────

def cleanup(path: str | None):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def user_tag(user) -> str:
    return f"@{user.username}" if user.username else f"<code>{user.id}</code>"


def spotify_type(url: str) -> str:
    match = SPOTIFY_RE.search(url or "")
    return match.group(1).lower() if match else "unknown"


# ── logging ───────────────────────────────────────────────────────────────────

async def log_new_user(bot: Client, user) -> None:
    if not getattr(config, "LOG_CHANNEL", None):
        return

    name = user.first_name or "Unknown"
    if user.last_name:
        name += f" {user.last_name}"

    username = f"@{user.username}" if user.username else "<i>None</i>"
    text = (
        "<blockquote>"
        "🆕 <b>New User</b>\n\n"
        f"<b>Name     :</b>  <b>{name}</b>\n"
        f"<b>ID       :</b>  <code>{user.id}</code>\n"
        f"<b>Username :</b>  {username}"
        "</blockquote>"
    )

    photos = []
    try:
        async for photo in bot.get_chat_photos(user.id, limit=1):
            photos.append(photo)
    except Exception:
        pass

    try:
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

    tag = user_tag(user)
    full_name = user.first_name or "Unknown"
    if user.last_name:
        full_name += f" {user.last_name}"

    text = (
        "<blockquote>"
        "🎵 <b>Track Delivered</b>\n\n"
        f"<b>User     :</b>  <b>{full_name}</b>  ({tag})\n"
        f"<b>ID       :</b>  <code>{user.id}</code>\n\n"
        f"<b>Track    :</b>  <i>{name}</i>"
        "</blockquote>"
    )

    try:
        await bot.send_message(
            config.LOG_CHANNEL,
            text,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        traceback.print_exc()


# ── handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(bot: Client, msg: Message):
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
        traceback.print_exc()

    buttons = []
    dev_url = getattr(config, "DEV_URL", None)
    if dev_url:
        buttons.append(
            InlineKeyboardButton(
                "Dev",
                url=dev_url,
                style=ButtonStyle.PRIMARY,
            )
        )
    buttons.append(
        InlineKeyboardButton(
            "Credits",
            callback_data="credits",
            style=ButtonStyle.PRIMARY,
        )
    )

    await msg.reply_text(
        "<blockquote>\n"
        "<b>Hey 👋</b>\n"
        "<b>Send me a Spotify track or playlist link and I'll process it.</b>\n\n"
        "<i>Paste a supported Spotify link below.</i>\n"
        "</blockquote>",
        reply_markup=InlineKeyboardMarkup([buttons]),
        parse_mode=ParseMode.HTML,
    )


async def cb_credits(_: Client, cb: CallbackQuery):
    await cb.answer()
    if cb.message:
        await cb.message.reply_text(
            "<blockquote>\n"
            "<b>Credits</b>\n\n"
            "<i>This bot was built by Mark.</i>\n\n"
            "<i>Give credit if you find it useful.</i>\n"
            "</blockquote>",
            parse_mode=ParseMode.HTML,
        )


async def handle_message(bot: Client, msg: Message):
    text = (msg.text or "").strip()
    match = SPOTIFY_RE.search(text)

    if not match:
        await msg.reply_text("That doesn't look like a supported Spotify link.")
        return

    url = match.group(0)
    link_type = match.group(1).lower()
    user = msg.from_user

    if user is None:
        await msg.reply_text("I couldn't identify the sender of this message.")
        return

    # ── single track ──────────────────────────────────────────────────────────
    if link_type == "track":
        status = await msg.reply_text("Fetching track...")
        local_path = None
        thumb = None

        try:
            loop = asyncio.get_running_loop()
            name, title, artist, local_path, thumb = await loop.run_in_executor(
                None,
                spotify_get_track,
                url,
            )

            await status.edit_text("Uploading...")
            if thumb:
                try:
                    await msg.reply_photo(
                        photo=thumb,
                        caption=f"<b>{name}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    traceback.print_exc()

            await msg.reply_audio(
                audio=local_path,
                title=title,
                performer=artist,
                thumb=thumb,
            )

            await status.delete()
            await log_download(bot, user, name)

        except Exception as exc:
            traceback.print_exc()
            try:
                await status.edit_text(
                    "Something went wrong while processing this link.\n\n"
                    f"<code>{type(exc).__name__}: {exc}</code>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                await msg.reply_text(
                    f"Error: {type(exc).__name__}: {exc}"
                )
        finally:
            cleanup(local_path)
            cleanup(thumb)

    # ── playlist / album ──────────────────────────────────────────────────────
    elif link_type in ("playlist", "album"):
        status = await msg.reply_text("Fetching playlist / album...")

        completed = 0
        failed = 0
        main_loop = asyncio.get_running_loop()
        pending_tasks = []

        async def send_track_result(
            name, title, artist, local_path, thumb, error, total
        ):
            nonlocal completed, failed

            if error:
                failed += 1
                print(f"[skip] {name}: {error}", flush=True)
                cleanup(local_path)
                cleanup(thumb)
            else:
                completed += 1
                try:
                    await msg.reply_audio(
                        audio=local_path,
                        caption=f"<b>{name}</b>",
                        title=title,
                        performer=artist,
                        thumb=thumb,
                        parse_mode=ParseMode.HTML,
                    )
                    await log_download(bot, user, name)
                except Exception:
                    failed += 1
                    traceback.print_exc()
                finally:
                    cleanup(local_path)
                    cleanup(thumb)

            try:
                await status.edit_text(
                    "<blockquote>"
                    f"📥 <b>{completed + failed}/{total}</b> done\n"
                    f"✅ <b>{completed}</b> succeeded   "
                    f"❌ <b>{failed}</b> failed"
                    "</blockquote>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        def on_result(
            index,
            total,
            name,
            title,
            artist,
            local_path,
            thumb,
            error,
        ):
            future = asyncio.run_coroutine_threadsafe(
                send_track_result(
                    name,
                    title,
                    artist,
                    local_path,
                    thumb,
                    error,
                    total,
                ),
                loop=main_loop,
            )
            pending_tasks.append(future)

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: spotify_get_playlist(url, on_result=on_result),
            )

            # Wait for all Telegram send operations scheduled by worker threads.
            if pending_tasks:
                await asyncio.gather(
                    *[
                        asyncio.wrap_future(future)
                        for future in pending_tasks
                    ],
                    return_exceptions=True,
                )

            try:
                await status.edit_text(
                    "<blockquote>"
                    f"Finished.\n✅ <b>{completed}</b> succeeded\n"
                    f"❌ <b>{failed}</b> failed"
                    "</blockquote>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        except Exception as exc:
            traceback.print_exc()
            await status.edit_text(
                "Something went wrong while processing this playlist / album.\n\n"
                f"<code>{type(exc).__name__}: {exc}</code>",
                parse_mode=ParseMode.HTML,
            )

    else:
        await msg.reply_text("Unsupported Spotify link type.")


# ── entry point ───────────────────────────────────────────────────────────────

async def main():
    bot = Client(
        "spoti_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
    )

    bot.add_handler(
        MessageHandler(
            cmd_start,
            filters.command("start") & filters.private,
        )
    )
    bot.add_handler(
        CallbackQueryHandler(
            cb_credits,
            filters.regex(r"^credits$"),
        )
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
