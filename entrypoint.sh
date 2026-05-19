#!/usr/bin/env sh
# Container entrypoint: run pre-start migrations, then exec the app server.
#
# Migrations run under a session-scoped Postgres advisory lock (see
# qfa/cli/migrate.py), so concurrent replicas serialise safely.
#
# Run with WORKDIR set to the project root so the Alembic CLI can find
# ./alembic.ini.

set -eu

.venv/bin/python -m qfa.cli.migrate

exec .venv/bin/gunicorn qfa.main:app --worker-class asgi --bind 0.0.0.0:8000
