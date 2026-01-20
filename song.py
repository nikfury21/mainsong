# error.py — complete, Render-ready, Pyrogram + PyTgCalls (MediaStream) based music helpers
import os
import tempfile
import asyncio
import threading
import logging
import aiohttp
from flask import Flask
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
import time
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
import re
from functools import partial


# --- Compatibility handling for PyTgCalls versions ---
try:
    from pytgcalls import StreamType
    from pytgcalls.types import Update
except ImportError:
    StreamType = None
    Update = None
from pyrogram.enums import ChatAction
import requests

import google.generativeai as genai
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.5-flash")


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
API_BASE = "https://shrutibots.site"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
USERBOT_SESSION = os.getenv("USERBOT_SESSION")   # session string for user account
BOT_TOKEN = os.getenv("BOT_TOKEN", None)         # optional: bot account token
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
TARGET_GROUP_ID = "-1003101399560"
MODS = [8353079084, 8355303766]  # your Telegram ID(s)



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
handler_client = bot if bot else userbot






# ======================================
# CORRECT QUEUE MODEL
# ======================================
# ======================
# PLAYLIST SYSTEM
# ======================
# playlists[user_id][playlist_name] = [ "song query", "song query", ... ]
import json
from pathlib import Path

PLAYLIST_FILE = Path("playlists.json")

# single source of truth for playlists
USER_PLAYLISTS = {}
# keep legacy name `playlists` as an alias so older code continues to work
playlists = USER_PLAYLISTS


BACKUP_CHAT_ID = 8353079084  # 🔴 YOUR Telegram ID
PLAYLIST_BACKUP_FILE = "playlists_backup.json"

import uuid

LYRICS_CACHE = {}


def fetch_lyrics(query: str) -> str | None:
    try:
        url = "https://lrclib.net/api/search"
        params = {"q": query}
        r = requests.get(url, params=params, timeout=10)

        if r.status_code != 200:
            return None

        data = r.json()
        if not data:
            return None

        # Prefer synced lyrics, fallback to plain
        for item in data:
            if item.get("plainLyrics"):
                return item["plainLyrics"]

        return None

    except Exception:
        return None

def is_hindi(text: str) -> bool:
    """Return True if text contains Devanagari characters (basic Hindi detection)."""
    return bool(re.search(r"[\u0900-\u097F]", text)) if text else False





def normalize_lyrics_query(q: str) -> str:
    q = q.lower().strip()

    # "song by artist" → "artist - song"
    if " by " in q:
        song, artist = q.split(" by ", 1)
        q = f"{artist.strip()} - {song.strip()}"

    # remove junk words
    q = re.sub(r"\b(official|audio|video|lyrics|mv|remastered)\b", "", q)
    q = re.sub(r"[^\w\s\-]", "", q)
    q = re.sub(r"\s+", " ", q)

    return q.strip()

def lyrics_button(title: str):
    key = uuid.uuid4().hex[:8]  # short & Telegram-safe
    LYRICS_CACHE[key] = title  # store ORIGINAL YouTube title

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📜 Lyrics", callback_data=f"lyrics|{key}")]]
    )



@handler_client.on_callback_query(filters.regex("^lyrics\\|"))
async def lyrics_callback(_, query):
    key = query.data.split("|", 1)[1]

    title = LYRICS_CACHE.get(key)
    if not title:
        return await query.answer("❌ Lyrics expired.", show_alert=True)

    # Normalize & fetch lyrics (same as before)
    fixed = normalize_lyrics_query(title)
    lyrics = await asyncio.to_thread(fetch_lyrics, fixed)

    if not lyrics:
        return await query.message.reply_text("❌ Lyrics not available.")

    # cache the original lyrics for possible translation later
    LYRICS_CACHE[f"lyrics_{key}"] = lyrics

    # if lyrics are Hindi (Devanagari) -> show Translate button
    buttons = None
    if is_hindi(lyrics):
        buttons = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🌐 Translate to English", callback_data=f"translate|{key}")]]
        )

    # send original lyrics (no transliteration/translation here)
    if len(lyrics) <= 4000:
        await query.message.reply_text(lyrics, reply_markup=buttons)
    else:
        # if long, send first chunk with buttons, then the rest
        await query.message.reply_text(lyrics[:4000], reply_markup=buttons)
        for i in range(4000, len(lyrics), 4000):
            await query.message.reply_text(lyrics[i:i+4000])




