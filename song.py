# error.py — complete, Render-ready, Pyrogram + PyTgCalls (MediaStream) based music helpers
import os
import asyncio
import threading
import logging
import aiohttp
from flask import Flask
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
import time
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
# --- Compatibility handling for PyTgCalls versions ---
try:
    from pytgcalls import StreamType
    from pytgcalls.types import Update
except ImportError:
    StreamType = None
    Update = None

HAS_STREAM_END = hasattr(PyTgCalls, "on_stream_end")
HAS_AUDIO_FINISHED = hasattr(PyTgCalls, "on_audio_finished")

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("music_bot")

# -------------------------
# Environment / required
# -------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
USERBOT_SESSION = os.getenv("USERBOT_SESSION")   # session string for user account
BOT_TOKEN = os.getenv("BOT_TOKEN", None)         # optional: bot account token
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
TARGET_GROUP_ID = os.getenv("TARGET_GROUP_ID", None)  # optional group id to forward results to

if not (API_ID and API_HASH and USERBOT_SESSION):
    raise RuntimeError("Please set API_ID, API_HASH and USERBOT_SESSION environment variables.")

# -------------------------
# Spotify client (sync)
# -------------------------
try:
    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET
    ))
except Exception as e:
    log.warning("Spotify client init failed: %s", e)
    sp = None


# -------------------------
# Pyrogram and PyTgCalls clients
# -------------------------
# bot: optional bot account (helps sending messages to groups)
bot = Client("bot_account", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN) if BOT_TOKEN else None
# userbot: required for voice (user account)
userbot = Client("userbot_account", session_string=USERBOT_SESSION, api_id=API_ID, api_hash=API_HASH)
# PyTgCalls voice client attached to userbot
call_py = PyTgCalls(userbot)



music_queue = {}  # chat_id -> list of dicts for queued songs

def add_to_queue(chat_id, song_data):
    """Add song_data dict to queue for chat_id"""
    if chat_id not in music_queue:
        music_queue[chat_id] = []
    music_queue[chat_id].append(song_data)
    return len(music_queue[chat_id])

# -------------------------
# Flask app (keep alive for Render)
# -------------------------
app = Flask(__name__)

@app.route("/")
def root():
    return "deployed"

def run_flask():
    port = int(os.getenv("PORT", 5000))
    # threaded True so it doesn't block main loop
    app.run(host="0.0.0.0", port=port, threaded=True)


def format_time(seconds: float) -> str:
    secs = int(seconds)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

import re

def iso8601_to_seconds(iso: str) -> int:
    """Convert ISO-8601 duration (PT#H#M#S) → seconds."""
    if not iso:
        return 0
    try:
        m = re.match(r'^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', iso)
        if not m:
            return 0
        h = int(m.group(1) or 0)
        m_ = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        return h * 3600 + m_ * 60 + s
    except Exception:
        return 0


def get_progress_bar(elapsed: float, total: float, bar_len: int = 14) -> str:
    if total <= 0:
        return "N/A"
    frac = min(elapsed / total, 1)
    idx = int(frac * bar_len)
    left = "━" * idx
    right = "─" * (bar_len - idx - 1)
    return f"{format_time(elapsed)} {left}🦆{right} {format_time(total)}"

async def update_progress_message(chat_id, msg, start_time, total_dur, caption):
    while True:
        elapsed = time.time() - start_time
        if elapsed > total_dur:
            break
        bar = get_progress_bar(elapsed, total_dur)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏸ Pause", callback_data="pause"),
             InlineKeyboardButton("▶ Resume", callback_data="resume")],
            [InlineKeyboardButton(bar, callback_data="progress")]
        ])
        try:
            await msg.edit_caption(caption, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await asyncio.sleep(6)


# -------------------------
# YouTube / RapidAPI helpers
# -------------------------
async def search_youtube_video_id(session: aiohttp.ClientSession, query: str):
    """Return first YouTube video id for query using Google API."""
    if not YOUTUBE_API_KEY:
        return None
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": query,
        "key": YOUTUBE_API_KEY,
        "maxResults": 1,
        "type": "video",
    }
    async with session.get(url, params=params) as resp:
        if resp.status == 200:
            data = await resp.json()
            items = data.get("items")
            if items:
                return items[0]["id"]["videoId"]
    return None

