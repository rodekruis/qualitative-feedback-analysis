#!/usr/bin/env sh
# Container entrypoint: run pre-start migrations, then exec the app server.
#
# Migrations run under a session-scoped Postgres advisory lock (see
# qfa/cli/migrate.py), so concurrent replicas serialise safely.
#
# Run with WORKDIR set to the project root so the Alembic CLI can find
# ./alembic.ini.
#
# --timeout is a gunicorn *heartbeat* check (the asgi worker ticks it from a
# coroutine every second), not a request-duration limit — a request that only
# awaits I/O never trips it. Left unset it defaulted to gunicorn's 30s, and a
# request running synchronous CPU work (Presidio anonymisation, ONNX
# embedding) on the event loop thread starved that heartbeat well before the
# request itself was anywhere near LLM_TIMEOUT_SECONDS (230s), so gunicorn
# SIGKILLed workers that were still on-budget. 60s now: with #325's
# anonymisation and embedding calls off the event loop thread
# (asyncio.to_thread), the heartbeat coroutine should never be starved by
# request work, so this is sized to catch a genuinely wedged worker fast, not
# to accommodate a slow request.

set -eu

.venv/bin/python -m qfa.cli.migrate

exec .venv/bin/gunicorn qfa.main:app --worker-class asgi --timeout 120 --bind 0.0.0.0:8000
