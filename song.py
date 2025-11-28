# error.py — complete, Render-ready, Pyrogram + PyTgCalls (MediaStream) based music helpers
import os
import tempfile
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
from bs4 import BeautifulSoup
from pyrogram.enums import ChatAction

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
MODS = [8353079084, 8355303766]  # your Telegram ID(s)
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")  # Genius API token from environment

if not (API_ID and API_HASH and USERBOT_SESSION):
    raise RuntimeError("Please set API_ID, API_HASH and USERBOT_SESSION environment variables.")

# -------------------------
# Pyrogram and PyTgCalls clients
# -------------------------
# bot: optional bot account (helps sending messages to groups)
bot = Client("bot_account", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN) if BOT_TOKEN else None
# userbot: required for voice (user account)
userbot = Client("userbot_account", session_string=USERBOT_SESSION, api_id=API_ID, api_hash=API_HASH)
# PyTgCalls voice client attached to userbot
call_py = PyTgCalls(userbot)




# ======================================
# CORRECT QUEUE MODEL
# ======================================
current_song = {}      # chat_id -> dict (NOW PLAYING)
music_queue = {}       # chat_id -> list of next songs


def add_to_queue(chat_id, song):
    """Add next song after current one."""
    if chat_id not in music_queue:
        music_queue[chat_id] = []
    music_queue[chat_id].append(song)
    return len(music_queue[chat_id])   # return queue position (1-based)



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


import yt_dlp
import asyncio
import tempfile
import os
from functools import partial

# -------------------------
# Caption helpers
# -------------------------
def parse_artist_and_title(query: str):
    """
    Try to extract (artist, title) from user query.
    Patterns handled:
      - "Artist - Title"
      - "Title - Artist"
      - "Title by Artist"
    Fallback: artist = "Unknown Artist", title = query
    """
    q = query.strip()
    # Try "Artist - Title" or "Title - Artist"
    if " - " in q:
        left, right = q.split(" - ", 1)
        # Heuristic: if left looks like person (contains spaces) assume artist-left
        # Default to (artist, title) = (left, right)
        return left.strip(), right.strip()
    # Try "Title by Artist"
    if " by " in q.lower():
        parts = q.rsplit(" by ", 1)
        if len(parts) == 2:
            title, artist = parts
            return artist.strip(), title.strip()
    # Fallback
    return "Unknown Artist", q

def generate_song_bio(artist: str, title: str):
    """
    Small, safe generic bio template. You may replace this with a call to a real API
    (Spotify/Last.fm/Discogs) if you want accurate bios.
    """
    # Keep bio neutral and generic so we don't assert incorrect facts
    bio = (
        f'{artist}’s \"{title}\" explores emotional themes and textures — '
        "a track that resonated with listeners online. "
        "The artist teased this song several times on their socials."
    )
    return bio

def build_caption_html(artist: str, title: str, bio: str, include_emoji: bool = True):
    """
    Return HTML-formatted caption using bold/italic/underline combos.
    Safe for ParseMode.HTML in Pyrogram — and for parse_mode='HTML' in PTB.
    Example output:
      <b><i><u>artist- "title"</u></i></b>\n\n<b><u>song bio-</u></b> "<i>bio</i>"
    """
    header = "🎵 " if include_emoji else ""
    artist_title = f'{artist}- "{title}"'
    caption = (
        f"{header}<b><i><u>{artist_title}</u></i></b>\n\n"
        f"<b><u>song bio-</u></b> \"<i>{bio}</i>\""
    )
    return caption


async def rapid_youtube_search(session, query: str):
    url = "https://youtube-search-results.p.rapidapi.com/youtube-search/"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "youtube-search-results.p.rapidapi.com"
    }
    params = {"q": query}

    async with session.get(url, headers=headers, params=params) as r:
        if r.status != 200:
            return None
        data = await r.json()

    videos = data.get("items", [])
    for item in videos:
        if item.get("type") == "video":
            return item.get("id")  # videoId

    return None



from urllib.parse import quote

async def genius_search(query):
    if not GENIUS_TOKEN:
        return None

    url = f"https://api.genius.com/search?q={quote(query)}"
    headers = {"Authorization": f"Bearer {GENIUS_TOKEN}"}

    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            if r.status != 200:
                return None
            data = await r.json()

    hits = data.get("response", {}).get("hits", [])
    if not hits:
        return None

    # Try first 5 hits
    for h in hits[:5]:
        link = h["result"].get("url")
        if link:
            return link

    return None