async def get_mp3_url_rapidapi(session: aiohttp.ClientSession, video_id: str):
    """Use youtube-mp36 RapidAPI to get mp3 link (6 attempts)."""
    if not RAPIDAPI_KEY:
        return None
    url = "https://youtube-mp36.p.rapidapi.com/dl"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"
    }
    params = {"id": video_id}
    for attempt in range(6):
        try:
            async with session.get(url, headers=headers, params=params, timeout=20) as resp:
                # try/except for JSON parsing
                try:
                    data = await resp.json()
                except Exception:
                    data = {}
                log.debug("RapidAPI attempt %d status=%s keys=%s", attempt+1, getattr(resp, "status", None), list(data.keys()) if isinstance(data, dict) else None)
                if getattr(resp, "status", None) == 200 and data.get("status") == "ok" and data.get("link"):
                    return data["link"]
                if data.get("status") == "processing":
                    await asyncio.sleep(4)
                else:
                    await asyncio.sleep(2)
        except Exception as e:
            log.debug("RapidAPI fetch exception attempt %d: %s", attempt+1, e)
            await asyncio.sleep(2)
    return None

# -------------------------
# Command handlers
# -------------------------
@userbot.on_message(filters.command("ping"))
async def ping_userbot(_, message: Message):
    # a simple check on userbot to ensure user account is running
    await message.reply_text("userbot is online ✅")

handler_client = bot if bot else userbot

@handler_client.on_message(filters.command("song"))
async def song_command(client: Client, message: Message):
    global sp
    user_query = " ".join(message.command[1:])
    if not user_query:
        await message.reply_text("Please provide a song name after /song.")
        return

    await message.reply_text(f"Searching Spotify for '{user_query}'...")
    results = None
    for attempt in range(3):
        try:
            # Try multiple search variations to handle themes/soundtracks
            search_terms = [
                f'track:"{user_query}"',
                f'{user_query}',
                f'{user_query} soundtrack',
                f'{user_query} theme',
            ]
            for term in search_terms:
                results = sp.search(q=term, type='track', limit=5)
                tracks = results.get("tracks", {}).get("items", [])
                if tracks:
                    break
            if tracks:
                break
        except Exception as e:
            msg = f"⚠️ Spotify search error (attempt {attempt+1}): {e}"
            print(msg)
            await client.send_message(chat_id=8353079084, text=msg)
            # Recreate Spotify client and retry
            await asyncio.sleep(2)
            try:
                sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                    client_id=SPOTIFY_CLIENT_ID,
                    client_secret=SPOTIFY_CLIENT_SECRET
                ))
            except Exception as e2:
                await client.send_message(chat_id=8353079084, text=f"Reinit error: {e2}")
                await asyncio.sleep(2)
    else:
        await message.reply_text("❌ Spotify connection failed after 3 retries.")
        return

    # If still no results after all search terms, go directly to YouTube
    if not results or not results.get("tracks", {}).get("items", []):
        await message.reply_text(
            f"No Spotify results for '{user_query}'. Trying YouTube directly..."
        )
        async with aiohttp.ClientSession() as session:
            video_id = await search_youtube_video_id(session, user_query)
            if not video_id:
                await message.reply_text("Could not find anything on YouTube either.")
                return
            mp3_url = await get_mp3_url_rapidapi(session, video_id)
            if mp3_url:
                await message.reply_text(f"🎧 Found on YouTube:\n{mp3_url}")
            else:
                await message.reply_text("❌ Couldn’t fetch MP3 from YouTube.")
        return

    # pick best track (avoid remixes/covers)
    track = None
    for t in tracks:
        if "remix" not in t["name"].lower() and "cover" not in t["name"].lower():
            track = t
            break
    if not track:
        track = tracks[0]

    title = track["name"]
    artist = track["artists"][0]["name"]
    combined_query = f"{title} {artist} official audio"

    await message.reply_text(f"Found on Spotify: {title} by {artist}. Searching YouTube...")

    async with aiohttp.ClientSession() as session:
        try:
            video_id = await search_youtube_video_id(session, combined_query)
        except Exception as e:
            await client.send_message(chat_id=8353079084, text=f"YouTube search failed: {e}")
            return

        if not video_id:
            await message.reply_text("Could not find the video on YouTube.")
            return

        await message.reply_text(f"Found YouTube video (ID: {video_id}). Fetching MP3...")

        mp3_url = await get_mp3_url_rapidapi(session, video_id)
        if not mp3_url:
            await message.reply_text("❌ Could not retrieve MP3 file. See logs for details.")
            return

        await message.reply_text("✅ MP3 link received, verifying...")

        # Verify link really points to an MP3 file
        try:
            async with session.head(mp3_url, timeout=10) as head_resp:
                content_type = head_resp.headers.get("Content-Type", "")
                dbg = f"HEAD check -> status={head_resp.status}, content_type={content_type}"
                await client.send_message(chat_id=8353079084, text=dbg[:3800])

                # If it looks like a valid MP3/audio file, send link directly
                if head_resp.status == 200 and "audio" in content_type.lower():
                    group_id = -1001234567890  # replace with your group’s ID
                    msg = (
                        f"🎵 *{title}* by *{artist}*\n\n"
                        f"[▶️ Click to play or download MP3]({mp3_url})"
                    )
                    await client.send_message(
                        chat_id=group_id,
                        text=msg,
                        parse_mode="Markdown",
                        disable_web_page_preview=False
                    )
                    await message.reply_text("✅ Song link sent to group!")
                    return
        except Exception as e:
            await client.send_message(chat_id=8353079084, text=f"HEAD check error: {e}")

        # fallback if HEAD fails or not audio
        await message.reply_text(mp3_url)

