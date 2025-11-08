# ─── Base Image ─────────────────────────────
FROM python:3.10-slim

# ─── Set Environment Variables ─────────────
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=1.6.0

# ─── Install system dependencies ───────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ffmpeg \
        wget \
        curl \
        git \
        libffi-dev \
        libssl-dev \
        libpq-dev \
        python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ─── Set working directory ─────────────────
WORKDIR /app

# ─── Copy requirements ─────────────────────
COPY requirements.txt .

# ─── Upgrade pip and install dependencies ─
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# ─── Copy bot source code ──────────────────
COPY . .

# ─── Expose port if needed (for Flask webhooks) ─
EXPOSE 5000

# ─── Set default command ───────────────────
CMD ["python", "song.py"]