@handler_client.on_callback_query(filters.regex("^translate\\|"))
async def translate_lyrics_cb(_, query):
    key = query.data.split("|", 1)[1]
    lyrics = LYRICS_CACHE.get(f"lyrics_{key}")

    # if not cached, ask user to press the lyrics button first
    if not lyrics:
        return await query.answer("❌ Original lyrics not found. Click the Lyrics button first.", show_alert=True)

    # indicate work started
    await query.answer("Translating…")

    # build prompt (preserve line breaks, ask for natural English)
    prompt = f"""
Convert the following Hindi song lyrics into Hinglish.

Rules:
- Keep the language Hindi.
- ONLY change the script from Devanagari to English letters.
- Do NOT translate meanings into English.
- Do NOT rewrite creatively.
- Preserve line breaks exactly.
- Use natural, commonly spoken Hinglish.
- Avoid incorrect phonetics like: maim, mem, batem.
- Use forms like: main, mein, baatein, hoon, kyun, nahi.

Example:

Hindi:
हाँ, करता मैं बातें मेरी आईने से देखो तेरी
तुझसे कैसे खुल के वैसे बोलूँ मैं ये राज़ दिल के?

Correct Hinglish:
haan, karta main baatein meri aaine se dekho teri
tujhse kaise khul ke waise bolun main ye raaz dil ke?

Now convert this:

{lyrics}
"""


    try:
        # call Gemini model (2.5-flash)
        resp = gemini.generate_content(prompt)
        translated = resp.text.strip()

    except Exception as e:
        await query.message.reply_text(f"❌ Translation failed: {e}")
        return

    # send translated text (split safely)
    if len(translated) <= 4000:
        await query.message.reply_text(translated)
    else:
        for i in range(0, len(translated), 4000):
            await query.message.reply_text(translated[i:i+4000])




def load_playlists():
    global USER_PLAYLISTS, playlists
    if PLAYLIST_FILE.exists():
        try:
            with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    # preserve same object references and update contents
                    USER_PLAYLISTS.clear()
                    USER_PLAYLISTS.update(data)
                    playlists.clear()
                    playlists.update(data)
                else:
                    USER_PLAYLISTS.clear()
                    playlists.clear()
        except Exception:
            USER_PLAYLISTS.clear()
            playlists.clear()
    else:
        USER_PLAYLISTS.clear()
        playlists.clear()



def save_playlists():
    with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(USER_PLAYLISTS, f, indent=2, ensure_ascii=False)



def get_user_playlists(user_id: int):
    uid = str(user_id)
    USER_PLAYLISTS.setdefault(uid, {})
    return USER_PLAYLISTS[uid]


def normalize_name(name: str) -> str:
    return name.strip().lower()


current_song = {}
music_queue = {}
chat_locks = {}
vc_session = {}  # chat_id -> unique session id
vc_active = set()        # chats where bot is in VC
timers = {}              # chat_id -> auto_next asyncio.Task

async def download_thumbnail(url: str) -> str | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as r:
                if r.status != 200:
                    return None

                fd, path = tempfile.mkstemp(suffix=".jpg")
                os.close(fd)

                with open(path, "wb") as f:
                    f.write(await r.read())

                return path
    except:
        return None

async def get_youtube_details(video_id: str):
    """Return (title, duration_seconds, thumbnail_url)"""
    if not YOUTUBE_API_KEY:
        return None, 0, f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,contentDetails",
        "id": video_id,
        "key": YOUTUBE_API_KEY
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            if r.status != 200:
                return None, 0, f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

            data = await r.json()
            items = data.get("items", [])
            if not items:
                return None, 0, f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

            snippet = items[0]["snippet"]
            duration = iso8601_to_seconds(items[0]["contentDetails"]["duration"])

            return (
                snippet.get("title"),
                duration,
                snippet["thumbnails"]["high"]["url"]
            )


