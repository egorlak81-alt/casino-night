FROM python:3.11-slim

RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD gunicorn bot:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
