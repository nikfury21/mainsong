# Use official Python 3.10 slim image
FROM python:3.10-slim

# Set working directory inside container
WORKDIR /app

# Copy requirements file
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source code
COPY . .

# Expose port for Flask app
EXPOSE 5000

# Environment variables can be passed at runtime or set here for testing
# ENV TELEGRAM_TOKEN=your_telegram_token
# ENV SPOTIFY_CLIENT_ID=your_spotify_client_id
# ENV SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
# ENV RAPIDAPI_KEY=your_rapidapi_key
# ENV YOUTUBE_API_KEY=your_youtube_api_key
# ENV PORT=5000

# Run the bot (Flask + Telegram bot concurrently)
CMD ["python", "song.py"]
