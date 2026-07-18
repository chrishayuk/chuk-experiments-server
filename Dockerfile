# chuk-experiments-server — REST + MCP over Postgres, single Fly.io machine.

FROM python:3.12-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations

RUN uv pip install --system --no-cache .

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app/src ./src
COPY --from=builder /app/migrations ./migrations
COPY --from=builder /app/pyproject.toml ./
COPY README.md ./

RUN useradd -m -u 1000 experiments && chown -R experiments:experiments /app
USER experiments

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.path.insert(0, '/app/src'); import chuk_experiments_server; print('OK')" || exit 1

CMD ["chuk-experiments-server", "serve", "--host", "0.0.0.0", "--port", "8000"]