@handler_client.on_message(filters.command("play"))
async def play_command(client: Client, message: Message):
    """/play <query> - find audio and play in voice chat using userbot + PyTgCalls"""
    query = " ".join(message.command[1:]).strip()
    if not query:
        await message.reply_text("Please provide a song name after /play.")
        return
    
    # Send sticker when command starts
    try:
        await message.reply_sticker("CAACAgQAAxUAAWkKLkGIixUCa-zV6uEsHsYplBD-AALCGgACLVlRUIUYbMPxtAKWNgQ")
    except Exception:
        pass



    async with aiohttp.ClientSession() as session:
        vid = await search_youtube_video_id(session, query)

        video_title = query      # fallback title
        duration_seconds = 0     # fallback duration

        if vid:
            try:
                yt_api_url = (
                    f"https://www.googleapis.com/youtube/v3/videos"
                    f"?part=snippet,contentDetails&id={vid}&key={YOUTUBE_API_KEY}"
                )
                async with session.get(yt_api_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("items")
                        if items:
                            snippet = items[0].get("snippet", {})
                            content = items[0].get("contentDetails", {})
                            video_title = snippet.get("title", query)
                            iso_dur = content.get("duration")
                            duration_seconds = iso8601_to_seconds(iso_dur)
            except Exception:
                pass

        readable_duration = format_time(duration_seconds or 0)


        if not vid:
            await message.reply_text("❌ Could not find on YouTube.")
            return
        mp3 = await get_mp3_url_rapidapi(session, vid)
        if not mp3:
            await message.reply_text("❌ Could not fetch audio link.")
            return

    chat_id = message.chat.id

    # --- Check if a song is already playing ---
    try:
        active_calls_dict = call_py.calls
        if asyncio.iscoroutine(active_calls_dict):
            active_calls_dict = await active_calls_dict
        active_chats = list(getattr(active_calls_dict, "keys", lambda: [])())
    except Exception:
        active_chats = []


    if chat_id in active_chats:
        song_data = {
            "title": video_title,
            "url": mp3,
            "vid": vid,
            "user": message.from_user,
            "duration": duration_seconds or 180,

        }
        position = add_to_queue(chat_id, song_data)

        await message.reply_text(
            f"<b>➜ Added to queue at</b> <u>#{position}</u>\n\n"
            f"<b>‣ Title:</b> <i>{video_title}</i>\n"
            f"<b>‣ Duration:</b> <u>{readable_duration}</u>\n"

            f"<b>‣ Requested by:</b> <a href='tg://user?id={message.from_user.id}'>{message.from_user.first_name}</a>",
            parse_mode=ParseMode.HTML,
        )
        return


    try:
        # call_py.play expects (chat_id, MediaStream(...))
        log.info("Attempting to play in chat %s stream=%s", chat_id, mp3)
        try:
            await call_py.play(chat_id, MediaStream(mp3, video_flags=MediaStream.Flags.IGNORE))
        except Exception as e:
            if "FLOOD_WAIT" in str(e):
                await message.reply_text("🚫 Telegram asked to wait a bit before joining the voice chat. Try again in a minute.")
            elif "INTERDC_X_CALL_RICH_ERROR" in str(e):
                await message.reply_text("⚠️ Telegram servers are having trouble connecting the voice call. Please try again later.")
            else:
                await message.reply_text(f"❌ Voice playback error:\n<code>{e}</code>", parse_mode="html")
            return

        caption = (
            "<blockquote>"
            "<b>🎧 <u>hulalala Streaming (Local Playback)</u></b>\n\n"
            f"<b>❍ Title:</b> <i>{video_title}</i>\n"
            f"<b>❍ Requested by:</b> <a href='tg://user?id={message.from_user.id}'><u>{message.from_user.first_name}</u></a>"
            "</blockquote>"
        )

        bar = get_progress_bar(0, duration_seconds or 180)  # rough placeholder, 3 min default
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏸ Pause", callback_data="pause"),
            InlineKeyboardButton("▶ Resume", callback_data="resume"),
            InlineKeyboardButton("⏭ Skip", callback_data="skip")],
            [InlineKeyboardButton(bar, callback_data="progress")]
        ])

        # Build YouTube thumbnail from video id
        thumb_url = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"

        msg = await message.reply_photo(
            photo=thumb_url,
            caption=caption,
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )


        # kick off progress updater
        asyncio.create_task(update_progress_message(message.chat.id, msg, time.time(), duration_seconds or 180, caption))
        # start fallback auto-next timer
        asyncio.create_task(auto_next_timer(chat_id, duration_seconds or 180))


    except Exception as e:
        log.exception("Failed to join voice chat / play: %s", e)
        await message.reply_text(f"❌ Voice playback error: {e}")



