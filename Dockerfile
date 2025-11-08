# Use official Python 3.10 slim image
FROM python:3.10-slim

# Install ffmpeg for audio streaming
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

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

# Run the bot (Flask + Telegram bot concurrently)
CMD ["python", "song.py"]