async def scrape_lyrics(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            if r.status != 200:
                return None
            html = await r.text()

    soup = BeautifulSoup(html, "html.parser")

    # NEW Genius layout
    blocks = soup.find_all("div", {"data-lyrics-container": "true"})
    
    if not blocks:
        # OLD Genius layout
        old = soup.find("div", class_="lyrics")
        if old:
            return old.get_text("\n").strip()
        return None

    lines = []

    for block in blocks:
        raw = block.get_text("\n").strip()
        for line in raw.split("\n"):
            line = line.strip()

            if not line:
                lines.append("")
                continue

            if line.startswith("[") and line.endswith("]"):
                continue

            bad = ["contributors", "translation", "read more", "lyrics"]
            if any(b in line.lower() for b in bad):
                continue

            lines.append(line)

    final = "\n".join(lines).strip()
    return final




async def genius_bio(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            if r.status != 200:
                return None
            html = await r.text()

    soup = BeautifulSoup(html, "html.parser")

    desc = soup.find("div", class_=lambda c: c and "SongDescription__Content" in c)
    if desc:
        txt = desc.get_text(" ").strip()
        return txt if txt else None

    return None



async def html_youtube_first(query: str):
    import aiohttp, re
    url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            html = await r.text()

    # find first video-id
    match = re.search(r"watch\?v=([A-Za-z0-9_-]{11})", html)

    if match:
        return match.group(1)
    return None


def ytdlp_search_and_download_nocookie(query, out_dir):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": False,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearchdate1:{query}", download=True)
        entry = info["entries"][0] if "entries" in info else info
        filename = ydl.prepare_filename(entry)
        return filename, entry


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


async def download_with_progress(session, url, progress_msg):
    async with session.get(url) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunks = []
        last_edit = time.time()

        async for chunk in resp.content.iter_chunked(1024 * 128):
            downloaded += len(chunk)
            chunks.append(chunk)

            percent = int((downloaded / total) * 100) if total else 0

            # update only every 1.5 seconds
            if time.time() - last_edit > 1.5:
                last_edit = time.time()
                try:
                    await progress_msg.edit_text(
                        f"<b><u>Retrieving data… {percent}%</u></b>",
                        parse_mode=ParseMode.HTML
                    )
                except:
                    pass

        return b"".join(chunks)


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
handler_client = bot if bot else userbot


@handler_client.on_message(filters.command("lyrics"))
async def lyrics_cmd(client, message):
    if len(message.command) < 2:
        return await message.reply_text(
            "Usage: <b>/lyrics song name</b>",
            parse_mode=ParseMode.HTML
        )

    query = " ".join(message.command[1:])
    await message.reply_chat_action(ChatAction.TYPING)

    url = await genius_search(query)

    if not url:
        return await message.reply_text(
            "❌ No lyrics found.",
            parse_mode=ParseMode.HTML
        )

    lyrics = await scrape_lyrics(url)
    if not lyrics:
        return await message.reply_text(
            "❌ Could not extract lyrics.",
            parse_mode=ParseMode.HTML
        )

    artist, title = parse_artist_and_title(query)
    header_caption = f"🎵 <b>{artist} — \"{title}\"</b>"

    if len(lyrics) <= 4096:
        return await message.reply_text(f"{header_caption}\n\n{lyrics}", parse_mode=ParseMode.HTML)

    for i in range(0, len(lyrics), 4096):
        await message.reply_text(lyrics[i:i+4096], parse_mode=ParseMode.HTML)





# -------------------------
# Command handlers
# -------------------------
@userbot.on_message(filters.command("ping"))
async def ping_userbot(_, message: Message):
    user_id = message.from_user.id
    if user_id not in MODS:
        return
    # a simple check on userbot to ensure user account is running
    await message.reply_text("userbot is online ✅")


@handler_client.on_message(filters.command("song"))
async def song_command(client: Client, message: Message):
    ADMIN = 8353079084

    import tempfile
    import os
    import time

    # helper to build single-line step message (only one bullet visible)
    def _single_step_text(step_num: int, total_steps: int, text: str):
        header = "<b><u>Processing Request</u></b>\n\n"
        return header + f"• Step {step_num}/{total_steps}: {text}"

    # Safe edit helper
    async def safe_edit(msg_obj, new_text, parse_mode=ParseMode.HTML, min_interval=1.0, last_edit_time_holder=None):
        try:
            now = time.time()
            if last_edit_time_holder is not None:
                last = last_edit_time_holder[0]
                wait = max(0, min_interval - (now - last))
                if wait > 0:
                    await asyncio.sleep(wait)
            await msg_obj.edit_text(new_text, parse_mode=parse_mode)
            if last_edit_time_holder is not None:
                last_edit_time_holder[0] = time.time()
        except:
            pass

    user_query = " ".join(message.command[1:])
    if not user_query:
        await message.reply_text("Please provide a song name after /song.")
        return

    # create progress message
    progress_msg = await message.reply_text(_single_step_text(1, 6, "Searching…"), parse_mode=ParseMode.HTML)
    last_edit_ref = [time.time()]

    # send debug to admin
    await client.send_message(ADMIN, f"YT-Only Search: '{user_query}'")

    # ------- Step 1: Search YouTube (HTML) -------
    await safe_edit(progress_msg, _single_step_text(1, 6, "Finding best match…"), ParseMode.HTML, last_edit_time_holder=last_edit_ref)

    video_id = await html_youtube_first(user_query)
    if not video_id:
        await safe_edit(progress_msg, _single_step_text(1, 6, "❌ No matching video found."), ParseMode.HTML, last_edit_time_holder=last_edit_ref)
        return

    await client.send_message(ADMIN, f"Found video_id = {video_id}")

    # ---------- Step 2: Use the video_id we already found ----------
    await safe_edit(progress_msg, _single_step_text(2, 6, "Preparing audio source…"), ParseMode.HTML, last_edit_time_holder=last_edit_ref)

    video_id = video_id  # keep same ID found earlier
    thumb_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


    await client.send_message(ADMIN, f"Using HTML-found video_id = {video_id}")

    async with aiohttp.ClientSession() as session:

        # Step 3: Get MP3 link
        await safe_edit(progress_msg, _single_step_text(3, 6, "Fetching audio file…"), ParseMode.HTML, last_edit_time_holder=last_edit_ref)

        mp3_url = await get_mp3_url_rapidapi(session, video_id)
        if not mp3_url:
            await safe_edit(progress_msg, _single_step_text(3, 6, "❌ MP3 link not found."), ParseMode.HTML, last_edit_time_holder=last_edit_ref)
            return

        await client.send_message(ADMIN, f"RapidAPI MP3 link OK")

        # Step 4: Download MP3
        await safe_edit(progress_msg, _single_step_text(4, 6, "Retrieving data… 0%"), ParseMode.HTML, last_edit_time_holder=last_edit_ref)

        try:
            async with session.get(mp3_url) as resp:
                mp3_bytes = await resp.read()

        except Exception as e:
            await client.send_message(ADMIN, f"Download error: {e}")
            await safe_edit(progress_msg, _single_step_text(4, 6, "❌ Download failed."), ParseMode.HTML, last_edit_time_holder=last_edit_ref)
            return

        # Step 5: Save to temp
        await safe_edit(progress_msg, _single_step_text(5, 6, "Finalizing audio…"), ParseMode.HTML, last_edit_time_holder=last_edit_ref)

        fd, temp_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        with open(temp_path, "wb") as f:
            f.write(mp3_bytes)

        # Step 6: Upload
        await safe_edit(progress_msg, _single_step_text(6, 6, "Sending audio…"), ParseMode.HTML, last_edit_time_holder=last_edit_ref)

        try:
            # ----- Download thumbnail (local file required) -----
            thumb_path = None
            try:
                async with session.get(thumb_url) as t:
                    if t.status == 200:
                        thumb_bytes = await t.read()
                        fd2, thumb_path = tempfile.mkstemp(suffix=".jpg")
                        os.close(fd2)
                        with open(thumb_path, "wb") as f:
                            f.write(thumb_bytes)
            except:
                thumb_path = None

            # ----- Upload audio with or without thumbnail -----
            # build caption
            artist, title = parse_artist_and_title(user_query)
            title = user_query

            search_url = await genius_search(f"{title} {artist}")
            bio = await genius_bio(search_url) if search_url else None

            if bio:
                caption = (
                    f"🎵 <b><u>{artist} - \"{title}\"</u></b>\n\n"
                    f"<b><u>Song Bio:</u></b>\n<i>{bio}</i>"
                )
            else:
                caption = f"🎵 <b><u>{artist} - \"{title}\"</u></b>"



            
            await client.send_audio(
                chat_id=message.chat.id,
                audio=temp_path,
                thumb=thumb_path if thumb_path else None,
                caption=caption,
                parse_mode=ParseMode.HTML,
                file_name=f"{title}.mp3"
            )



                

            # cleanup thumbnail
            try:
                if thumb_path:
                    os.remove(thumb_path)
            except:
                pass


        except Exception as e:
            await client.send_message(ADMIN, f"Upload error: {e}")
            await safe_edit(progress_msg, _single_step_text(6, 6, "❌ Upload failed."), ParseMode.HTML, last_edit_time_holder=last_edit_ref)
        finally:
            try: os.remove(temp_path)
            except: pass

        try: await progress_msg.delete()
        except: pass




@handler_client.on_message(filters.reply & filters.command("play"))
async def play_replied_audio(client, message):
    replied = message.reply_to_message
    chat_id = message.chat.id

    if not replied.audio:
        return await message.reply_text("❌ Reply to an audio file only!")

    audio = replied.audio
    file_id = audio.file_id
    title = audio.title or audio.file_name or "Unknown Title"
    duration = audio.duration or 180

    try:
        file_path = await replied.download()

        await call_py.play(
            chat_id,
            MediaStream(
                file_path,
                video_flags=MediaStream.Flags.IGNORE
            )
        )



    except Exception as e:
        return await message.reply_text(
            f"❌ Playback failed:\n<code>{e}</code>",
            parse_mode=ParseMode.HTML
        )

    current_song[chat_id] = {
        "title": title,
        "url": file_id,
        "vid": None,
        "user": message.from_user,
        "duration": duration,
        "start_time": time.time()
    }

    artist = audio.performer or "Unknown Artist"
    title = audio.title or "Unknown Title"

    caption = f"🎵 <b>{artist} — \"{title}\"</b>"

    await message.reply_text(
        f"{caption}\n\n<b>🎧 Streaming replied audio</b>",
        parse_mode=ParseMode.HTML
    )



@handler_client.on_message(filters.command("play"))
async def play_command(client: Client, message: Message):
    """/play <query> - SAME SEARCH RESULT AS /song"""
    query = " ".join(message.command[1:]).strip()
    if not query:
        await message.reply_text("Please provide a song name after /play.")
        return
    
    # Send sticker when command starts
    try:
        await message.reply_sticker("CAACAgQAAxUAAWkPQRUy37GVR42R2w26sKQx4FKBAAKrGQACQwl4UJ1u2xb-mMqINgQ")
    except Exception:
        pass

    # ─────────────────────────────────────────
    # 🔍 STEP 1 — HTML YouTube Search (Same as /song)
    # ─────────────────────────────────────────
    async with aiohttp.ClientSession() as session:

        vid = await html_youtube_first(query)
        if not vid:
            await message.reply_text("❌ No matching YouTube results.")
            return

        # Thumbnail
        thumb_url = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"

        # ─────────────────────────────────────────
        # 🎧 STEP 2 — Fetch MP3 (Same as /song)
        # ─────────────────────────────────────────
        mp3 = await get_mp3_url_rapidapi(session, vid)
        if not mp3:
            await message.reply_text("❌ Could not fetch audio link.")
            return

        # ─────────────────────────────────────────
        # 🏷️ STEP 3 — Fetch Title + Duration (Optional)
        # ─────────────────────────────────────────
        video_title = query
        duration_seconds = 0

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
        except:
            pass

    readable_duration = format_time(duration_seconds or 0)
    chat_id = message.chat.id

    # ─────────────────────────────────────────
    # 🎵 CHECK IF SOMETHING IS ALREADY PLAYING (QUEUE)
    # ─────────────────────────────────────────
    try:
        active_calls_dict = call_py.calls
        if asyncio.iscoroutine(active_calls_dict):
            active_calls_dict = await active_calls_dict
        active_chats = list(getattr(active_calls_dict, "keys", lambda: [])())
    except Exception:
        active_chats = []

    if chat_id in current_song:
        pos = add_to_queue(chat_id, {
            "title": video_title,
            "url": mp3,
            "vid": vid,
            "user": message.from_user,
            "duration": duration_seconds or 180
        })

        await message.reply_text(
            f"<b>➜ Added to queue at</b> <u>#{pos}</u>\n\n"
            f"<b>‣ Title:</b> <i>{video_title}</i>\n"
            f"<b>‣ Duration:</b> <u>{readable_duration}</u>\n"
            f"<b>‣ Requested by:</b> <a href='tg://user?id={message.from_user.id}'>{message.from_user.first_name}</a>",
            parse_mode=ParseMode.HTML,
        )
        return


    # ─────────────────────────────────────────
    # ▶ PLAY NOW IN VC
    # ─────────────────────────────────────────
    try:
        await call_py.play(
            chat_id,
            MediaStream(
                mp3,
                video_flags=MediaStream.Flags.IGNORE
            )
        )


        current_song[chat_id] = {
            "title": video_title,
            "url": mp3,
            "vid": vid,
            "user": message.from_user,
            "duration": duration_seconds or 180,
            "start_time": time.time()
        }


        caption = (
            "<blockquote>"
            "<b>🎧 <u>hulalala Streaming (Local Playback)</u></b>\n\n"
            f"<b>❍ Title:</b> <i>{video_title}</i>\n"
            f"<b>❍ Requested by:</b> "
            f"<a href='tg://user?id={message.from_user.id}'><u>{message.from_user.first_name}</u></a>"
            "</blockquote>"
        )

        bar = get_progress_bar(0, duration_seconds or 180)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏸ Pause", callback_data="pause"),
             InlineKeyboardButton("▶ Resume", callback_data="resume"),
             InlineKeyboardButton("⏭ Skip", callback_data="skip")],
            [InlineKeyboardButton(bar, callback_data="progress")]
        ])

        msg = await message.reply_photo(
            photo=thumb_url,
            caption=caption,
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

        # Start progress ui
        asyncio.create_task(update_progress_message(chat_id, msg, time.time(), duration_seconds or 180, caption))
        asyncio.create_task(auto_next_timer(chat_id, duration_seconds or 180))

    except Exception as e:
        await message.reply_text(f"❌ Voice playback error:\n<code>{e}</code>", parse_mode=ParseMode.HTML)







async def handle_next(chat_id):
    # no songs in queue
    if chat_id not in music_queue or not music_queue[chat_id]:
        current_song.pop(chat_id, None)
        music_queue.pop(chat_id, None)

        try:
            await call_py.leave_call(chat_id)
        except:
            pass

        await bot.send_message(chat_id, "✅ Queue finished and cleared.", parse_mode=ParseMode.HTML)
        return

    # get next song
    next_song = music_queue[chat_id].pop(0)
    current_song[chat_id] = next_song
    next_song["start_time"] = time.time()

    try:
        # switch stream
        if hasattr(call_py, "change_stream"):
            await call_py.change_stream(chat_id, MediaStream(next_song["url"], video_flags=MediaStream.Flags.IGNORE))
        else:
            await call_py.play(chat_id, MediaStream(next_song["url"], video_flags=MediaStream.Flags.IGNORE))

        thumb = f"https://img.youtube.com/vi/{next_song['vid']}/hqdefault.jpg"
        caption = (
            "<blockquote>"
            "<b>🎧 <u>Now Playing</u></b>\n\n"
            f"<b>❍ Title:</b> <i>{next_song['title']}</i>\n"
            f"<b>❍ Requested by:</b> "
            f"<a href='tg://user?id={next_song['user'].id}'><u>{next_song['user'].first_name}</u></a>"
            "</blockquote>"
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏸ Pause", callback_data="pause"),
             InlineKeyboardButton("▶ Resume", callback_data="resume"),
             InlineKeyboardButton("⏭ Skip", callback_data="skip")]
        ])

        msg = await bot.send_photo(chat_id, thumb, caption=caption, reply_markup=kb)
        asyncio.create_task(auto_next_timer(chat_id, next_song["duration"]))

    except Exception as e:
        await bot.send_message(chat_id, f"⚠️ Could not auto-play next queued song:\n<code>{e}</code>", parse_mode=ParseMode.HTML)



