#!/usr/bin/env sh
# Container entrypoint: run pre-start migrations, then exec the app server.
#
# Migrations run under a session-scoped Postgres advisory lock (see
# qfa/cli/migrate.py), so concurrent replicas serialise safely. When
# DB_TRACK_USAGE is unset or false, the migration step is a no-op and
# control falls through to the server immediately.
#
# Run with WORKDIR set to the project root so the Alembic CLI can find
# ./alembic.ini.

set -eu

if [ "${DB_TRACK_USAGE:-false}" = "true" ]; then
    .venv/bin/python -m qfa.cli.migrate
fi

exec .venv/bin/gunicorn qfa.main:app --worker-class asgi --bind 0.0.0.0:8000
