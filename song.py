
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
import html
from PIL import Image
from io import BytesIO

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
bot = Client(
    "bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

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

def clean_text(s: str) -> str:
    if not s:
        return ""
    return s.encode("utf-8", "ignore").decode("utf-8", "ignore")


async def is_vc_active(chat_id: int) -> bool:
    try:
        call = call_py.get_call(chat_id)
        return call is not None
    except Exception:
        return False





def format_views(count):
    if not count:
        return "0"
    count = int(count)
    if count >= 1_000_000:
        return f"{count/1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count/1_000:.1f}K"
    return str(count)



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

loop_counts = {}
current_song = {}
music_queue = {}
chat_locks = {}
vc_session = {}  # chat_id -> unique session id
vc_active = set()        # chats where bot is in VC
timers = {}              # chat_id -> auto_next asyncio.Task
BANNED_USERS = set()
afk_users = {}


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
    """
    Returns:
    title, channel, views, duration_seconds, thumbnail_url
    """
    if not YOUTUBE_API_KEY:
        return None, None, None, 0, f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,contentDetails,statistics",
        "id": video_id,
        "key": YOUTUBE_API_KEY
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            if r.status != 200:
                return None, None, None, 0, f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

            data = await r.json()
            items = data.get("items", [])
            if not items:
                return None, None, None, 0, f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

            item = items[0]
            snippet = item["snippet"]
            stats = item.get("statistics", {})
            duration = iso8601_to_seconds(item["contentDetails"]["duration"])

            return (
                snippet.get("title"),
                snippet.get("channelTitle"),
                stats.get("viewCount"),
                duration,
                snippet["thumbnails"]["high"]["url"]
            )


import json


@handler_client.on_message(filters.command("bban"))
async def bban(_, message):
    if message.from_user.id not in MODS:
        return

    if not message.reply_to_message:
        return await message.reply_text("Reply to a user")

    BANNED_USERS.add(message.reply_to_message.from_user.id)
    await message.reply_text("User banned")


@handler_client.on_message(filters.command("unbban"))
async def unbban(_, message):
    if message.from_user.id not in MODS:
        return

    if not message.reply_to_message:
        return await message.reply_text("Reply to a user")

    BANNED_USERS.discard(message.reply_to_message.from_user.id)
    await message.reply_text("User unbanned")



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


def bi(text: str) -> str:
    return f"<b><i>{text}</i></b>"

@handler_client.on_message(filters.command("addplaylist"))
async def add_playlist(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    if len(message.command) < 2:
        return await message.reply_text(bi("Nah not like this qt, lemme show how its done\n/addplaylist (name)"), parse_mode=ParseMode.HTML)

    user_id = message.from_user.id
    name = normalize_name(" ".join(message.command[1:]))

    user_pl = get_user_playlists(user_id)

    if name in user_pl:
        return await message.reply_text(bi("The good/bad thing is that you already made a playlist named same as this"), parse_mode=ParseMode.HTML)
    
    user_pl[name] = []
    save_playlists()

    await message.reply_text(
        bi(f"Okay sir, ready to vibe now {name} created."),
        parse_mode=ParseMode.HTML
    )



@handler_client.on_message(filters.command("add"))
async def add_to_playlist(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    if len(message.command) < 2:
        return await message.reply_text(bi("Not again, lemme show you how its done\n/add (playlist name)"), parse_mode=ParseMode.HTML)

    user_id = message.from_user.id
    name = normalize_name(message.command[1])
    user_pl = get_user_playlists(user_id)

    if name not in user_pl:
        return await message.reply_text(bi("I guess you have a typo mistake here or you forgot to make a playlist with this name as it doesnt exist"), parse_mode=ParseMode.HTML)

    if message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
    else:
        text = message.text.split(None, 2)[-1]

    queries = [q.strip() for q in text.split("\n") if q.strip()]
    if not queries:
        return await message.reply_text(bi("Aah i cant see any song here to add either im dora the explorer or you are drunk"), parse_mode=ParseMode.HTML)

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
        bi(f"Yah yeah! added {added} song(s) to {name}"),
        parse_mode=ParseMode.HTML
    )



@handler_client.on_message(filters.command("playlist"))
async def show_playlist(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    if len(message.command) < 2:
        return await message.reply_text(bi("Nah dude not again like this, lemme show how its done:\n/playlist(name)"), parse_mode=ParseMode.HTML)

    user_id = message.from_user.id
    name = normalize_name(message.command[1])
    user_pl = get_user_playlists(user_id)

    if name not in user_pl or not user_pl[name]:
        return await message.reply_text(bi("Playlist is empty or not found just like your brain"), parse_mode=ParseMode.HTML)

    text = f"🎵 **Playlist: {name}**\n\n"
    for i, song in enumerate(user_pl[name], start=1):
        text += f"{i}. {song['title']}\n"


    await message.reply_text(text)


@handler_client.on_message(filters.command("dlt"))
async def delete_playlist_or_song(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    if len(message.command) < 2:
        return await message.reply_text(bi("Uk you have to be precise to use me haha, usage:\n/dlt (playlist name) (index)"), parse_mode=ParseMode.HTML)
    user_id = message.from_user.id
    args = message.command[1:]
    name = normalize_name(args[0])
    user_pl = get_user_playlists(user_id)

    if name not in user_pl:
        return await message.reply_text(bi("I swear i check playlist with this name but doesnt found any with this name"), parse_mode=ParseMode.HTML)

    # delete whole playlist
    if len(args) == 1:
        del user_pl[name]
        save_playlists()
        return await message.reply_text(bi(f"Ok your wish almighty, deleted {name}"),parse_mode=ParseMode.HTML)

    indexes = sorted({int(i) for i in args[1:] if i.isdigit()}, reverse=True)
    pl = user_pl[name]

    removed = 0
    for idx in indexes:
        if 1 <= idx <= len(pl):
            pl.pop(idx - 1)
            removed += 1

    save_playlists()
    await message.reply_text(bi(f"Ok your wish almighty, deleted {removed} song(s) from {name}"),parse_mode=ParseMode.HTML)



@handler_client.on_message(filters.command("pplay"))
async def play_playlist(client: Client, message: Message):
    if message.from_user.id in BANNED_USERS:
        return

    args = message.command[1:]
    if not args:
        return await message.reply_text(bi("Nah ik you are doing this like you doesnt know anything, usage-\n/pplay (playlist) &lt;random/index&gt;."), parse_mode=ParseMode.HTML)

    user_id = message.from_user.id
    user_pl = get_user_playlists(user_id)

    name = normalize_name(args[0])

    if name not in user_pl:
        return await message.reply_text(bi("I swear i check playlist with this name but doesnt found any with this name."), parse_mode=ParseMode.HTML)

    songs = user_pl[name].copy()
    if not songs:
        return await message.reply_text(bi("Dude you doesnt have any song in this playlist, go ahead and add some."), parse_mode=ParseMode.HTML)

    # /pplay name random
    if len(args) > 1 and args[1] == "random":
        import random
        random.shuffle(songs)

    # /pplay name 3
    elif len(args) > 1 and args[1].isdigit():
        idx = int(args[1])
        if not (1 <= idx <= len(songs)):
            return await message.reply_text(bi("Dk but that index doesnt seems appropriate"), parse_mode=ParseMode.HTML)
        songs = [songs[idx - 1]]

    for song in songs:
        fake = message
        fake.text = f"/play {song['query']}"
        fake.command = ["play", song["query"]]

        await play_command(client, fake)
        await asyncio.sleep(60)




    await message.reply_text(bi(f"Yuhuu playling playlist {name}"),parse_mode=ParseMode.HTML)


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
            [
                InlineKeyboardButton(" Pause", callback_data="pause"),
                InlineKeyboardButton(" Resume", callback_data="resume")
            ],
            [InlineKeyboardButton(bar, callback_data="progress")],
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
    await message.reply_text(bi("Huh, im online since you born"), parse_mode=ParseMode.HTML)


@handler_client.on_message(filters.command("song"))
async def song_command(client: Client, message: Message):
    if message.from_user.id in BANNED_USERS:
        return

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
        await message.reply_text(bi("Either you are dumb or you are high on cocaine, lemme teach you the correct usage:\n/song (name)"), parse_mode=ParseMode.HTML)
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
        await safe_edit(progress_msg, _single_step_text(1, 6, "I tried my best but didnt found any matching video, sorry cutie. "), ParseMode.HTML, last_edit_time_holder=last_edit_ref)
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
            video_title, channel, views, duration, thumb_url = await get_youtube_details(video_id)

            video_title = clean_text(video_title or user_query)
            channel = clean_text(channel or "Unknown")
            duration = duration or 0


            # ⛔ duration limit: 2 hours
            if duration > 7200:
                await safe_edit(
                    progress_msg,
                    _single_step_text(
                        4, 6,
                        bi(f"I will not fall in this trap again, the song's duration is ({format_time(duration)}).\nMaximum allowed: 2 hours.")
                    ),
                    ParseMode.HTML,
                    last_edit_time_holder=last_edit_ref
                )
                return

            # now download audio
            temp_path = await api_download_audio(video_id)


        except Exception as e:
            await safe_edit(
                progress_msg,
                _single_step_text(
                    4, 6,
                    bi("Uff, download failed, dont blame me for this."),
                    ParseMode.HTML
                )
            )


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
            title = clean_text(title)
            channel = clean_text(channel)


            from urllib.parse import quote_plus

            safe_title = clean_text(title)

            youtube_url = f"https://youtu.be/{video_id}"
            lyrics_url = "https://www.google.com/search?q=" + quote_plus(safe_title + " lyrics")

            views_text = format_views(views)
            user = message.from_user

            caption = f"""

࿇ <b>𝗦𝗼𝗻𝗴 𝗦𝗲𝗮𝗿𝗰𝗵 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲𝗱!</b> Here's your song ;

━─━─━━─━「₪」━━─━─━─━

❖ <b>𝗗𝗲𝘁𝗮𝗶𝗹𝘀 :</b>
<blockquote>{title}</blockquote>
❖ <b>𝗔𝗿𝘁𝗶𝘀𝘁 / 𝗖𝗵𝗮𝗻𝗻𝗲𝗹 :</b>
<blockquote>{channel}</blockquote>
❖ <b>𝗩𝗶𝗲𝘄𝘀 :</b>
<blockquote>{views_text}</blockquote>
❖ <b>𝗬𝗼𝘂𝗧𝘂𝗯𝗲 :</b>
<blockquote><a href="{youtube_url}">{title}</a></blockquote>
❖ <b>𝗟𝘆𝗿𝗶𝗰𝘀 :</b>
<blockquote><a href="{lyrics_url}">Official Song Lyrics</a></blockquote>
• <b>𝗦𝗼𝗻𝗴 𝗥𝗲𝗾𝘂𝗲𝘀𝘁𝗲𝗱 𝗕𝘆 :</b>
<blockquote><a href="tg://user?id={user.id}">{user.first_name}</a></blockquote>

━─━─━━─━「₪」━━─━─━─━
            """






            
            await client.send_audio(
                chat_id=message.chat.id,
                audio=temp_path,
                thumb=thumb_path if thumb_path else None,
                caption=caption,
                parse_mode=ParseMode.HTML,
                file_name=f"{clean_text(title)}.mp3",
            )





                

            # cleanup thumbnail
            try:
                if thumb_path:
                    os.remove(thumb_path)
            except:
                pass


        except Exception as e:
            await client.send_message(ADMIN, f"Upload error: {e}")
            await safe_edit(
                progress_msg,
                _single_step_text(6, 6, bi("Uff, upload failed, dont blame me for this.")),
                ParseMode.HTML,
                last_edit_time_holder=last_edit_ref
            )

        finally:
            try: os.remove(temp_path)
            except: pass

        try: await progress_msg.delete()
        except: pass




@handler_client.on_message(filters.reply & filters.command("play"))
async def play_replied_audio(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    replied = message.reply_to_message
    chat_id = message.chat.id

    


    if not replied.audio:
        return await message.reply_text(bi("Dude you was supposed to reply with an audio file."), parse_mode=ParseMode.HTML)

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
        vc_active.add(chat_id)  # optional, not trusted anymore




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
    )




@handler_client.on_message(filters.command("play"))
async def play_command(client: Client, message: Message):
    if message.from_user.id in BANNED_USERS:
        return

    """/play <query> - same search/result as /song but robust to race conditions"""
    query = " ".join(message.command[1:]).strip()
    if not query:
        await message.reply_text(bi("Hey you, yes you, eat almonds, you forgot to give a song name after /play, kid."),parse_mode=ParseMode.HTML)
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
        video_title, channel, views, duration_seconds, thumb_url = await get_youtube_details(vid)

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
            vc_active.add(chat_id)  # optional, not trusted anymore



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
            # 🔥 ALWAYS start auto-next timer for FIRST song
            old = timers.pop(chat_id, None)
            if old:
                old.cancel()

            timers[chat_id] = asyncio.create_task(
                auto_next_timer(chat_id, duration_seconds or 180, session_id)
            )




        except Exception as e:
            await message.reply_text(f"❌ Voice playback error:\n<code>{e}</code>", parse_mode=ParseMode.HTML)


@handler_client.on_message(filters.command("vplay"))
async def vplay_command(client: Client, message: Message):
    if message.from_user.id in BANNED_USERS:
        return

    query = " ".join(message.command[1:]).strip()
    if not query:
        return await message.reply_text(bi("Hey you, yes you, eat almonds, you forgot to give a video name after /vplay, kid."), parse_mode=ParseMode.HTML)

    vid = await html_youtube_first(query)
    if not vid:
        return await message.reply_text("❌ No matching YouTube results.")

    chat_id = message.chat.id

    


    try:
        video_path = await api_download_video(vid)
        title, _, _, duration, thumb_url = await get_youtube_details(vid)


        title = title or query
        duration = duration or 180

    except Exception as e:
        return await message.reply_text(
            f"❌ Video fetch failed:\n<code>{e}</code>",
            parse_mode=ParseMode.HTML
        )

    readable_duration = format_time(duration)

    # 🔥 Fix ghost VC
    if chat_id in current_song and chat_id not in vc_active:
        await cleanup_chat(chat_id)

    task = timers.pop(chat_id, None)
    if task:
        task.cancel()

    lock = get_chat_lock(chat_id)

    async with lock:
        # If something already playing → queue video
        if chat_id in current_song and chat_id in vc_active:
            pos = add_to_queue(chat_id, {
                "title": title,
                "url": video_path,
                "vid": vid,
                "user": message.from_user,
                "duration": duration,
                "is_video": True
            })

            return await message.reply_text(
                f"<b>➜ Added video to queue at</b> <u>#{pos}</u>\n\n"
                f"<b>🎬 Title:</b> <i>{title}</i>\n"
                f"<b>⏱ Duration:</b> <u>{readable_duration}</u>",
                parse_mode=ParseMode.HTML
            )

        # Start video playback
        vc_session[chat_id] = vc_session.get(chat_id, 0) + 1
        session_id = vc_session[chat_id]

        await call_py.play(
            chat_id,
            MediaStream(video_path)  # ✅ VIDEO STREAM
        )
        vc_active.add(chat_id)  # optional, not trusted anymore

        current_song[chat_id] = {
            "title": title,
            "url": video_path,
            "vid": vid,
            "user": message.from_user,
            "duration": duration,
            "start_time": time.time(),
            "is_video": True
        }

        caption = (
            "<blockquote>"
            "<b>🎬 <u>Streaming Video</u></b>\n\n"
            f"<b>❍ Title:</b> <i>{title}</i>\n"
            f"<b>❍ Requested by:</b> "
            f"<a href='tg://user?id={message.from_user.id}'>{message.from_user.first_name}</a>"
            "</blockquote>"
        )

        bar = get_progress_bar(0, duration)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸ Pause", callback_data="pause"),
                InlineKeyboardButton("▶ Resume", callback_data="resume"),
                InlineKeyboardButton("⏭ Skip", callback_data="skip")
            ],
            [InlineKeyboardButton(bar, callback_data="progress")],

        ])

        msg = await message.reply_photo(
            photo=thumb_url,
            caption=caption,
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

        asyncio.create_task(
            update_progress_message(chat_id, msg, time.time(), duration, caption)
        )

        timers[chat_id] = asyncio.create_task(
            auto_next_timer(chat_id, duration, session_id)
        )



async def handle_next(chat_id):
    lock = get_chat_lock(chat_id)
    async with lock:

        # ── LOOP LOGIC ─────────────────────────────
        prev = current_song.get(chat_id)
        if prev and loop_counts.get(chat_id, 0) > 0:
            loop_counts[chat_id] -= 1
            music_queue.setdefault(chat_id, []).insert(0, prev.copy())

        # ── No songs left ─────────────────────────────
        if chat_id not in music_queue or not music_queue[chat_id]:
            await cleanup_chat(chat_id)
            try:
                await bot.send_message(
                    chat_id,
                    "✅ Queue finished and cleared.",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass
            return

        # ── Get next item ─────────────────────────────
        next_song = music_queue[chat_id].pop(0)
        current_song[chat_id] = next_song
        next_song["start_time"] = time.time()

        is_video = next_song.get("is_video", False)

        try:
            # ── Switch stream correctly ─────────────────
            if hasattr(call_py, "change_stream"):
                if is_video:
                    await call_py.change_stream(
                        chat_id,
                        MediaStream(next_song["url"])
                    )
                else:
                    await call_py.change_stream(
                        chat_id,
                        MediaStream(
                            next_song["url"],
                            video_flags=MediaStream.Flags.IGNORE
                        )
                    )
            else:
                if is_video:
                    await call_py.play(chat_id, MediaStream(next_song["url"]))
                else:
                    await call_py.play(
                        chat_id,
                        MediaStream(
                            next_song["url"],
                            video_flags=MediaStream.Flags.IGNORE
                        )
                    )

            vc_active.add(chat_id)  # optional, not trusted anymore

            # ── UI text ────────────────────────────────
            thumb = f"https://img.youtube.com/vi/{next_song.get('vid')}/hqdefault.jpg"
            icon = "🎬" if is_video else "🎧"
            label = "Now Playing (Video)" if is_video else "Now Playing"

            caption = (
                "<blockquote>"
                f"<b>{icon} <u>{label}</u></b>\n\n"
                f"<b>❍ Title:</b> <i>{next_song['title']}</i>\n"
                f"<b>❍ Requested by:</b> "
                f"<a href='tg://user?id={next_song['user'].id}'>"
                f"<u>{next_song['user'].first_name}</u></a>"
                "</blockquote>"
            )

            bar = get_progress_bar(0, next_song.get("duration", 180))

            # ── REMOVED LYRICS BUTTON ─────────────────
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⏸ Pause", callback_data="pause"),
                    InlineKeyboardButton("▶ Resume", callback_data="resume"),
                    InlineKeyboardButton("⏭ Skip", callback_data="skip")
                ],
                [InlineKeyboardButton(bar, callback_data="progress")]
            ])

            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=thumb,
                caption=caption,
                reply_markup=kb,
                parse_mode=ParseMode.HTML
            )

            # ── Progress updater ───────────────────────
            asyncio.create_task(
                update_progress_message(
                    chat_id,
                    msg,
                    time.time(),
                    next_song.get("duration", 180),
                    caption
                )
            )

            # ── Auto-next timer ────────────────────────
            vc_session[chat_id] = vc_session.get(chat_id, 0) + 1
            session_id = vc_session[chat_id]

            timers[chat_id] = asyncio.create_task(
                auto_next_timer(
                    chat_id,
                    next_song.get("duration", 180),
                    session_id
                )
            )

        except Exception as e:
            try:
                await bot.send_message(
                    chat_id,
                    f"⚠️ Could not auto-play next item:\n<code>{e}</code>",
                    parse_mode=ParseMode.HTML
                )
            except:
                pass