# --- Event bindings (timer-based fallback for PyTgCalls builds without stream_end) ---
async def auto_next_timer(chat_id: int, duration: int):
    """Fallback timer to trigger next song after duration."""
    await asyncio.sleep(duration)
    await handle_next(chat_id)


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
        await handle_next(chat_id)


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
# Native Seek / Seekback + Auto Queue Clear + Ping
# ==============================
from datetime import datetime

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


import asyncio
import time
from pytgcalls.types import MediaStream

async def restart_with_seek(chat_id: int, seek_pos: int, message: Message):
    """Restart playback at a given position using FFmpeg trim."""
    if chat_id not in music_queue or not music_queue[chat_id]:
        await message.reply("❌ Nothing is playing.")
        return

    song_info = music_queue[chat_id][0]
    media_url = song_info["url"]
    title = song_info["title"]

    try:
        # stop or leave current VC before replaying
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass

        trimmed_path = f"seeked_{chat_id}.mp3"

        # run ffmpeg to trim from seek_pos
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(seek_pos),
            "-i", media_url,
            "-acodec", "copy",
            trimmed_path
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

        # replay trimmed file
        await call_py.play(chat_id, MediaStream(trimmed_path, video_flags=MediaStream.Flags.IGNORE))
        song_info["start_time"] = time.time() - seek_pos

        await message.reply(f"⏩ Seeked to {format_time(seek_pos)} in **{title}**")

    except Exception as e:
        await message.reply(f"❌ Failed to seek: {e}")


