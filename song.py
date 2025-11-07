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

async def get_mp3_url_rapidapi(session, video_id: str):
    url = "https://youtube-mp36.p.rapidapi.com/dl"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"
    }
    params = {"id": video_id}
    async with session.get(url, headers=headers, params=params) as resp:
        if resp.status == 200:
            data = await resp.json()
            return data.get("link") or (data.get("formats", [{}])[0].get("url") if data.get("formats") else None)
    return None

async def song_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = " ".join(context.args)
    if not user_query:
        await update.message.reply_text("Please provide a song name after /song.")
        return

    await update.message.reply_text(f"Searching Spotify for '{user_query}'...")
    results = sp.search(q=user_query, limit=1, type="track")
    tracks = results.get("tracks", {}).get("items", [])

    if not tracks:
        await update.message.reply_text(f"No results found on Spotify for '{user_query}'.")
        return

    track = tracks[0]
    title = track["name"]
    artist = track["artists"][0]["name"]
    combined_query = f"{title} {artist}"

    await update.message.reply_text(f"Found on Spotify: {title} by {artist}. Searching YouTube for video...")

    async with aiohttp.ClientSession() as session:
        video_id = await search_youtube_video_id(session, combined_query)
        if not video_id:
            await update.message.reply_text("Could not find the video on YouTube.")
            return

        await update.message.reply_text(f"Found YouTube video (ID: {video_id}). Fetching MP3 download link...")

        mp3_url = await get_mp3_url_rapidapi(session, video_id)
        if not mp3_url:
            await update.message.reply_text("Could not retrieve MP3 file from YouTube.")
            return

        try:
            async with session.get(mp3_url) as audio_resp:
                if audio_resp.status != 200:
                    await update.message.reply_text("Failed to download the MP3 file.")
                    return

                data = await audio_resp.read()
                await update.message.reply_audio(audio=data, title=title, performer=artist)

        except Exception as e:
            await update.message.reply_text(f"Error sending audio: {e}")

def run_telegram_bot():
    app_telegram = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
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

