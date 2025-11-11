# vplay_handler.py — video playback support for Telegram voice chats
import asyncio
import time
import logging
import aiohttp
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pytgcalls import PyTgCalls, MediaStream

# ========== Logging ==========
log = logging.getLogger("vplay")
logging.basicConfig(level=logging.INFO)

# ========== Initialize userbot & pytgcalls video ==========
from os import getenv

API_ID = int(getenv("API_ID", "0"))
API_HASH = getenv("API_HASH")
USERBOT_SESSION = getenv("USERBOT_SESSION")

if not (API_ID and API_HASH and USERBOT_SESSION):
    raise RuntimeError("API_ID, API_HASH, and USERBOT_SESSION must be set")

userbot_video = Client("video_userbot", api_id=API_ID, api_hash=API_HASH, session_string=USERBOT_SESSION)
vcall_py = PyTgCalls(userbot_video)

# ========== YouTube Helpers ==========
YOUTUBE_API_KEY = getenv("YOUTUBE_API_KEY")
RAPIDAPI_KEY = getenv("RAPIDAPI_KEY")

async def search_youtube_video_id(session, query: str):
    """Search YouTube and return first video ID"""
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": query,
        "key": YOUTUBE_API_KEY,
        "maxResults": 1,
        "type": "video"
    }
    async with session.get(url, params=params) as resp:
        data = await resp.json()
        items = data.get("items", [])
        if items:
            return items[0]["id"]["videoId"]
    return None


async def get_video_link_rapidapi(session, vid: str):
    """Fetch MP4 direct link via RapidAPI (YouTube MP3/MP4 downloader)"""
    url = "https://youtube-mp36.p.rapidapi.com/dl"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"
    }
    params = {"id": vid}
    for _ in range(6):
        async with session.get(url, headers=headers, params=params) as resp:
            try:
                data = await resp.json()
            except:
                data = {}
            if data.get("status") == "ok" and data.get("link"):
                return data["link"]
            await asyncio.sleep(2)
    return None


# ========== /vplay Command ==========
@userbot_video.on_message(filters.command("vplay"))
async def vplay_command(client, message):
    query = " ".join(message.command[1:])
    if not query:
        await message.reply_text("🎬 Usage: `/vplay <video name>`", parse_mode=ParseMode.MARKDOWN)
        return

    await message.reply_text(f"🔍 Searching YouTube for **{query}** ...", parse_mode=ParseMode.MARKDOWN)

    async with aiohttp.ClientSession() as session:
        vid = await search_youtube_video_id(session, query)
        if not vid:
            await message.reply_text("❌ No video found.")
            return

        yt_link = f"https://www.youtube.com/watch?v={vid}"
        await message.reply_text(f"📺 Found [YouTube Video]({yt_link})\nFetching direct MP4...", parse_mode=ParseMode.MARKDOWN)

        mp4_link = await get_video_link_rapidapi(session, vid)
        if not mp4_link:
            await message.reply_text("❌ Could not get MP4 link.")
            return

    chat_id = message.chat.id
    try:
        await vcall_py.join_group_call(
            chat_id,
            MediaStream(
                mp4_link,
                video_flags=MediaStream.Flags.ENABLE,  # enable video stream
                audio_flags=MediaStream.Flags.IGNORE
            ),
        )

        await message.reply_text(
            f"🎥 **Now Playing Video:** [{query}]({yt_link})",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False
        )

    except Exception as e:
        log.error("vplay error: %s", e)
        await message.reply_text(f"❌ Video playback failed:\n`{e}`", parse_mode=ParseMode.MARKDOWN)


# ========== Controls ==========
@userbot_video.on_message(filters.command("vstop"))
async def vstop_command(client, message):
    chat_id = message.chat.id
    try:
        await vcall_py.leave_call(chat_id)
        await message.reply_text("🛑 Video stopped.")
    except Exception as e:
        await message.reply_text(f"❌ Failed to stop video: `{e}`", parse_mode=ParseMode.MARKDOWN)


@userbot_video.on_message(filters.command("vpause"))
async def vpause_command(client, message):
    try:
        await vcall_py.pause_stream(message.chat.id)
        await message.reply_text("⏸ Video paused.")
    except Exception as e:
        await message.reply_text(f"❌ {e}")


@userbot_video.on_message(filters.command("vresume"))
async def vresume_command(client, message):
    try:
        await vcall_py.resume_stream(message.chat.id)
        await message.reply_text("▶️ Video resumed.")
    except Exception as e:
        await message.reply_text(f"❌ {e}")


# ========== Startup ==========
async def start_vplay_service():
    log.info("Starting video bot...")
    await userbot_video.start()
    await vcall_py.start()
    log.info("✅ Video PyTgCalls client started.")
    await asyncio.Event().wait()  # keep running

if __name__ == "__main__":
    asyncio.run(start_vplay_service())
