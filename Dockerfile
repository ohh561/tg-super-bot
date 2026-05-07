FROM python:3.10-slim

WORKDIR /app

COPY bot.py .

RUN pip install --no-cache-dir python-telegram-bot[job-queue] aiohttp

CMD ["python", "bot.py"]
