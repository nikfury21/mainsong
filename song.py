# error.py — complete, Render-ready, Pyrogram + PyTgCalls (MediaStream) based music helpers
import os
import asyncio
import threading
import logging
import aiohttp
from flask import Flask
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream

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

@userbot.on_message(filters.command("song"))
async def song_command_userbot(_, message: Message):
    """Allow using /song from userbot account (if desired). Mirror of bot handler."""
    # simply forward to play flow using same logic as bot below
    await message.reply_text("Use the bot account to run /song (if available).")

# We'll register handlers on `bot` if bot exists, otherwise on userbot (fallback)
handler_client = bot if bot else userbot

@handler_client.on_message(filters.command("song"))
async def song_command(client: Client, message: Message):
    """/song <query> - search Spotify then fallback to YouTube and reply mp3 link"""
    query = " ".join(message.command[1:]).strip()
    if not query:
        await message.reply_text("Please provide a song name after /song.")
        return

    await message.reply_text(f"🔎 Searching Spotify for: {query}")

    tracks = []
    # Try spotify if available
    if sp:
        for attempt in range(3):
            try:
                results = sp.search(q=query, type="track", limit=5)
                tracks = results.get("tracks", {}).get("items", [])
                if tracks:
                    break
            except Exception as e:
                log.warning("Spotify search attempt %d failed: %s", attempt+1, e)
                # re-init spotify client
                try:
                    sp_re = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                        client_id=SPOTIFY_CLIENT_ID,
                        client_secret=SPOTIFY_CLIENT_SECRET
                    ))
                    globals()['sp'] = sp_re
                    sp = sp_re
                except Exception:
                    await asyncio.sleep(1)
    # If no spotify tracks, fallback to youtube search directly
    if not tracks:
        await message.reply_text("No Spotify results — searching YouTube...")
        async with aiohttp.ClientSession() as session:
            vid = await search_youtube_video_id(session, query)
            if not vid:
                await message.reply_text("❌ Could not find on YouTube either.")
                return
            mp3 = await get_mp3_url_rapidapi(session, vid)
            if mp3:
                await message.reply_text(f"🎧 Found:\n{mp3}")
            else:
                await message.reply_text("❌ Could not fetch mp3 link from RapidAPI.")
        return

    # choose best track (avoid remixes/covers)
    track = None
    for t in tracks:
        name = t.get("name", "").lower()
        if "remix" not in name and "cover" not in name:
            track = t
            break
    if not track:
        track = tracks[0]

    title = track.get("name")
    artist = track.get("artists", [])[0].get("name") if track.get("artists") else ""
    search_q = f"{title} {artist} official audio"

    await message.reply_text(f"Found on Spotify: {title} — Searching YouTube for best audio...")

    async with aiohttp.ClientSession() as session:
        vid = await search_youtube_video_id(session, search_q)
        if not vid:
            await message.reply_text("❌ Could not locate on YouTube.")
            return
        mp3 = await get_mp3_url_rapidapi(session, vid)
        if not mp3:
            await message.reply_text("❌ Could not get mp3 link.")
            return

        # Prefer sending via bot account to a group if configured
        if TARGET_GROUP_ID and bot:
            try:
                await bot.send_message(int(TARGET_GROUP_ID), f"🎵 *{title}* by *{artist}*\n[▶️ Play/Download]({mp3})", parse_mode="Markdown", disable_web_page_preview=False)
                await message.reply_text("✅ Link sent to group.")
                return
            except Exception as e:
                log.debug("Failed to send to target group: %s", e)

        # fallback: reply with mp3 link
        await message.reply_text(f"🎧 {title} — {artist}\n{mp3}")

@handler_client.on_message(filters.command("play"))
async def play_command(client: Client, message: Message):
    """/play <query> - find audio and play in voice chat using userbot + PyTgCalls"""
    query = " ".join(message.command[1:]).strip()
    if not query:
        await message.reply_text("Please provide a song name after /play.")
        return

    await message.reply_text(f"🔊 Searching '{query}' and preparing to play...")

    async with aiohttp.ClientSession() as session:
        vid = await search_youtube_video_id(session, query)
        if not vid:
            await message.reply_text("❌ Could not find on YouTube.")
            return
        mp3 = await get_mp3_url_rapidapi(session, vid)
        if not mp3:
            await message.reply_text("❌ Could not fetch audio link.")
            return

    chat_id = message.chat.id
    try:
        # call_py.play expects (chat_id, MediaStream(...))
        log.info("Attempting to play in chat %s stream=%s", chat_id, mp3)
        await call_py.play(chat_id, MediaStream(mp3, video_flags=MediaStream.Flags.IGNORE))
        await message.reply_text("✅ Playing in voice chat (attempted).")
    except Exception as e:
        log.exception("Failed to join voice chat / play: %s", e)
        await message.reply_text(f"❌ Voice playback error: {e}")

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

if __name__ == "__main__":
    import threading

    def start_flask_background():
        threading.Thread(target=run_flask, daemon=True).start()
        log.info("🌐 Flask webserver started in background thread.")

    async def run_all():
        start_flask_background()
        await main_loop()