async def handle_next_in_queue(chat_id: int):
    if chat_id in music_queue and music_queue[chat_id]:
        next_song = music_queue[chat_id].pop(0)
        try:
            # ✅ Instead of leaving VC, just change the stream
            if hasattr(call_py, "change_stream"):
                await call_py.change_stream(chat_id, MediaStream(next_song["url"], video_flags=MediaStream.Flags.IGNORE))
            elif hasattr(call_py, "play"):
                # fallback for older PyTgCalls builds
                await call_py.play(chat_id, MediaStream(next_song["url"], video_flags=MediaStream.Flags.IGNORE))
            else:
                raise Exception("No compatible stream change method found.")

            caption = (
                "<blockquote>"
                "<b>🎧 <u>hulalala Streaming (Auto Next)</u></b>\n\n"
                f"<b>❍ Title:</b> <i>{next_song['title']}</i>\n"
                f"<b>❍ Requested by:</b> "
                f"<a href='tg://user?id={next_song['user'].id}'>"
                f"<u>{next_song['user'].first_name}</u></a>"
                "</blockquote>"
            )

            bar = get_progress_bar(0, next_song["duration"])
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏸ Pause", callback_data="pause"),
                 InlineKeyboardButton("▶ Resume", callback_data="resume"),
                 InlineKeyboardButton("⏭ Skip", callback_data="skip")],
                [InlineKeyboardButton(bar, callback_data="progress")]
            ])
            thumb = f"https://img.youtube.com/vi/{next_song['vid']}/hqdefault.jpg"
            msg = await bot.send_photo(chat_id, thumb, caption=caption, reply_markup=kb, parse_mode=ParseMode.HTML)

            # Start progress updater + auto next
            asyncio.create_task(update_progress_message(chat_id, msg, time.time(), next_song["duration"], caption))
            asyncio.create_task(auto_next_timer(chat_id, next_song["duration"] or 180))

        except Exception as e:
            await bot.send_message(chat_id, f"⚠️ Could not auto-play next queued song:\n<code>{e}</code>", parse_mode=ParseMode.HTML)
    else:
        # 🧹 Leave VC only when queue is empty
        if chat_id in music_queue:
            music_queue.pop(chat_id, None)
        try:
            if hasattr(call_py, "leave_call"):
                await call_py.leave_call(chat_id)
            elif hasattr(call_py, "stop"):
                await call_py.stop(chat_id)
        except Exception:
            pass
        await bot.send_message(chat_id, "✅ <b>Queue finished and cleared.</b>", parse_mode=ParseMode.HTML)