@handler_client.on_message(filters.group & filters.command("seek"))
async def seek_forward(client, message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("❌ Usage: /seek <seconds>")
        return

    chat_id = message.chat.id
    if chat_id not in music_queue or not music_queue[chat_id]:
        await message.reply("❌ Nothing is playing.")
        return

    seconds = int(args[1])
    song_info = music_queue[chat_id][0]
    elapsed = int(time.time() - song_info.get("start_time", time.time()))
    seek_pos = elapsed + seconds

    duration = int(song_info.get("duration", 0))
    if seek_pos >= duration:
        seek_pos = duration

    await restart_with_seek(chat_id, seek_pos, message)


@handler_client.on_message(filters.group & filters.command("seekback"))
async def seek_backward(client, message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.reply("❌ Usage: /seekback <seconds>")
        return

    chat_id = message.chat.id
    if chat_id not in music_queue or not music_queue[chat_id]:
        await message.reply("❌ Nothing is playing.")
        return

    seconds = int(args[1])
    song_info = music_queue[chat_id][0]
    elapsed = int(time.time() - song_info.get("start_time", time.time()))
    seek_pos = max(0, elapsed - seconds)

    await restart_with_seek(chat_id, seek_pos, message)

# ==============================
# Auto queue clear when VC ends
# ==============================
try:
    @call_py.on_stream_end()
    async def on_stream_end_handler(_, update):
        chat_id = update.chat_id
        if chat_id in music_queue:
            music_queue.pop(chat_id, None)
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        await bot.send_message(chat_id, "✅ Voice chat ended — queue cleared.", parse_mode=ParseMode.HTML)
except Exception:
    log.warning("PyTgCalls version may not support on_stream_end, using timer fallback.")


# ==============================
# Ping command (mods only)
# ==============================
# ==============================
# Clean Ping Command (Mods Only)
# ==============================
from datetime import datetime

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

    # Build human-readable uptime string
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days > 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")
    if seconds:
        parts.append(f"{seconds} second{'s' if seconds > 1 else ''}")

    uptime_str = " ".join(parts) if parts else "a moment"

    await msg.edit_text(
        f"<b>Pong!</b> <code>{latency:.2f}s</code>\n"
        f"<b>Uptime</b> - <code>{uptime_str}</code>\n"
        f"<b>Bot of</b> <a href='https://t.me/PraiseTheFraud'>F U R Y</a>",
        parse_mode=ParseMode.HTML,
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
        loop.call_soon_threadsafe(stop_event.set)


    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_handler)

    # run startup + main service
    loop.create_task(start_services())
    loop.run_until_complete(stop_event.wait())
    log.info("🛑 Received shutdown signal, exiting...")

if __name__ == "__main__":
    main()