import json

def dump_playlists_to_file(path=PLAYLIST_BACKUP_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(USER_PLAYLISTS, f, indent=2, ensure_ascii=False)



async def cleanup_chat(chat_id: int):
    vc_session[chat_id] = vc_session.get(chat_id, 0) + 1
    vc_active.discard(chat_id)
    current_song.pop(chat_id, None)
    music_queue.pop(chat_id, None)

    task = timers.pop(chat_id, None)
    if task:
        task.cancel()

    try:
        await call_py.leave_call(chat_id)
    except:
        pass





def normalize_name(name: str) -> str:
    return name.strip().lower()


def get_chat_lock(chat_id: int) -> asyncio.Lock:
    """Return a per-chat asyncio.Lock (create if missing)."""
    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()
    return chat_locks[chat_id]
# -----------------------------------------------------------------


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



# -------------------------
# Caption helpers
# -------------------------
async def api_download_audio(video_id: str) -> str:
    file_path = f"{DOWNLOAD_DIR}/{video_id}.mp3"
    if os.path.exists(file_path):
        return file_path

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{API_BASE}/download",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "type": "audio"}
        ) as r:
            data = await r.json()
            token = data.get("download_token")
            if not token:
                raise RuntimeError("No audio token")

        stream_url = f"{API_BASE}/stream/{video_id}?type=audio&token={token}"
        async with session.get(stream_url) as r:
            with open(file_path, "wb") as f:
                async for chunk in r.content.iter_chunked(65536):
                    f.write(chunk)

    return file_path


async def api_download_video(video_id: str) -> str:
    file_path = f"{DOWNLOAD_DIR}/{video_id}.mp4"
    if os.path.exists(file_path):
        return file_path

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{API_BASE}/download",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "type": "video"}
        ) as r:
            data = await r.json()
            token = data.get("download_token")
            if not token:
                raise RuntimeError("No video token")

        stream_url = f"{API_BASE}/stream/{video_id}?type=video&token={token}"
        async with session.get(stream_url) as r:
            with open(file_path, "wb") as f:
                async for chunk in r.content.iter_chunked(131072):
                    f.write(chunk)

    return file_path


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


