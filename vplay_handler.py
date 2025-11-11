# vplay_handler.py
import os
import asyncio
import aiohttp
import logging
from pyrogram import Client, filters
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
from pyrogram.enums import ParseMode

# Import your shared userbot from error.py
from error import userbot, search_youtube_video_id, RAPIDAPI_KEY, YOUTUBE_API_KEY

# Optional: use a second PyTgCalls instance just for video
call_video = PyTgCalls(userbot)

log = logging.getLogger("vplay")

async def get_video_url_rapidapi(session, video_id: str):
    """Fetch downloadable MP4 video link via RapidAPI or fallback."""
    url = "https://youtube-mp36.p.rapidapi.com/dl"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"
    }
    params = {"id": video_id}
    async with session.get(url, headers=headers, params=params) as resp:
        try:
            data = await resp.json()
            return data.get("link") or data.get("url")
        except Exception:
            return None

@Client.on_message(filters.command("vplay"))
async def vplay_command(client: Client, message):
    """Play a YouTube video (with video + audio) in VC."""
    chat_id = message.chat.id
    query = " ".join(message.command[1:]).strip()

    if not query:
        await message.reply_text("❌ Usage: /vplay <video name>")
        return

    status = await message.reply_text(f"🎬 Searching YouTube for **{query}**...")

    async with aiohttp.ClientSession() as session:
        video_id = await search_youtube_video_id(session, query)
        if not video_id:
            await status.edit_text("❌ No video found on YouTube.")
            return

        # Try getting downloadable video URL
        video_url = await get_video_url_rapidapi(session, video_id)
        if not video_url:
            await status.edit_text("❌ Could not get a playable link.")
            return

        # Prepare video info
        title = query.title()
        thumb = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

        # Notify user
        await status.edit_text(
            f"🎞 **Streaming video:** {title}\n"
            f"👤 Requested by: {message.from_user.mention}",
            parse_mode=ParseMode.MARKDOWN
        )

        try:
            # Start streaming (video + audio)
            await call_video.play(
                chat_id,
                MediaStream(video_url)
            )

            await message.reply_photo(
                photo=thumb,
                caption=f"🎥 <b>Now Playing:</b> <i>{title}</i>\n"
                        f"👤 Requested by <b>{message.from_user.first_name}</b>",
                parse_mode=ParseMode.HTML
            )

        except Exception as e:
            await message.reply_text(f"❌ Video stream error:\n<code>{e}</code>", parse_mode=ParseMode.HTML)
            log.error(f"vplay error: {e}")
