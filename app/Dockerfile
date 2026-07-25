# Multi-arch base: the same Dockerfile builds natively on linux/arm64 (Oracle
# Ampere A1) and on linux/amd64.
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Every dependency ships an aarch64 wheel today; the toolchain is kept so a
# future one without a wheel builds instead of failing the deploy.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY whatsapp_bot ./whatsapp_bot

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /data \
    && chown bot:bot /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER bot
VOLUME ["/data"]

# Overridden by docker-compose for the worker.
CMD ["uvicorn", "whatsapp_bot.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