@handler_client.on_message(filters.command("addplaylist"))
async def add_playlist(client, message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: /addplaylist <name>")

    user_id = message.from_user.id
    name = normalize_name(" ".join(message.command[1:]))

    user_pl = get_user_playlists(user_id)

    if name in user_pl:
        return await message.reply_text("⚠️ Playlist already exists.")

    user_pl[name] = []
    save_playlists()

    await message.reply_text(f"✅ Playlist **{name}** created.")



@handler_client.on_message(filters.command("add"))
async def add_to_playlist(client, message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: /add <playlist_name>")

    user_id = message.from_user.id
    name = normalize_name(message.command[1])
    user_pl = get_user_playlists(user_id)

    if name not in user_pl:
        return await message.reply_text("❌ Playlist not found.")

    if message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
    else:
        text = message.text.split(None, 2)[-1]

    queries = [q.strip() for q in text.split("\n") if q.strip()]
    if not queries:
        return await message.reply_text("❌ No songs detected.")

    added = 0

    for query in queries:
        try:
            vid = await html_youtube_first(query)
            if not vid:
                continue

            title, _, _ = await get_youtube_details(vid)
            title = title or query

            user_pl[name].append({
                "title": title,
                "query": query,
                "vid": vid
            })
            added += 1

        except Exception:
            continue

    save_playlists()

    await message.reply_text(
        f"✅ Added **{added}** song(s) to **{name}**"
    )



@handler_client.on_message(filters.command("playlist"))
async def show_playlist(client, message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: /playlist <name>")

    user_id = message.from_user.id
    name = normalize_name(message.command[1])
    user_pl = get_user_playlists(user_id)

    if name not in user_pl or not user_pl[name]:
        return await message.reply_text("⚠️ Playlist empty or not found.")

    text = f"🎵 **Playlist: {name}**\n\n"
    for i, song in enumerate(user_pl[name], start=1):
        text += f"{i}. {song['title']}\n"


    await message.reply_text(text)


@handler_client.on_message(filters.command("dlt"))
async def delete_playlist_or_song(client, message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: /dlt <name> [indexes]")

    user_id = message.from_user.id
    args = message.command[1:]
    name = normalize_name(args[0])
    user_pl = get_user_playlists(user_id)

    if name not in user_pl:
        return await message.reply_text("❌ Playlist not found.")

    # delete whole playlist
    if len(args) == 1:
        del user_pl[name]
        save_playlists()
        return await message.reply_text(f"🗑 Deleted playlist **{name}**")

    indexes = sorted({int(i) for i in args[1:] if i.isdigit()}, reverse=True)
    pl = user_pl[name]

    removed = 0
    for idx in indexes:
        if 1 <= idx <= len(pl):
            pl.pop(idx - 1)
            removed += 1

    save_playlists()
    await message.reply_text(
        f"🗑 Removed **{removed}** song(s) from **{name}**"
    )



@handler_client.on_message(filters.command("pplay"))
async def play_playlist(client: Client, message: Message):
    args = message.command[1:]
    if not args:
        return await message.reply_text("❌ Usage: /pplay <playlist> [random|index]")

    user_id = message.from_user.id
    user_pl = get_user_playlists(user_id)

    name = normalize_name(args[0])

    if name not in user_pl:
        return await message.reply_text("❌ Playlist not found.")

    songs = user_pl[name].copy()
    if not songs:
        return await message.reply_text("⚠️ Playlist is empty.")

    # /pplay name random
    if len(args) > 1 and args[1] == "random":
        import random
        random.shuffle(songs)

    # /pplay name 3
    elif len(args) > 1 and args[1].isdigit():
        idx = int(args[1])
        if not (1 <= idx <= len(songs)):
            return await message.reply_text("❌ Invalid index.")
        songs = [songs[idx - 1]]

    for song in songs:
        fake = message
        fake.text = f"/play {song['query']}"
        fake.command = ["play", song["query"]]
        await play_command(client, fake)



    await message.reply_text(f"▶️ Playing playlist **{name}**")


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
        await asyncio.sleep(15)


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

        # Step 3: Download MP3
        await safe_edit(progress_msg, _single_step_text(3, 5, "Downloading audio…"), ParseMode.HTML, last_edit_time_holder=last_edit_ref)


        # Step 4: Download MP3
        await safe_edit(progress_msg, _single_step_text(4, 6, "Downloading audio…"), ParseMode.HTML, last_edit_time_holder=last_edit_ref)

        try:
            temp_dir = tempfile.mkdtemp()
            # fetch real YouTube metadata
            video_title, duration, thumb_url = await get_youtube_details(video_id)

            # fallback safety
            video_title = video_title or user_query
            duration = duration or 0

            # ⛔ duration limit: 2 hours
            if duration > 7200:
                await safe_edit(
                    progress_msg,
                    _single_step_text(
                        4, 6,
                        f"❌ Song too long ({format_time(duration)}).\nMaximum allowed: 2 hours."
                    ),
                    ParseMode.HTML,
                    last_edit_time_holder=last_edit_ref
                )
                return

            # now download audio
            temp_path = await api_download_audio(video_id)


        except Exception as e:
            await safe_edit(progress_msg, _single_step_text(4, 6, "❌ Download failed."), ParseMode.HTML, last_edit_time_holder=last_edit_ref)
            return


        # Step 5: Save to temp
        await safe_edit(progress_msg, _single_step_text(5, 6, "Finalizing audio…"), ParseMode.HTML, last_edit_time_holder=last_edit_ref)

        

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
            artist, _ = parse_artist_and_title(video_title)
            title = video_title

            caption = f" <b><u>{artist} - \"{title}\"</u></b>"





            
            await client.send_audio(
                chat_id=message.chat.id,
                audio=temp_path,
                thumb=thumb_path if thumb_path else None,
                caption=caption,
                parse_mode=ParseMode.HTML,
                file_name=f"{title}.mp3",
                reply_markup=lyrics_button(title)
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
        vc_session[chat_id] = vc_session.get(chat_id, 0) + 1
        session_id = vc_session[chat_id]


        await call_py.play(
            chat_id,
            MediaStream(
                file_path,
                video_flags=MediaStream.Flags.IGNORE
            )
        )
        vc_active.add(chat_id)




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
        parse_mode=ParseMode.HTML,
        reply_markup=lyrics_button(title)
    )




@handler_client.on_message(filters.command("play"))
async def play_command(client: Client, message: Message):
    """/play <query> - same search/result as /song but robust to race conditions"""
    query = " ".join(message.command[1:]).strip()
    if not query:
        await message.reply_text("Please provide a song name after /play.")
        return

    try:
        await message.reply_sticker("CAACAgQAAxUAAWkPQRUy37GVR42R2w26sKQx4FKBAAKrGQACQwl4UJ1u2xb-mMqINgQ")
    except:
        pass

    vid = await html_youtube_first(query)
    if not vid:
        await message.reply_text("❌ No matching YouTube results.")
        return

    thumb_url = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"

    try:
        mp3 = await api_download_audio(vid)
        video_title, duration_seconds, thumb_url = await get_youtube_details(vid)

        # fallback safety
        video_title = video_title or query
        duration_seconds = duration_seconds or 180


    except Exception as e:
        await message.reply_text(
            f"❌ Audio extraction failed:\n<code>{e}</code>",
            parse_mode=ParseMode.HTML
        )
        return



               
    readable_duration = format_time(duration_seconds or 0)
    chat_id = message.chat.id

    # --- Acquire per-chat lock to prevent races ---
    # 🔥 FIX: clear ghost state if VC ended earlier
    if chat_id in current_song and chat_id not in vc_active:
        await cleanup_chat(chat_id)

    task = timers.pop(chat_id, None)
    if task:
        task.cancel()

    lock = get_chat_lock(chat_id)

    async with lock:
        # if something is already playing -> add to queue
        if chat_id in current_song and chat_id in vc_active:

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

        # Nothing playing -> start playback
        try:
            # Ensure we stop any stray stream before starting
            try:
                if hasattr(call_py, "stop_stream"):
                    await call_py.stop_stream(chat_id)
                elif hasattr(call_py, "leave_call"):
                    # leave then join is handled by PyTgCalls automatically when playing
                    try:
                        await call_py.leave_call(chat_id)
                    except:
                        pass
            except:
                pass

            # start stream
            vc_session[chat_id] = vc_session.get(chat_id, 0) + 1
            session_id = vc_session[chat_id]

            await call_py.play(
                chat_id,
                MediaStream(
                    mp3,
                    video_flags=MediaStream.Flags.IGNORE
                )
            )
            vc_active.add(chat_id)



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
                "<b>🎧 <u>Streaming (Local Playback)</u></b>\n\n"
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
                [InlineKeyboardButton(bar, callback_data="progress")],
                [InlineKeyboardButton("📜 Lyrics", callback_data=f"lyrics|{video_title}")]
            ])

            msg = await message.reply_photo(
                photo=thumb_url,
                caption=caption,
                reply_markup=kb,
                parse_mode=ParseMode.HTML
            )


            asyncio.create_task(update_progress_message(chat_id, msg, time.time(), duration_seconds or 180, caption))
            task = asyncio.create_task(
                auto_next_timer(chat_id, duration_seconds or 180, session_id)
            )
            timers[chat_id] = task



        except Exception as e:
            await message.reply_text(f"❌ Voice playback error:\n<code>{e}</code>", parse_mode=ParseMode.HTML)





async def handle_next(chat_id):
    lock = get_chat_lock(chat_id)
    async with lock:
        # no songs in queue
        if chat_id not in music_queue or not music_queue[chat_id]:
            await cleanup_chat(chat_id)

            try:
                await bot.send_message(chat_id, "✅ Queue finished and cleared.", parse_mode=ParseMode.HTML)
            except:
                pass
            return

        # get next song
        next_song = music_queue[chat_id].pop(0)
        current_song[chat_id] = next_song
        next_song["start_time"] = time.time()

        try:
            # switch stream (prefer change_stream if available)
            if hasattr(call_py, "change_stream"):
                await call_py.change_stream(chat_id, MediaStream(next_song["url"], video_flags=MediaStream.Flags.IGNORE))
            else:
                await call_py.play(chat_id, MediaStream(next_song["url"], video_flags=MediaStream.Flags.IGNORE))

            thumb = f"https://img.youtube.com/vi/{next_song.get('vid')}/hqdefault.jpg"
            caption = (
                "<blockquote>"
                "<b>🎧 <u>Now Playing</u></b>\n\n"
                f"<b>❍ Title:</b> <i>{next_song['title']}</i>\n"
                f"<b>❍ Requested by:</b> "
                f"<a href='tg://user?id={next_song['user'].id}'><u>{next_song['user'].first_name}</u></a>"
                "</blockquote>"
            )

            bar = get_progress_bar(0, next_song.get("duration", 180))

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏸ Pause", callback_data="pause"),
                InlineKeyboardButton("▶ Resume", callback_data="resume"),
                InlineKeyboardButton("⏭ Skip", callback_data="skip")],
                [InlineKeyboardButton(bar, callback_data="progress")],
                [InlineKeyboardButton("📜 Lyrics", callback_data=f"lyrics|{next_song['title']}")]
            ])

            msg = await bot.send_photo(
                chat_id,
                thumb,
                caption=caption,
                reply_markup=kb,
                parse_mode=ParseMode.HTML
            )


            asyncio.create_task(
                update_progress_message(
                    chat_id,
                    msg,
                    time.time(),
                    next_song.get("duration", 180),
                    caption
                )
            )

            vc_session[chat_id] = vc_session.get(chat_id, 0) + 1
            session_id = vc_session[chat_id]

            task = asyncio.create_task(
                auto_next_timer(chat_id, next_song.get("duration", 180), session_id)
            )
            timers[chat_id] = task


        except Exception as e:
            try:
                await bot.send_message(chat_id, f"⚠️ Could not auto-play next queued song:\n<code>{e}</code>", parse_mode=ParseMode.HTML)
            except:
                pass