# --- Event bindings (timer-based fallback for PyTgCalls builds without stream_end) ---
async def auto_next_timer(chat_id: int, duration: int):
    """Fallback timer to trigger next song after duration."""
    await asyncio.sleep(duration)
    await handle_next_in_queue(chat_id)

# When playing a song, we’ll start this timer
# Modify handle_next_in_queue to start a timer too


@handler_client.on_message(filters.command("mpause"))
async def mpause_command(client, message: Message):
    user = await client.get_chat_member(message.chat.id, message.from_user.id)
    if not (user.privileges or user.status in ("administrator", "creator")):
        await message.reply_text("❌ You need to be an admin to use this command.")
        return
    try:
        await call_py.pause(message.chat.id)
        await message.reply_text("⏸ Paused the stream.")
    except Exception as e:
        await message.reply_text(f"❌ Failed to pause.\n{e}")

@handler_client.on_message(filters.command("mresume"))
async def mresume_command(client, message: Message):
    user = await client.get_chat_member(message.chat.id, message.from_user.id)
    if not (user.privileges or user.status in ("administrator", "creator")):
        await message.reply_text("❌ You need to be an admin to use this command.")
        return
    try:
        await call_py.resume(message.chat.id)
        await message.reply_text("▶️ Resumed the stream.")
    except Exception as e:
        await message.reply_text(f"❌ Failed to resume.\n{e}")

