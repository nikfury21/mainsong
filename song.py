# error_fixed.py
import os
import aiohttp
import asyncio
import threading
from flask import Flask
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pytgcalls import PyTgCalls
from pytgcalls import StreamType
from pytgcalls.types.input_stream import InputAudioStream

# -------------------------
# Environment / init
# -------------------------
TELEGRAM_API_ID = int(os.getenv("API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")            # optional if you run only userbot-driven commands
USERBOT_SESSION = os.getenv("USERBOT_SESSION")  # session string for userbot (required for PyTgCalls)
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and USERBOT_SESSION):
    raise RuntimeError("Please set API_ID, API_HASH and USERBOT_SESSION in environment.")

# Spotipy client (sync)
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
))

# Pyrogram bot client (bot account) and userbot client (for voice)
# Use two separate Pyrogram Clients: one for bot commands, one for user account (PyTgCalls uses user account)
bot = Client("bot_account", api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH, bot_token=BOT_TOKEN)
userbot = Client("userbot_account", session_string=USERBOT_SESSION, api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH)

# PyTgCalls voice client attached to userbot
voice = PyTgCalls(userbot)

# Flask app (kept as requested) — will run in a background thread
app = Flask(__name__)

@app.route("/")
def index():
    return "deployed"

def run_flask():
    port = int(os.getenv("PORT", 5000))
    # Use threaded=True so Flask does not block other threads
    app.run(host="0.0.0.0", port=port, threaded=True)

# -------------------------
# YouTube / RapidAPI helpers
# -------------------------
async def search_youtube_video_id(session: aiohttp.ClientSession, query: str):
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

async def get_mp3_url_rapidapi(session: aiohttp.ClientSession, video_id: str, debug_chat=None, query=None):
    url = "https://youtube-mp36.p.rapidapi.com/dl"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"
    }
    params = {"id": video_id}

    for attempt in range(6):  # 6 attempts with backoff
        try:
            async with session.get(url, headers=headers, params=params, timeout=20) as resp:
                data = await resp.json()
                dbg = f"[Attempt {attempt+1}] RapidAPI status={resp.status}, data_keys={list(data.keys())}"
                print(dbg)
                if debug_chat:
                    try:
                        await debug_chat.send_message(chat_id=debug_chat.chat.id, text=dbg[:3800])
                    except Exception:
                        pass

                if resp.status != 200:
                    await asyncio.sleep(2)
                    continue
                if data.get("status") == "ok" and data.get("link"):
                    return data["link"]
                elif data.get("status") == "processing":
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(2)
        except Exception as e:
            msg = f"⚠️ RapidAPI fetch exception (attempt {attempt+1}): {e}"
            print(msg)
            if debug_chat:
                try:
                    await debug_chat.send_message(chat_id=debug_chat.chat.id, text=msg)
                except Exception:
                    pass
            await asyncio.sleep(2)
    return None

