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

