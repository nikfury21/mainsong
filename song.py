
import os
import aiohttp
import asyncio
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import threading
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped


# Environment variables (set these in Render environment or your OS)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")

# Initialize Spotify client (blocking, but fast)
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=SPOTIFY_CLIENT_ID,
                                                           client_secret=SPOTIFY_CLIENT_SECRET))

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
USERBOT_SESSION = os.getenv("USERBOT_SESSION")

userbot = Client(USERBOT_SESSION, api_id=API_ID, api_hash=API_HASH)
voice = PyTgCalls(userbot)


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
                dbg = f"[Attempt {attempt+1}] RapidAPI status={resp.status}, data={data}"
                print(dbg)
                if debug_chat:
                    await debug_chat.send_message(chat_id=8353079084, text=dbg[:3800])

                if resp.status != 200:
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
                await debug_chat.send_message(chat_id=8353079084, text=msg)
            await asyncio.sleep(2)
    return None


async def song_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global sp
    user_query = " ".join(context.args)
    if not user_query:
        await update.message.reply_text("Please provide a song name after /song.")
        return

    await update.message.reply_text(f"Searching Spotify for '{user_query}'...")
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
            await context.bot.send_message(chat_id=8353079084, text=msg)
            # Recreate Spotify client and retry
            await asyncio.sleep(2)
            try:
                sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                    client_id=SPOTIFY_CLIENT_ID,
                    client_secret=SPOTIFY_CLIENT_SECRET
                ))
            except Exception as e2:
                await context.bot.send_message(chat_id=8353079084, text=f"Reinit error: {e2}")
                await asyncio.sleep(2)
    else:
        await update.message.reply_text("❌ Spotify connection failed after 3 retries.")
        return

    # If still no results after all search terms, go directly to YouTube
    if not results or not results.get("tracks", {}).get("items", []):
        await update.message.reply_text(
            f"No Spotify results for '{user_query}'. Trying YouTube directly..."
        )
        async with aiohttp.ClientSession() as session:
            video_id = await search_youtube_video_id(session, user_query)
            if not video_id:
                await update.message.reply_text("Could not find anything on YouTube either.")
                return
            mp3_url = await get_mp3_url_rapidapi(session, video_id, debug_chat=context.bot)
            if mp3_url:
                await update.message.reply_text(f"🎧 Found on YouTube:\n{mp3_url}")
            else:
                await update.message.reply_text("❌ Couldn’t fetch MP3 from YouTube.")
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

        await update.message.reply_text("✅ MP3 link received, verifying...")

        # Verify link really points to an MP3 file
        try:
            async with session.head(mp3_url, timeout=10) as head_resp:
                content_type = head_resp.headers.get("Content-Type", "")
                dbg = f"HEAD check -> status={head_resp.status}, content_type={content_type}"
                await context.bot.send_message(chat_id=8353079084, text=dbg[:3800])

                # If it looks like a valid MP3/audio file, send link directly
                if head_resp.status == 200 and "audio" in content_type.lower():
                    group_id = -1001234567890  # replace with your group’s ID
                    msg = (
                        f"🎵 *{title}* by *{artist}*\n\n"
                        f"[▶️ Click to play or download MP3]({mp3_url})"
                    )
                    await context.bot.send_message(
                        chat_id=group_id,
                        text=msg,
                        parse_mode="Markdown",
                        disable_web_page_preview=False
                    )
                    await update.message.reply_text("✅ Song link sent to group!")
                    return
        except Exception as e:
            await context.bot.send_message(chat_id=8353079084, text=f"HEAD check error: {e}")

        # fallback if HEAD fails or not audio
        # fallback if HEAD fails or not audio — send MP3 link only
        await update.message.reply_text(mp3_url)


async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = " ".join(context.args)
    if not user_query:
        await update.message.reply_text("Please provide a song name after /play.")
        return

    await update.message.reply_text(f"🎵 Searching and playing '{user_query}'...")

    async with aiohttp.ClientSession() as session:
        video_id = await search_youtube_video_id(session, user_query)
        if not video_id:
            await update.message.reply_text("Could not find on YouTube.")
            return

        mp3_url = await get_mp3_url_rapidapi(session, video_id, debug_chat=context.bot)
        if not mp3_url:
            await update.message.reply_text("Could not fetch MP3 link.")
            return

        # ✅ Join VC and stream
        chat_id = update.effective_chat.id
        await play_audio(chat_id, mp3_url)
        await update.message.reply_text("✅ Playing in voice chat!")


async def play_audio(chat_id: int, mp3_url: str):
    try:
        await voice.join_group_call(
            chat_id,
            AudioPiped(mp3_url)
        )
    except Exception as e:
        print(f"VC join error: {e}")


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
    app_telegram.add_handler(CommandHandler("play", play_command))

    print("Telegram bot is running...")
    app_telegram.run_polling()

import threading
import os

if __name__ == "__main__":
    # Start Flask server in a separate thread
    port = int(os.getenv("PORT", 5000))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port)).start()

    async def start_all():
        await userbot.start()
        await voice.start()

    # Start userbot and voice client
    asyncio.run(start_all())

    # Run Telegram bot in main thread
    run_telegram_bot()
