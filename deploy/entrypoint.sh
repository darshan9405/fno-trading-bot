#!/bin/sh
# Backend entrypoint: seed the instrument whitelist (idempotent, non-fatal),
# then start gunicorn.
set -e

echo "[entrypoint] seeding instruments..."
python scripts/seed_instruments.py || echo "[entrypoint] instrument seed failed (non-fatal; S1 will retry)"

echo "[entrypoint] starting gunicorn..."
exec gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 --access-logfile - --error-logfile - run_backend:app