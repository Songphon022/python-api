FROM python:3.11-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && pip install --no-cache-dir -r requirements.txt \
    && rm -rf /var/lib/apt/lists/*

COPY main.py ./main.py

RUN mkdir -p /app/downloads

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:create_api_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