@handler_client.on_message(filters.command("fplay"))
async def fplay_command(client: Client, message: Message):
    """Force play a song immediately, stopping current playback. The previous current song is moved to the front of the queue."""
    query = " ".join(message.command[1:]).strip()
    if not query:
        await message.reply_text("Provide a song name after /fplay.")
        return

    chat_id = message.chat.id

    async with aiohttp.ClientSession() as session:
        vid = await html_youtube_first(query)
        if not vid:
            await message.reply_text("❌ No matching YouTube results.")
            return

        mp3 = await api_download_audio(vid)
        video_title, duration_seconds, thumb_url = await get_youtube_details(vid)

        # fallback safety
        video_title = video_title or query
        duration_seconds = duration_seconds or 180



        if not mp3:
            await message.reply_text("❌ Could not fetch audio link.")
            return

        # get title/duration best-effort
        video_title, duration_seconds, thumb_url = await get_youtube_details(vid)

        # fallback safety
        video_title = video_title or query
        duration_seconds = duration_seconds or 180

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
                        duration_seconds = iso8601_to_seconds(content.get("duration"))
        except:
            pass

    lock = get_chat_lock(chat_id)
    async with lock:
        # if a song is playing, move it to front of queue before replacing
        if chat_id in current_song:
            prev = current_song.pop(chat_id, None)
            if prev:
                music_queue.setdefault(chat_id, []).insert(0, prev)
            # stop current playback
            try:
                if hasattr(call_py, "stop_stream"):
                    await call_py.stop_stream(chat_id)
                elif hasattr(call_py, "leave_call"):
                    await call_py.leave_call(chat_id)
            except:
                pass

        # start forced song
        try:
            vc_session[chat_id] = vc_session.get(chat_id, 0) + 1
            session_id = vc_session[chat_id]

            await call_py.play(chat_id, MediaStream(mp3, video_flags=MediaStream.Flags.IGNORE))
            current_song[chat_id] = {
                "title": video_title,
                "url": mp3,
                "vid": vid,
                "user": message.from_user,
                "duration": duration_seconds or 180,
                "start_time": time.time()
            }
            await message.reply_text(f"⏯️ Forced play: <b>{video_title}</b>", parse_mode=ParseMode.HTML)

            # start auto-next timer
            task = asyncio.create_task(
                auto_next_timer(chat_id, duration_seconds or 180, session_id)
            )
            timers[chat_id] = task


        except Exception as e:
            await message.reply_text(f"❌ Could not force-play: {e}")

