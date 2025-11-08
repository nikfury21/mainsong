
import os
import aiohttp
import asyncio
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import threading

# Environment variables (set these in Render environment or your OS)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")

# Initialize Spotify client (blocking, but fast)
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=SPOTIFY_CLIENT_ID,
                                                           client_secret=SPOTIFY_CLIENT_SECRET))

app = Flask(__name__)

@app.route("/")
def index():
    return "deployed"

async def search_youtube_video_id(session, query: str):
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

async def get_mp3_url_rapidapi(session, video_id: str, debug_chat=None, query=None):
    """
    Requests MP3 link from RapidAPI (youtube-mp36) and waits until it's actually downloadable.
    Retries and verifies the CDN file to prevent 404 errors.
    """
    url = "https://youtube-mp36.p.rapidapi.com/dl"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"
    }
    params = {"id": video_id}

    for attempt in range(10):  # 10 attempts with backoff (~50s total)
        try:
            async with session.get(url, headers=headers, params=params, timeout=20) as resp:
                data = await resp.json()
                dbg = f"[Attempt {attempt+1}] RapidAPI status={resp.status}, data={data}"
                print(dbg)
                if debug_chat:
                    await debug_chat.send_message(chat_id=8353079084, text=dbg[:3800])

                if resp.status != 200:
                    await asyncio.sleep(3)
                    continue

                # ✅ Got a potential link
                if data.get("status") == "ok" and data.get("link"):
                    link = data["link"]

                    # Verify the link really exists
                    for check in range(5):
                        try:
                            async with session.head(link, timeout=10) as head_resp:
                                if (
                                    head_resp.status == 200
                                    and "audio" in head_resp.headers.get("Content-Type", "")
                                ):
                                    print(f"MP3 is live on attempt {attempt+1}, check {check+1}")
                                    return link
                                else:
                                    print(f"Waiting for CDN (check {check+1})...")
                                    await asyncio.sleep(3)
                        except Exception:
                            await asyncio.sleep(3)

                    # if not ready after checks, wait and retry API
                    await asyncio.sleep(4)

                elif data.get("status") == "processing":
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(3)

        except Exception as e:
            msg = f"⚠️ RapidAPI fetch exception (attempt {attempt+1}): {e}"
            print(msg)
            if debug_chat:
                await debug_chat.send_message(chat_id=8353079084, text=msg)
            await asyncio.sleep(3)

    # if still nothing works
    return None

async def song_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = " ".join(context.args)
    if not user_query:
        await update.message.reply_text("Please provide a song name after /song.")
        return

    await update.message.reply_text(f"Searching Spotify for '{user_query}'...")
    try:
        results = sp.search(q=user_query, limit=1, type="track")
    except Exception as e:
        await context.bot.send_message(chat_id=8353079084, text=f"Spotify search error: {e}")
        return

    tracks = results.get("tracks", {}).get("items", [])
    if not tracks:
        await update.message.reply_text(f"No results found on Spotify for '{user_query}'.")
        return

    track = tracks[0]
    title = track["name"]
    artist = track["artists"][0]["name"]
    combined_query = f"{title} {artist}"

    await update.message.reply_text(f"Found on Spotify: {title} by {artist}. Searching YouTube...")

    async with aiohttp.ClientSession() as session:
        try:
            video_id = await search_youtube_video_id(session, combined_query)
        except Exception as e:
            await context.bot.send_message(chat_id=8353079084, text=f"YouTube search failed: {e}")
            return

        if not video_id:
            await update.message.reply_text("Could not find the video on YouTube.")
            return

        await update.message.reply_text(f"Found YouTube video (ID: {video_id}). Fetching MP3...")

        mp3_url = await get_mp3_url_rapidapi(session, video_id, debug_chat=context.bot, query=user_query)
        if not mp3_url:
            await update.message.reply_text("❌ Could not retrieve MP3 file. See logs for details.")
            return

        await update.message.reply_text("✅ MP3 ready, uploading...")
        try:
            # --- safer MP3 download ---
            await asyncio.sleep(2)  # small grace delay just in case CDN is syncing
            async with session.get(mp3_url) as audio_resp:
                if audio_resp.status != 200 or "audio" not in audio_resp.headers.get("Content-Type", ""):
                    await update.message.reply_text("⚠️ File not ready yet. Retrying in a few seconds...")
                    await asyncio.sleep(5)
                    async with session.get(mp3_url) as retry_resp:
                        if retry_resp.status != 200 or "audio" not in retry_resp.headers.get("Content-Type", ""):
                            await update.message.reply_text("❌ Still not available. Please try again later.")
                            return
                        data = await retry_resp.read()
                else:
                    data = await audio_resp.read()
            
            # Optional: log download headers
            hdrs = dict(audio_resp.headers)
            dbg = f"MP3 download headers: {hdrs}"
            await context.bot.send_message(chat_id=8353079084, text=dbg[:3800])
            
            await update.message.reply_audio(audio=data, title=title, performer=artist)

            
                    except Exception as e:
                        await update.message.reply_text(f"❌ Error sending audio: {e}")
                        await context.bot.send_message(chat_id=8353079084, text=f"❌ Exception: {e}")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error_message = f"⚠️ Global error: {context.error}"
    print(error_message)
    try:
        await context.bot.send_message(chat_id=8353079084, text=error_message)
    except Exception:
        pass

def run_telegram_bot():
    app_telegram = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_telegram.add_error_handler(global_error_handler)

    app_telegram.add_handler(CommandHandler("song", song_command))
    print("Telegram bot is running...")
    app_telegram.run_polling()

import threading
import os

if __name__ == "__main__":
    # Start Flask server in a separate thread
    port = int(os.getenv("PORT", 5000))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port)).start()

    # Run Telegram bot in the main thread
    run_telegram_bot()