@handler_client.on_message(filters.command("loop"))
async def loop_command(client, message: Message):
    if message.from_user.id in BANNED_USERS:
        return

    args = message.command[1:]

    if not args or not args[0].isdigit():
        return await message.reply_text("❌ Usage: /loop <number>")

    chat_id = message.chat.id

    if chat_id not in current_song:
        return await message.reply_text("❌ Nothing is playing.")

    loop_counts[chat_id] = int(args[0])

    await message.reply_text(
        f"🔁 Loop set to {args[0]} time(s)."
    )


if HAS_STREAM_END:
    @call_py.on_stream_end()
    async def stream_end_handler(_, update):
        chat_id = update.chat_id

        if not await is_vc_active(chat_id):
            return

        await handle_next(chat_id)


@handler_client.on_message(filters.command("end"))
async def end_command(client: Client, message: Message):
    if message.from_user.id in BANNED_USERS:
        return


    chat_id = message.chat.id

    vc_session[chat_id] = vc_session.get(chat_id, 0) + 1

    t = timers.pop(chat_id, None)
    if t:
        t.cancel()

    music_queue.pop(chat_id, None)
    current_song.pop(chat_id, None)
    loop_counts.pop(chat_id, None)

    try:
        await call_py.leave_call(chat_id)
    except:
        pass

    vc_active.discard(chat_id)

    await message.reply_text("🛑 Ended everything.")


