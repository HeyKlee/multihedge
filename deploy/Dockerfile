# MultiHedge Docker image for XORA (Linux) — sister container to AutoHedge.
#
# Runs 2 long-lived processes as ONE container sharing a single multihedge.db:
#   1x loom      (paper engine, ticks every 30s per coin)
#   1x dash_web  (stdlib HTTP :9052, tailnet-local read-only UI)
#
# Built context = multihedge/ root. Stdlib-only runtime + venv deps are not
# needed here; the code imports pricefeed/paper/strategy/multihedge + reuses
# AutoHedge's .env for JUPITER_API_KEY / SOLANA keys (bind-mounted).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1

# supervisord to keep both long-lived procs alive in one container.
RUN apt-get update && apt-get install -y --no-install-recommends \
        supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# The one-shot independent containers (shadow scalper, agent, signer) run as
# uid 1000 with an --env-file / bind-mounted DB. Make /app writable so SQLite
# can place WAL/journal next to a bind-mounted single file without requiring
# root or leaking the .env dirs into the container. No secrets live in /app.
RUN chmod 0777 /app

# Copy only the multihedge package.
COPY *.py /app/
# config is read at runtime; copy a default so the container self-starts.
COPY config.yaml /app/config.yaml

# Runtime deps: httpx + PyYAML + solders/solana for live fills (via chain) +
# FastAPI/uvicorn for the AutoHedge-style dashboard.
RUN pip install --no-cache-dir httpx PyYAML solders solana==0.30.2 python-dotenv fastapi uvicorn

# supervisord config + entrypoint.
COPY deploy/multihedge.supervisord.conf /etc/supervisor/conf.d/multihedge.conf
COPY deploy/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 9052

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["supervisord", "-c", "/etc/supervisor/conf.d/multihedge.conf", "-n"]