# -------------------------
# Bot command handlers (Pyrogram)
# -------------------------
@bot.on_message(filters.command("song"))
async def song_command(client: Client, message: Message):
    """/song <query> — search spotify, fallback to YouTube, reply mp3 link (same logic as original)"""
    global sp
    user_query = " ".join(message.command[1:]).strip()
    if not user_query:
        await message.reply_text("Please provide a song name after /song.")
        return

    await message.reply_text(f"Searching Spotify for '{user_query}'...")
    results = None
    tracks = []
    for attempt in range(3):
        try:
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
            # attempt to reinit spotify client (sync)
            await asyncio.sleep(2)
            try:
                sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                    client_id=SPOTIFY_CLIENT_ID,
                    client_secret=SPOTIFY_CLIENT_SECRET
                ))
            except Exception as e2:
                print(f"Reinit spotify failed: {e2}")
                await asyncio.sleep(2)
    else:
        await message.reply_text("❌ Spotify connection failed after 3 retries.")
        return

    # If no spotify results, go to YouTube directly
    if not results or not tracks:
        await message.reply_text(f"No Spotify results for '{user_query}'. Trying YouTube directly...")
        async with aiohttp.ClientSession() as session:
            video_id = await search_youtube_video_id(session, user_query)
            if not video_id:
                await message.reply_text("Could not find anything on YouTube either.")
                return
            mp3_url = await get_mp3_url_rapidapi(session, video_id, debug_chat=message)
            if mp3_url:
                await message.reply_text(f"🎧 Found on YouTube:\n{mp3_url}")
            else:
                await message.reply_text("❌ Couldn’t fetch MP3 from YouTube.")
        return

    # choose best track (avoid remixes/covers)
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
            # send to bot owner or log (best-effort)
            print(f"YouTube search failed: {e}")
            await message.reply_text("YouTube search failed, try again later.")
            return

        if not video_id:
            await message.reply_text("Could not find the video on YouTube.")
            return

        await message.reply_text(f"Found YouTube video (ID: {video_id}). Fetching MP3...")

        mp3_url = await get_mp3_url_rapidapi(session, video_id, debug_chat=message, query=user_query)
        if not mp3_url:
            await message.reply_text("❌ Could not retrieve MP3 file. See logs for details.")
            return

        await message.reply_text("✅ MP3 link received, verifying...")

        # Verify link really points to an MP3 file
        try:
            async with session.head(mp3_url, timeout=10) as head_resp:
                content_type = head_resp.headers.get("Content-Type", "")
                dbg = f"HEAD check -> status={head_resp.status}, content_type={content_type}"
                print(dbg)

                if head_resp.status == 200 and "audio" in content_type.lower():
                    group_id = int(os.getenv("TARGET_GROUP_ID", "-1001234567890"))  # replace with your group id or env
                    msg = (
                        f"🎵 *{title}* by *{artist}*\n\n"
                        f"[▶️ Click to play or download MP3]({mp3_url})"
                    )
                    # send to target group (if BOT_TOKEN is set and bot is in group). If not, this will raise.
                    try:
                        await client.send_message(chat_id=group_id, text=msg, parse_mode="markdown", disable_web_page_preview=False)
                        await message.reply_text("✅ Song link sent to group!")
                        return
                    except Exception as e:
                        print(f"Failed to send to group: {e}")
                        # fallback to replying with link to the user
                else:
                    print("HEAD check suggests not audio or failed")
        except Exception as e:
            print(f"HEAD check error: {e}")

        # fallback: send mp3 link in chat
        await message.reply_text(mp3_url)

@bot.on_message(filters.command("play"))
async def play_command(client: Client, message: Message):
    user_query = " ".join(message.command[1:]).strip()
    if not user_query:
        await message.reply_text("Please provide a song name after /play.")
        return

    await message.reply_text(f"🎵 Searching and playing '{user_query}'...")

    async with aiohttp.ClientSession() as session:
        video_id = await search_youtube_video_id(session, user_query)
        if not video_id:
            await message.reply_text("Could not find on YouTube.")
            return

        mp3_url = await get_mp3_url_rapidapi(session, video_id, debug_chat=message)
        if not mp3_url:
            await message.reply_text("Could not fetch MP3 link.")
            return

        # Join voice chat and stream
        chat_id = message.chat.id
        try:
            await play_audio(chat_id, mp3_url)
            await message.reply_text("✅ Playing in voice chat!")
        except Exception as e:
            await message.reply_text(f"❌ Failed to join voice chat: {e}")

async def play_audio(chat_id: int, mp3_url: str):
    try:
        # using AudioPiped to stream from URL
        await voice.join_group_call(
            chat_id,
            InputAudioStream(mp3_url),
            stream_type=StreamType().pulse_stream
        )

    except Exception as e:
        print(f"VC join error: {e}")
        raise

# -------------------------
# Startup / main
# -------------------------
async def main():
    # start userbot (required for PyTgCalls)
    print("Starting userbot...")
    await userbot.start()
    print("Starting PyTgCalls voice client...")
    await voice.start()
    print("Starting bot client...")
    await bot.start()
    print("✅ Bot and voice client started. Ready.")

    # keep running
    await idle()  # waits until Ctrl+C or stop

if __name__ == "__main__":
    # Start Flask in a background thread (so we satisfy platforms that expect an HTTP endpoint)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("Flask server started in background thread.")

    # Run asyncio main that starts pyrogram clients and voice
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        # best-effort shutdown
        try:
            asyncio.get_event_loop().run_until_complete(voice.stop())
        except Exception:
            pass
        try:
            userbot.stop()
        except Exception:
            pass
        try:
            bot.stop()
        except Exception:
            pass