@handler_client.on_message(filters.command("fplay"))
async def fplay_command(client: Client, message: Message):
    if message.from_user.id in BANNED_USERS:
        return

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
        video_title, channel, views, duration_seconds, thumb_url = await get_youtube_details(vid)

        # fallback safety
        video_title = video_title or query
        duration_seconds = duration_seconds or 180



        if not mp3:
            await message.reply_text("❌ Could not fetch audio link.")
            return

        # get title/duration best-effort
        video_title, channel, views, duration_seconds, thumb_url = await get_youtube_details(vid)

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
    if message.from_user.id in BANNED_USERS:
        return

    query = " ".join(message.command[1:]).strip()
    if not query:
        return await message.reply_text(
            bi("Hey you, yes you, eat almonds, you forgot to give a video name after /video, kid."),
            parse_mode=ParseMode.HTML
        )

    msg = await message.reply_text(
        bi("Lemme scroll YouTube to find the video so you don’t have to 😌"),
        parse_mode=ParseMode.HTML
    )

    # 🔍 Search video
    vid = await html_youtube_first(query)
    if not vid:
        return await msg.edit_text("❌ No video found.")

    # 🎯 Fetch REAL YouTube details
    title, channel, views, duration, thumb_url = await get_youtube_details(vid)

    # Safety fallbacks
    title = title or query
    channel = channel or "Unknown Channel"
    duration = duration or 0
    views = views or 0

    if duration > 3600:
        return await msg.edit_text(
            f"❌ Video is too long.\n\n"
            f"📏 Duration: {format_time(duration)}\n"
            f"⚠️ Maximum allowed: 1 hour"
        )

    try:
        await msg.edit_text(
            bi("Using forbidden jutsu to download this video… 🌀"),
            parse_mode=ParseMode.HTML
        )

        # ⬇️ Download video
        video_path = await api_download_video(vid)

        # 🖼 Download thumbnail locally (Telegram requires local file)
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

        # 🔗 URLs
        youtube_url = f"https://youtu.be/{vid}"
        views_text = format_views(views)
        user = message.from_user
        lyrics_url = f"https://www.google.com/search?q={title.replace(' ', '+')}+lyrics"


        # 🎨 FINAL UI CAPTION
        caption = f"""

࿇ <b>𝗩𝗶𝗱𝗲𝗼 𝗦𝗲𝗮𝗿𝗰𝗵 𝗖𝗼𝗺𝗽𝗹𝗲𝘁𝗲𝗱!</b> Here's your video ;

━─━─━━─━「₪」━━─━─━─━

❖ <b>𝗗𝗲𝘁𝗮𝗶𝗹𝘀 :</b>
<blockquote>{title}</blockquote>
❖ <b>𝗖𝗵𝗮𝗻𝗻𝗲𝗹 :</b>
<blockquote>{channel}</blockquote>
❖ <b>𝗩𝗶𝗲𝘄𝘀 :</b>
<blockquote>{views_text}</blockquote>
❖ <b>𝗬𝗼𝘂𝗧𝘂𝗯𝗲 :</b>
<blockquote><a href="{youtube_url}">{title}</a></blockquote>
❖ <b>𝗟𝘆𝗿𝗶𝗰𝘀 :</b>
<blockquote><a href="{lyrics_url}">Official Video Lyrics</a></blockquote>
• <b>𝗩𝗶𝗱𝗲𝗼 𝗥𝗲𝗾𝘂𝗲𝘀𝘁𝗲𝗱 𝗕𝘆 :</b>
<blockquote><a href="tg://user?id={user.id}">{user.first_name}</a></blockquote>

━─━─━━─━「₪」━━─━─━─━
"""


        # 📤 Send video
        await client.send_video(
            chat_id=message.chat.id,
            video=video_path,
            thumb=thumb_path if thumb_path else None,
            caption=caption,
            parse_mode=ParseMode.HTML,
            supports_streaming=True,
        )

        # 🧹 Cleanup
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
    if message.from_user.id in BANNED_USERS:
        return

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

        if not await is_vc_active(chat_id):
            return

        await handle_next(chat_id)

    except asyncio.CancelledError:
        return