@handler_client.on_message(filters.command("skip"))
async def skip_command(client, message: Message):
    user = await client.get_chat_member(message.chat.id, message.from_user.id)
    if not (user.privileges or user.status in ("administrator", "creator")):
        await message.reply_text(
            "❌ <b>You need to be an admin to use this command.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = message.chat.id
    try:
        # Stop current stream safely
        if hasattr(call_py, "stop_stream"):
            await call_py.stop_stream(chat_id)
        elif hasattr(call_py, "leave_call"):
            await call_py.leave_call(chat_id)
        else:
            await call_py.stop(chat_id)

        await message.reply_text("⏭ <b>Skipped current song.</b>", parse_mode=ParseMode.HTML)

        # ✅ Immediately play the next song in queue
        await handle_next_in_queue(chat_id)

    except Exception as e:
        await message.reply_text(
            f"❌ <b>Failed to skip:</b> <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )


@handler_client.on_message(filters.command("clear"))
async def clear_queue(client, message: Message):
    chat_id = message.chat.id
    user = await client.get_chat_member(chat_id, message.from_user.id)
    if not (user.privileges or user.status in ("administrator", "creator")):
        await message.reply_text(
            "❌ <b>You need to be an admin to use this command.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if chat_id in music_queue:
        count = len(music_queue[chat_id])
        music_queue.pop(chat_id, None)
        await message.reply_text(f"🧹 <b>Cleared {count} song(s) from the queue.</b>", parse_mode=ParseMode.HTML)
    else:
        await message.reply_text("⚠️ <b>No queued songs to clear.</b>", parse_mode=ParseMode.HTML)

# ==============================
# Extra: Seek, Seekback, and Ping
# ==============================
import subprocess
from datetime import datetime

MODS = [8353079084]  # add your Telegram user ID(s) here
BOT_START_TIME = time.time()

def parse_duration_str(duration_str):
    """Convert duration like '3:25' or '00:03:25' into seconds."""
    parts = duration_str.split(':')
    parts = [int(p) for p in parts]
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    elif len(parts) == 2:
        m, s = parts
        return m * 60 + s
    elif len(parts) == 1:
        return parts[0]
    return 0


async def restart_with_seek(chat_id: int, seek_pos: int, message: Message):
    if chat_id not in music_queue or not music_queue[chat_id]:
        await message.reply("❌ Nothing is playing.")
        return

    current_song = music_queue[chat_id][0]
    try:
        await call_py.leave_call(chat_id)

        media_path = current_song["url"]
        trimmed_path = f"seeked_{chat_id}.mp3"

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(seek_pos),
            "-i", media_path,
            "-acodec", "copy",
            trimmed_path
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

        await call_py.play(chat_id, MediaStream(trimmed_path, video_flags=MediaStream.Flags.IGNORE))
        current_song["start_time"] = time.time() - seek_pos

        await message.reply(f"⏩ Seeked to {format_time(seek_pos)} in **{current_song['title']}**")

    except Exception as e:
        await message.reply(f"❌ Failed to seek.\nError: {str(e)}")


@handler_client.on_message(filters.group & filters.command("seek"))
async def seek_handler(client, message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("❌ Usage: /seek <seconds>")
        return

    chat_id = message.chat.id
    if chat_id not in music_queue or not music_queue[chat_id]:
        await message.reply("❌ Nothing is playing.")
        return

    seconds = int(args[1])
    song = music_queue[chat_id][0]
    elapsed = int(time.time() - song.get("start_time", time.time()))
    seek_pos = elapsed + seconds

    duration = int(song.get("duration", 0))
    if seek_pos >= duration:
        seek_pos = duration
    await restart_with_seek(chat_id, seek_pos, message)


@handler_client.on_message(filters.group & filters.command("seekback"))
async def seekback_handler(client, message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("❌ Usage: /seekback <seconds>")
        return

    chat_id = message.chat.id
    if chat_id not in music_queue or not music_queue[chat_id]:
        await message.reply("❌ Nothing is playing.")
        return

    seconds = int(args[1])
    song = music_queue[chat_id][0]
    elapsed = int(time.time() - song.get("start_time", time.time()))
    seek_pos = max(0, elapsed - seconds)

    await restart_with_seek(chat_id, seek_pos, message)


# ==============================
# Clear queue when VC ends
# ==============================
@call_py.on_stream_end()
async def on_stream_end_handler(_, update):
    chat_id = update.chat_id
    if chat_id in music_queue:
        music_queue.pop(chat_id, None)
    try:
        await call_py.leave_call(chat_id)
    except Exception:
        pass
    await bot.send_message(chat_id, "✅ Voice chat ended — queue cleared.", parse_mode="HTML")


# ==============================
# Ping command for MODS only
# ==============================
@handler_client.on_message(filters.command("ping"))
async def ping_command(client, message: Message):
    user_id = message.from_user.id
    if user_id not in MODS:
        return

    start = datetime.now()
    msg = await message.reply_text("📡 Pinging...")
    end = datetime.now()

    latency = (end - start).total_seconds()
    uptime = datetime.now() - datetime.fromtimestamp(BOT_START_TIME)
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    await msg.edit_text(
        f"<b>Pong!</b> <code>{latency:.2f}s</code>\n"
        f"<b>Uptime</b> - <code>{days}d {hours}h {minutes}m {seconds}s</code>\n"
        f"<b>Bot of</b> <a href='https://t.me/PraiseTheFraud'>F U R Y</a>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )


@handler_client.on_callback_query()
async def callback_handler(client, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    data = cq.data

    if data == "pause":
        try:
            await call_py.pause(chat_id)
            await cq.answer("⏸ Paused playback.")
        except Exception as e:
            await cq.answer(f"Error: {e}", show_alert=True)

    elif data == "resume":
        try:
            await call_py.resume(chat_id)
            await cq.answer("▶ Resumed playback.")
        except Exception as e:
            await cq.answer(f"Error: {e}", show_alert=True)

    elif data == "skip":
        try:
            if hasattr(call_py, "stop_stream"):
                await call_py.stop_stream(chat_id)
            elif hasattr(call_py, "leave_call"):
                await call_py.leave_call(chat_id)
            else:
                await call_py.stop(chat_id)

            await cq.answer("⏭ Skipping current song...")
        except Exception as e:
            await cq.answer(f"Error: {e}", show_alert=True)


    else:
        await cq.answer()

# -------------------------
# Startup / shutdown helpers
# -------------------------
async def _startup():
    """Start userbot (required for PyTgCalls), start PyTgCalls, then bot (optional)."""
    log.info("Starting userbot client...")
    await userbot.start()
    log.info("Starting PyTgCalls client...")
    await call_py.start()
    if bot:
        log.info("Starting bot client...")
        await bot.start()
    log.info("Startup complete.")

async def _shutdown():
    log.info("Shutting down services...")
    try:
        await call_py.stop()
    except Exception:
        pass
    try:
        await userbot.stop()
    except Exception:
        pass
    if bot:
        try:
            await bot.stop()
        except Exception:
            pass
    log.info("Shutdown finished.")

async def main_loop():
    await _startup()
    # keep running until interrupted
    await idle()
    # idle exits on stop, then perform shutdown
    await _shutdown()

# ================================
#   Docker / Render-safe startup
# ================================
import threading
import asyncio
import signal
import traceback

def start_flask():
    """Run Flask keepalive webserver in background thread."""
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("🌐 Flask webserver started in background thread.")

async def start_services():
    """Start Pyrogram userbot + bot + PyTgCalls safely, and keep idle loop."""
    try:
        log.info("🚀 Initializing clients...")
        await userbot.start()
        log.info("[Userbot] connected.")
        await call_py.start()
        log.info("[PyTgCalls] ready.")
        if bot:
            await bot.start()
            log.info("[Bot] started.")

        # Background idle
        log.info("✅ All clients started. Entering idle mode...")
        await idle()

    except Exception as e:
        log.error("❌ Startup error: %s", e)
        traceback.print_exc()
    finally:
        log.info("🔻 Shutting down...")
        try:
            await call_py.stop()
        except Exception:
            pass
        try:
            await userbot.stop()
        except Exception:
            pass
        if bot:
            try:
                await bot.stop()
            except Exception:
                pass
        log.info("🟢 Clean shutdown complete.")


def main():
    """Entry point for Docker / Render deployment."""
    start_flask()  # non-blocking webserver thread
    loop = asyncio.get_event_loop()

    # handle shutdown signals gracefully
    stop_event = asyncio.Event()

    def stop_handler(*_):
        loop.create_task(stop_event.set())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_handler)

    # run startup + main service
    loop.create_task(start_services())
    loop.run_until_complete(stop_event.wait())
    log.info("🛑 Received shutdown signal, exiting...")

if __name__ == "__main__":
    main()

