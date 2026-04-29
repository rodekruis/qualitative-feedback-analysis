#!/usr/bin/env bash
# Fix ownership of named-volume mount points in /workspace.
# Docker creates these as root before the container user takes effect.
set -euo pipefail

VOLUME_DIRS=(
    /workspace/.venv
    /home/dev/.claude
    /commandhistory
)

for dir in "${VOLUME_DIRS[@]}"; do
    if [ -d "$dir" ]; then
        chown -R dev:dev "$dir"
    fi
done