# When playing a song, we’ll start this timer
# Modify handle_next_in_queue to start a timer too


@handler_client.on_message(filters.command("mpause"))
async def mpause_command(client, message: Message):
    if message.from_user.id in BANNED_USERS:
        return

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
    if message.from_user.id in BANNED_USERS:
        return

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
    if message.from_user.id in BANNED_USERS:
        return

    chat_id = message.chat.id   # ✅ FIX: define chat_id

    user = await client.get_chat_member(chat_id, message.from_user.id)
    if not (user.privileges or user.status in ("administrator", "creator")):
        await message.reply_text(
            "❌ <b>You need to be an admin to use this command.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ✅ FIX: VC state check
    if not await is_vc_active(chat_id):
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
    if message.from_user.id in BANNED_USERS:
        return

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


@handler_client.on_message(filters.command("seek"))
async def seek_cmd(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    chat_id = message.chat.id

    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: /seek <seconds>")

    try:
        seconds = int(message.command[1])
        if seconds <= 0:
            raise ValueError
    except:
        return await message.reply_text("❌ Enter a valid number of seconds.")

    # 🔥 REAL PLAYING CHECK (important)
    try:
        call = call_py.get_call(chat_id)
    except:
        return await message.reply_text("❌ Nothing is playing.")

    # 🔁 SEEK FORWARD
    await call_py.change_stream(
        chat_id,
        MediaStream(
            call.input.filename,
            seek=seconds,
            video_flags=MediaStream.Flags.IGNORE
        )
    )

    await message.reply_text(f"⏩ Seeked forward {seconds} seconds.")


@handler_client.on_message(filters.command("seekback"))
async def seekback_cmd(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    chat_id = message.chat.id

    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: /seekback <seconds>")

    try:
        seconds = int(message.command[1])
        if seconds <= 0:
            raise ValueError
    except:
        return await message.reply_text("❌ Enter a valid number of seconds.")

    # 🔥 REAL PLAYING CHECK
    try:
        call = call_py.get_call(chat_id)
    except:
        return await message.reply_text("❌ Nothing is playing.")

    # ⏪ SEEK BACKWARD (negative seek)
    await call_py.change_stream(
        chat_id,
        MediaStream(
            call.input.filename,
            seek=-seconds,
            video_flags=MediaStream.Flags.IGNORE
        )
    )

    await message.reply_text(f"⏪ Seeked back {seconds} seconds.")


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



# ================= AI SYSTEM PROMPT =================

SYSTEM_PROMPT = (
    "- Identity:\n"
    "  - Name: Waguri\n"
    "  - Personality: sarcastic, witty, mildly savage\n"
    "  - Tone: human, street-smart, confident\n"
    "  - Slightly rude, never formal, never soft\n\n"

    "- Core Rule (MOST IMPORTANT):\n"
    "  - Always answer the user's question first\n"
    "  - Roasting is allowed ONLY after giving the answer\n"
    "  - If no question is asked, respond with sarcasm\n\n"

    "- Reply Style:\n"
    "  - Short replies (1–3 lines max)\n"
    "  - Clear, direct, useful\n"
    "  - Cool sarcasm, not aggressive abuse\n"
    "  - Slang allowed but readable\n"
    "  - No unnecessary roasting\n\n"

    "- Forbidden:\n"
    "  - No emojis\n"
    "  - No hashtags\n"
    "  - No apologies\n"
    "  - No \"as an AI\"\n\n"

    "- Behavior Rules:\n"
    "  - Do NOT refuse to answer normal questions\n"
    "  - Do NOT roast instead of answering\n"
    "  - Sarcasm should enhance replies, not replace them\n"
    "  - Stay confident, not annoying\n\n"

    "- Conversation Rules:\n"
    "  - Remember previous messages\n"
    "  - Maintain context\n"
    "  - Follow-ups must connect properly\n"
    "  - Explain briefly when needed\n\n"

    "- Greeting Handling:\n"
    "  - hi/hello/gm/gn → dry, dismissive, not abusive\n"
    "  - Max 2 lines\n\n"

    "- Mobile Phone Rule:\n"
    "  - Use fixed bullet format only:\n"
    "    - Manufacturer\n"
    "    - Display\n"
    "    - Processor\n"
    "    - RAM & Storage\n"
    "    - Camera\n"
    "    - Battery\n"
    "    - Build & Durability\n"
    "    - Connectivity\n"
    "    - Extras\n"
    "  - No storytelling, no deep explanations\n\n"

    "- Output Constraints:\n"
    "  - No long intros\n"
    "  - No emotional sympathy\n"
    "  - No character breaks\n"
    "  - Savage, but smart\n"
)


chat_history = {}
MAX_HISTORY = 10


from groq import Groq
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

async def ask_groq(chat_id: int, query: str) -> str:
    raw_history = chat_history.get(chat_id, [])

    history = [
        msg for msg in raw_history
        if isinstance(msg, dict)
        and "role" in msg
        and "content" in msg
    ]

    history.append({"role": "user", "content": query})
    history = history[-10:]

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            *history
        ],
        temperature=0.4
    )

    reply = response.choices[0].message.content.strip()

    history.append({"role": "assistant", "content": reply})
    chat_history[chat_id] = history

    return reply



from google import genai
from google.genai.types import GenerateContentConfig

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-2.5-flash"

async def ask_ai(chat_id: int, query: str) -> str:
    history = chat_history.get(chat_id, [])

    history.append(f"User: {query}")
    history = history[-MAX_HISTORY:]

    formatted_history = []

    for msg in history:
        if isinstance(msg, dict):
            role = msg.get("role", "")
            content = msg.get("content", "")
            formatted_history.append(f"{role.capitalize()}: {content}")
        else:
            formatted_history.append(msg)

    prompt = SYSTEM_PROMPT + "\n\n" + "\n".join(formatted_history)


    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=GenerateContentConfig(temperature=0.4)
    )

    reply = response.text.strip()
    history.append(f"Waguri: {reply}")
    chat_history[chat_id] = history

    return reply



