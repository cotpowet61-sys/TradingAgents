# TradeVision Portal — прод-образ (python 3.12 slim, непривилегированный пользователь).
# Сборка:  docker build -f portal/Dockerfile -t tradevision-portal .
# Запуск:  см. docker-compose.yml (том на /data, .env через env_file)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORTAL_DATA_DIR=/data \
    PORTAL_PORT=3005

# Зависимости отдельным слоем — кэш не сбрасывается при правке кода
COPY portal/requirements.txt /tmp/requirements.txt
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends curl \
 && pip install -q --upgrade pip \
 && pip install -q -r /tmp/requirements.txt \
 && apt-get purge -y --auto-remove \
 && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt

WORKDIR /app
# Код портала + backend (портал читает модули движка как библиотеку)
COPY portal/ /app/portal/
COPY backend/ /app/backend/

RUN useradd -r -u 10001 -g root -s /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown -R app:root /data /app
USER app

WORKDIR /app/portal
EXPOSE 3005
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORTAL_PORT}/api/health" > /dev/null || exit 1

# proxy-headers + доверие только внутренней сети compose
CMD ["sh", "-c", "python -m uvicorn server:app --host 0.0.0.0 --port ${PORTAL_PORT} --proxy-headers --forwarded-allow-ips='*' --no-server-header"]