@handler_client.on_message(filters.command("video"))
async def video_command(client: Client, message: Message):
    query = " ".join(message.command[1:]).strip()
    if not query:
        return await message.reply_text("❌ Usage: /video <search query>")

    msg = await message.reply_text("🔎 Searching YouTube...")

    vid = await html_youtube_first(query)
    if not vid:
        return await msg.edit_text("❌ No video found.")

    title, duration, thumb_url = await get_youtube_details(vid)

    if duration > 3600:
        return await msg.edit_text(
            f"❌ Video is too long.\n\n"
            f"📏 Duration: {format_time(duration)}\n"
            f"⚠️ Maximum allowed: 1 hour"
        )

    try:
        await msg.edit_text("⬇️ Downloading video...")

        video_path = await api_download_video(vid)

        # ---- DOWNLOAD THUMBNAIL LOCALLY (FIX) ----
        thumb_path = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(thumb_url) as r:
                    if r.status == 200:
                        fd, thumb_path = tempfile.mkstemp(suffix=".jpg")
                        os.close(fd)
                        with open(thumb_path, "wb") as f:
                            f.write(await r.read())
        except:
            thumb_path = None
        # ------------------------------------------

        await client.send_video(
            chat_id=message.chat.id,
            video=video_path,
            thumb=thumb_path if thumb_path else None,  # ✅ LOCAL FILE ONLY
            caption=f"🎬 <b>{title}</b>\n⏱ {format_time(duration)}",
            parse_mode=ParseMode.HTML,
            supports_streaming=True
        )

        # ---- CLEANUP ----
        try:
            os.remove(video_path)
        except:
            pass

        try:
            if thumb_path:
                os.remove(thumb_path)
        except:
            pass

        await msg.delete()

    except Exception as e:
        await msg.edit_text(
            f"❌ Failed to send video:\n<code>{e}</code>",
            parse_mode=ParseMode.HTML
        )



