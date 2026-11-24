FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py .
COPY mongodb.py .
COPY spotify_music_bot.py .
COPY health_server.py .

RUN mkdir -p /app/downloads

EXPOSE 8080

CMD ["sh", "-c", "python health_server.py & exec python spotify_music_bot.py"]
