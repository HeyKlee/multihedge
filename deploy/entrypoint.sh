#!/usr/bin/env bash

# Entrypoint for the MultiHedge container. Loads .env (JUPITER_API_KEY,
# SOLANA keys) before supervisord starts the daemons. Mirrors AutoHedge's.
set -e
if [ -f /app/.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /app/.env
  set +a
fi
exec "$@"