@handler_client.on_message(filters.command("resetvc"))
async def reset_vc(client: Client, message: Message):
    if message.from_user.id not in MODS:
        return

    chat_id = message.chat.id
    vc_session[chat_id] = vc_session.get(chat_id, 0) + 1
    await cleanup_chat(chat_id)

    await message.reply_text(
        "• Voice state reset:\n"
        "• Current song cleared\n"
        "• Queue cleared\n"
        "• Timers stopped"
    )



# --- Event bindings (timer-based fallback for PyTgCalls builds without stream_end) ---
async def auto_next_timer(chat_id: int, duration: int, session_id: int):
    try:
        await asyncio.sleep(duration)

        # ❌ OLD VC TIMER → IGNORE
        if vc_session.get(chat_id) != session_id:
            return

        if chat_id not in vc_active:
            return

        await handle_next(chat_id)

    except asyncio.CancelledError:
        return


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
    chat_id = message.chat.id   # ✅ FIX: define chat_id

    user = await client.get_chat_member(chat_id, message.from_user.id)
    if not (user.privileges or user.status in ("administrator", "creator")):
        await message.reply_text(
            "❌ <b>You need to be an admin to use this command.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ✅ FIX: VC state check
    if chat_id not in vc_active:
        return await message.reply_text("❌ Bot is not in a voice chat.")

    task = timers.pop(chat_id, None)
    if task:
        task.cancel()

    try:
        if hasattr(call_py, "stop_stream"):

            await call_py.stop_stream(chat_id)
        elif hasattr(call_py, "leave_call"):
            await call_py.leave_call(chat_id)
        else:
            await call_py.stop(chat_id)

        await message.reply_text(
            "⏭ <b>Skipped current song.</b>",
            parse_mode=ParseMode.HTML,
        )

        # ✅ Play next song in queue
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
        await handle_next(chat_id)


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


@handler_client.on_message(filters.command("reload"))
async def reload_playlists(client, message):
    if message.from_user.id not in MODS:
        return

    if not message.reply_to_message or not message.reply_to_message.document:
        return await message.reply_text(
            "❌ Reply to playlists JSON file."
        )

    doc = message.reply_to_message.document
    if not doc.file_name.endswith(".json"):
        return await message.reply_text("❌ Invalid file type.")

    path = await message.reply_to_message.download()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("Invalid playlist structure")

        # update single source of truth in-place
        global USER_PLAYLISTS, playlists
        USER_PLAYLISTS.clear()
        USER_PLAYLISTS.update(data)
        playlists.clear()
        playlists.update(data)

        await message.reply_text("✅ Playlists reloaded successfully.")

    except Exception as e:
        await message.reply_text(f"❌ Reload failed:\n<code>{e}</code>")

    finally:
        try:
            os.remove(path)
        except:
            pass


@handler_client.on_message(filters.command("backup"))
async def manual_backup(client, message):
    if message.from_user.id not in MODS:
        return

    dump_playlists_to_file()

    await client.send_document(
        message.chat.id,
        PLAYLIST_BACKUP_FILE,
        caption="📦 Manual playlist backup"
    )


# ================================
#   Docker / Render-safe startup
#   + Telegram playlist backup
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
    """Start Pyrogram userbot + bot + PyTgCalls safely, keep idle loop,
    and auto-backup playlists on shutdown.
    """
    # 🔹 Load playlists on startup
    try:
        load_playlists()
        log.info("📂 Playlists loaded into memory.")
    except Exception as e:
        log.error(f"Failed to load playlists: {e}")

    try:
        log.info("🚀 Initializing clients...")

        await userbot.start()
        log.info("[Userbot] connected.")

        await call_py.start()
        log.info("[PyTgCalls] ready.")

        if bot:
            await bot.start()
            log.info("[Bot] started.")

        log.info("✅ All clients started. Entering idle mode...")
        await idle()

    except Exception as e:
        log.error("❌ Runtime error: %s", e)
        traceback.print_exc()

    finally:
        log.info("🔻 Shutdown initiated, backing up playlists...")

        # 🔹 AUTO BACKUP PLAYLISTS TO DM
        try:
            dump_playlists_to_file()

            sender = bot if bot else userbot
            await sender.send_document(
                BACKUP_CHAT_ID,
                PLAYLIST_BACKUP_FILE,
                caption="📦 Playlist auto-backup before shutdown"
            )

            log.info("📦 Playlist backup sent successfully.")

        except Exception as e:
            log.error(f"Playlist backup failed: {e}")

        # 🔹 STOP SERVICES CLEANLY
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
    stop_event = asyncio.Event()

    def stop_handler(*_):
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_handler)
        except NotImplementedError:
            pass

    loop.create_task(start_services())
    loop.run_until_complete(stop_event.wait())

    log.info("🛑 Process terminated.")


if __name__ == "__main__":
    main()
