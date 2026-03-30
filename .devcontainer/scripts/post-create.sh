#!/bin/bash
# First-run setup: install dependencies and configure the environment.
set -e

echo "==> Fixing volume mount ownership..."
sudo /usr/local/bin/fix-volume-ownership.sh

echo "==> Configuring git to use HTTPS (host uses SSH, container uses HTTPS)..."
git config --global url."https://github.com/".insteadOf "git@github.com:"

echo "==> Installing Python dependencies..."
cd /workspace
uv sync

# ── Claude Code plugins & MCP servers (non-fatal) ─────────────────
# Reads .devcontainer/claude-setup.json and installs plugins/MCP servers
# natively so all paths resolve correctly inside the container.
# Failures here never block the dev environment.
CLAUDE_SETUP="/workspace/.devcontainer/claude-setup.json"
if [ -f "$CLAUDE_SETUP" ] && command -v claude >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
    echo "==> Setting up Claude Code plugins and MCP servers..."
    set +e

    jq -r '.marketplaces[]' "$CLAUDE_SETUP" | while read -r marketplace; do
        echo "    Adding marketplace: $marketplace"
        claude plugin marketplace add "$marketplace" --scope user 2>&1
    done

    jq -r '.plugins[]' "$CLAUDE_SETUP" | while read -r plugin; do
        echo "    Installing plugin: $plugin"
        claude plugin install "$plugin" --scope user 2>&1
    done

    while read -r server; do
        name=$(echo "$server" | jq -r '.name')
        cmd=$(echo "$server" | jq -r '.command')
        args=$(echo "$server" | jq -r '.args // [] | .[]')
        echo "    Adding MCP server: $name"
        # shellcheck disable=SC2086
        claude mcp add --scope user "$name" -- "$cmd" $args 2>&1
    done < <(jq -c '.mcp_servers[]' "$CLAUDE_SETUP")

    set -e
    echo "==> Claude Code setup complete."
else
    echo "==> Skipping Claude Code setup (claude-setup.json or claude CLI not found)."
fi

echo "==> Installing pre-commit hooks..."
cd /workspace
pre-commit install

echo "==> Post-create setup complete."
