FROM python:3.12-slim

# FFmpeg plays the generated speech into Discord voice
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Show logs immediately in Railway's log viewer
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
