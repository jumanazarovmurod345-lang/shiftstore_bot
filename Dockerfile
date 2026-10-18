# -------------------------------------------------------------
# Dockerfile для Railway (production)
# -------------------------------------------------------------
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# curl для healthcheck при необходимости, ca-certificates для HTTPS к Telegram и Voyage
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала устанавливаем зависимости (кэширование слоёв)
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Копируем исходный код приложения и миграции
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Не запускаем от root для безопасности
RUN useradd -u 1000 -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Запуск FastAPI сервиса через модуль app
CMD ["python", "-m", "app"]
