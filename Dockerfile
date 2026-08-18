FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 5000

ENV PORT=5000
ENV CHECK_INTERVAL=900
ENV CONCURRENCY_LIMIT=10
ENV MAX_PING_MS=1000
ENV TIMEOUT_PING=5
ENV EXTRA_VLESS_URL=""

CMD ["python", "main.py"]