@handler_client.on_message(filters.command("ask") & filters.text)
async def ask_handler(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    if len(message.command) < 2:
        await message.reply_text("Say it properly.")
        return

    query = " ".join(message.command[1:])
    reply = await ask_ai(message.chat.id, query)
    await message.reply_text(reply)


@handler_client.on_message(filters.mentioned & filters.text)
async def mention_handler(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    query = message.text.replace("@BestFreakingBot", "").strip()
    if not query:
        query = "Hello"
    reply = await ask_groq(message.chat.id, query)
    await message.reply_text(reply)


@handler_client.on_message(filters.text)
async def name_call_handler(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    if message.text.startswith("/"):
        return

    if "waguri" in message.text.lower():
        reply = await ask_groq(message.chat.id, message.text)
        await message.reply_text(reply)


@handler_client.on_message(filters.reply & filters.text)
async def reply_handler(client, message):
    if message.from_user.id in BANNED_USERS:
        return

    replied = message.reply_to_message
    if replied and replied.from_user.id == client.me.id:
        reply = await ask_groq(message.chat.id, message.text)
        await message.reply_text(reply)


# ================= AFK LOGIC ================= #

@bot.on_message(filters.command("afk"), group=-1)
async def afk_command(client, message):
    global afk_users

    user = message.from_user
    reason = "None"

    if len(message.command) > 1:
        reason = " ".join(message.command[1:]).strip() or "None"

    afk_media = None
    afk_media_type = None

    if message.reply_to_message:
        reply = message.reply_to_message
        if reply.photo:
            afk_media = reply.photo[-1].file_id
            afk_media_type = "photo"
        elif reply.sticker:
            afk_media = reply.sticker.file_id
            afk_media_type = "sticker"

    chat_id = message.chat.id
    existing = afk_users.get(user.id)

    if existing:
        chats = existing.get("chats", set())
        chats.add(chat_id)
    else:
        chats = {chat_id}

    afk_users[user.id] = {
        "time": datetime.utcnow(),
        "reason": reason,
        "media": afk_media,
        "media_type": afk_media_type,
        "chats": chats,
    }

    text = (
        f"<a href='tg://user?id={user.id}'>"
        f"{user.first_name}</a> is now away from keyboard! Sayonara!"
    )

    if reason and reason != "None":
        text += f"\nReason: {reason}"

    await message.reply_text(text)


@bot.on_message(filters.all & ~filters.command(["afk"]))
async def afk_watcher(client, message):
    global afk_users

    if not message.from_user:
        return

    sender_id = message.from_user.id
    current_chat = message.chat.id

    # === AFK RETURN CHECK ===
    if sender_id in afk_users:
        afk_data = afk_users.pop(sender_id)

        duration = datetime.utcnow() - afk_data["time"]
        seconds = int(duration.total_seconds())
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)

        parts = []
        if h: parts.append(f"{h}h")
        if m: parts.append(f"{m}m")
        if s: parts.append(f"{s}s")

        duration_str = " ".join(parts) or "moments"

        text = (
            f"<a href='tg://user?id={sender_id}'>"
            f"{message.from_user.first_name}</a> is now back online "
            f"and was AFK for {duration_str}."
        )

        if afk_data.get("reason") and afk_data["reason"] != "None":
            text += f"\nReason: {afk_data['reason']}"

        await message.reply_text(text, parse_mode=ParseMode.HTML)

        if afk_data:
            duration = datetime.utcnow() - afk_data["time"]
            seconds = int(duration.total_seconds())
            h, rem = divmod(seconds, 3600)
            m, s = divmod(rem, 60)

            parts = []
            if h: parts.append(f"{h}h")
            if m: parts.append(f"{m}m")
            if s: parts.append(f"{s}s")

            duration_str = " ".join(parts) or "moments"

            text = (
                f"<a href='tg://user?id={sender_id}'>"
                f"{message.from_user.first_name}</a> is now back online and was AFK for {duration_str}."
            )

            if afk_data.get("reason") and afk_data["reason"] != "None":
                text += f"\nReason: {afk_data['reason']}"

            await message.reply_text(text, parse_mode=ParseMode.HTML)

    # === MENTION CHECK ===
    mentioned = set()

    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                if ent.user.id in afk_users:
                    mentioned.add(ent.user.id)

            elif ent.type == "mention":
                username = message.text[ent.offset:ent.offset + ent.length]
                for uid in afk_users:
                    try:
                        user = await client.get_users(uid)
                        if user.username and username.lower() == f"@{user.username.lower()}":
                            mentioned.add(uid)
                    except:
                        continue

    if message.reply_to_message:
        replied_user = message.reply_to_message.from_user
        if replied_user and replied_user.id in afk_users:
            mentioned.add(replied_user.id)

    for uid in mentioned:
        afk_data = afk_users.get(uid)
        if not afk_data:
            continue

        duration = datetime.utcnow() - afk_data["time"]
        seconds = int(duration.total_seconds())
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)

        parts = []
        if h: parts.append(f"{h}h")
        if m: parts.append(f"{m}m")
        if s: parts.append(f"{s}s")

        duration_str = " ".join(parts) or "moments"

        user = await client.get_users(uid)

        text = (
            f"<a href='tg://user?id={uid}'>"
            f"{user.first_name}</a> is AFK since {duration_str}."
        )

        if afk_data.get("reason") and afk_data["reason"] != "None":
            text += f"\nReason: {afk_data['reason']}"

        await message.reply_text(text, parse_mode=ParseMode.HTML)


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
