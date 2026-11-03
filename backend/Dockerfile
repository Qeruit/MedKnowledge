FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# PostgreSQL 17 client tools are needed by the existing backup/restore code.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates postgresql-client-17 poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir -r /app/requirements.lock \
    && python -m pip check

COPY app /app/app
COPY scripts /app/scripts
COPY schema.sql /app/schema.sql
ENV TIKTOKEN_CACHE_DIR=/app/tiktoken-cache
COPY assets/tiktoken/9b5ad71b2ce5302211f9c61530b329a4922fc6a4 /app/tiktoken-cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4
RUN echo '223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7  /app/tiktoken-cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4' | sha256sum -c - \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home app \
    && mkdir -p /app/storage/md /app/backups /app/restores /app/.credentials \
    && chown -R app:app /app/storage /app/backups /app/restores /app/.credentials

USER 10001:10001
EXPOSE 8010
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--proxy-headers", "--forwarded-allow-ips", "*"]
