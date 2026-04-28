# Devcontainer

A fully configured Python development environment with uv, pre-commit,
Claude Code, and a default-deny network firewall. Designed to run Claude
Code with `--dangerously-skip-permissions` safely using network restrictions
and scoped credentials.

## What's inside

| Component | Details |
|---|---|
| Base image | `debian:bookworm-slim` |
| Python | Pinned version via [uv](https://github.com/astral-sh/uv) (see `PYTHON_VERSION` build arg) |
| Linting | pre-commit — baked into the image; ruff, ty — installed via `uv sync` from the project's dev dependencies |
| Code search | `ripgrep` (apt), `ast-grep` / `sg` (installed via `uv tool install ast-grep-cli`) for structural AST-based search |
| Shell | zsh + Oh My Zsh + Powerlevel10k + autosuggestions + syntax highlighting |
| Claude Code | Native binary with pre-configured plugins and MCP servers |
| GitHub CLI | `gh`, authenticated via `GH_TOKEN` from `.env` |
| Firewall | Default-deny egress via iptables + dnsmasq + ipset |

## Prerequisites

- Docker and Docker Compose
- An IDE that supports devcontainers (VS Code, JetBrains, etc.) or the
  [devcontainer CLI](https://github.com/devcontainers/cli)
- An Anthropic API key (`ANTHROPIC_API_KEY`) for Claude Code
- A GitHub fine-grained PAT (see [GitHub token](#github-token) below)

## Setup

1. **Create a GitHub token** — see [GitHub token](#github-token) below.

2. **Write your keys to `.devcontainer/.env`** (gitignored):

   ```bash
   cat > .devcontainer/.env <<'EOF'
   ANTHROPIC_API_KEY=sk-ant-XXXXX
   GH_TOKEN=github_pat_XXXXX
   EOF
   ```

3. **Open in your IDE** or start manually:

   ```bash
   devcontainer up --workspace-folder .
   devcontainer exec --workspace-folder . zsh
   
   # or shorthand scripts:
   dcrebuild  # build or rebuild the container
   dcexec zsh  # start the container and open a shell
   dcc        # run Claude Code with --dangerously-skip-permissions
   dcdown     # stop the container
   ```

4. **First run** happens automatically — `postCreateCommand` runs
   `uv sync`, installs pre-commit hooks, and sets up Claude Code plugins.

## Architecture

### File overview

```
.devcontainer/
  devcontainer.json          # Entry point — ties everything together
  docker-compose.yml         # Service definitions + volume mounts
  Dockerfile                 # Image layers: system pkgs, uv, Python, zsh, Claude
  init-firewall.sh           # Default-deny egress firewall (runs on every start)
  claude-setup.json          # Declarative Claude Code plugin/MCP config
  dotfiles/
    .zshrc                   # Oh My Zsh config (Powerlevel10k theme)
    .p10k.zsh                # Powerlevel10k prompt configuration
    statusline-command.sh    # Claude Code status line script (copied into ~/.claude on create)
  scripts/
    post-create.sh           # First-run setup (uv sync, pre-commit, Claude plugins, status line)
    fix-volume-ownership.sh  # Fixes root-owned volume mount points
  bin/                       # Host-side convenience wrappers (add to PATH or invoke directly)
    dcc                      # Run Claude Code with --dangerously-skip-permissions in the devcontainer
    dcdown                   # Stop the container (preserves volumes)
    dcexec                   # Start container if needed, then exec a command
    dcrebuild                # Rebuild the image and recreate the container
```

### Lifecycle

1. **Build** (`Dockerfile`): Installs system packages, uv, Python, zsh/
   oh-my-zsh, GitHub CLI, Claude Code, and pre-commit. Creates a non-root
   `dev` user (UID 1000) with scoped sudo for the firewall script only.

2. **Start** (`postStartCommand`): Runs the firewall script as root via
   sudo. This runs on every container start, not just the first time.

3. **Create** (`postCreateCommand`): Runs once after the container is first
   created. Installs Python dependencies (`uv sync`), sets up pre-commit
   hooks, configures Claude Code plugins/MCP servers from `claude-setup.json`,
   and provisions the Claude Code status line by copying
   `dotfiles/statusline-command.sh` into `~/.claude/` and merging the
   `statusLine` key into `~/.claude/settings.json`.

### Volume strategy

The project directory is bind-mounted into the container so edits from the
host IDE and the container are always in sync. Generated directories that
differ between host and container (like `.venv`) are masked with named
volumes to prevent cross-contamination.

| Mount | Type | Purpose |
|---|---|---|
| `..:/workspace` | bind | Project source code (read-write) |
| `venv:/workspace/.venv` | named volume | Isolates container's virtualenv from host |
| `*-claude-config` | named volume | Persists Claude Code config across rebuilds |
| `*-shell-history` | named volume | Persists zsh history across rebuilds |

The claude-config and shell-history volume names include the project folder
name (`${localWorkspaceFolderBasename}-*`) so multiple devcontainers don't
collide.

### Network firewall

The container runs a default-deny egress firewall that only allows outbound
connections to explicitly whitelisted domains. This is the key safety
mechanism for running Claude Code with `--dangerously-skip-permissions`.

**How it works:**

1. `dnsmasq` runs as the container's DNS resolver on localhost
2. For each allowed domain, dnsmasq's `--ipset` option adds resolved IPs
   to a netfilter ipset on every DNS lookup
3. `iptables` allows outbound HTTPS only to IPs in the ipset
4. CDN IP rotation is handled naturally — each DNS lookup updates the ipset

**Allowed destinations:**

| Service | Hosts |
|---|---|
| Claude Code | `api.anthropic.com`, `claude.ai`, `platform.claude.com`, `sentry.io`, `statsig.anthropic.com` |
| GitHub | `github.com`, `api.github.com`, `objects.githubusercontent.com` + published CIDR ranges |
| Python packages | `pypi.org`, `files.pythonhosted.org` |
| npm (MCP servers) | `registry.npmjs.org` |
| Docker network | Auto-detected subnet (for database sidecars) |
| DNS | UDP/TCP port 53 (locked to upstream resolver only) |

Everything else is rejected. To add domains, edit the `ALLOWED_HOSTS` array
in `init-firewall.sh`. Changes take effect on the next container start.

The firewall requires `NET_ADMIN` and `NET_RAW` capabilities, granted in
`docker-compose.yml`. The `dev` user can only run `init-firewall.sh` and
`fix-volume-ownership.sh` via sudo — no other root commands.

### Claude Code setup

`claude-setup.json` declares plugins and MCP servers to install inside the
container. This runs during `postCreateCommand` and is non-fatal — failures
don't block the dev environment.

```json
{
  "marketplaces": ["anthropics/claude-plugins-official", "..."],
  "plugins": ["superpowers", "..."],
  "mcp_servers": [
    { "name": "context7", "command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"] }
  ]
}
```

### GitHub token

The `GH_TOKEN` in `.devcontainer/.env` controls what the container (and
Claude Code) can do on GitHub. **Do not copy your host's full `gh` token**
— it typically has broad access to all your repositories.

Instead, create a **fine-grained personal access token** scoped to only
this repository. This limits the blast radius if the token is misused
(e.g., by Claude Code with `--dangerously-skip-permissions`).

**Create one here** (link pre-fills the repository scope):

> https://github.com/settings/personal-access-tokens/new?scoped_to=OWNER&scoped_repo=REPO

Replace `OWNER` and `REPO` with your GitHub username and repository name.

Grant these permissions:

| Permission    | Access       | Why                                                      |
|---------------|--------------|----------------------------------------------------------|
| Contents      | Read & Write | Push commits, read files                                 |
| Issues        | Read & Write | Create/comment on issues                                 |
| Metadata      | Read (auto)  | Required by GitHub                                       |
| Pull requests | Read & Write | Create/review PRs                                        |
| Actions       | Read         | Read workflow status, e.g. to see whether PRs are green |

Leave **everything else** at "No access". In particular:

- **Workflows** — omit to prevent modifying CI pipelines. GitHub blocks
  pushes that touch `.github/workflows/` without this permission.
- **Administration** — omit to prevent changes to repo settings.
- **Secrets** — omit to prevent reading or writing Actions secrets.

After creating the token, add it to `.devcontainer/.env` (see [Setup](#setup)
step 2).

### Exposing ports (Django, FastAPI, etc.)

To reach a dev server running inside the container from your host browser,
forward the port in `docker-compose.yml`:

```yaml
services:
  dev:
    ports:
      - "127.0.0.1:8000:8000"   # host:container — bind to loopback only
```

Then start your server bound to all interfaces inside the container:

```bash
# Django
uv run python manage.py runserver 0.0.0.0:8000

# FastAPI / uvicorn
uv run uvicorn myapp:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000` on your host.

**Multiple ports** — add one `ports` entry per port:

```yaml
ports:
  - "127.0.0.1:8000:8000"
  - "127.0.0.1:5678:5678"   # e.g. debugpy
```

**VS Code / JetBrains port forwarding** — IDEs also support forwarding ports
via the devcontainer spec. Add a `forwardPorts` list to `devcontainer.json`
as an alternative to (or in addition to) the `docker-compose.yml` approach:

```json
"forwardPorts": [8000]
```

The IDE will then auto-forward the port and open it in a browser tab.

> **Security note:** Always bind the host side to `127.0.0.1` (not `0.0.0.0`)
> to avoid exposing the dev server to other machines on your local network.
> The container's egress firewall restricts *outbound* connections; it does
> not limit what the host can reach on forwarded ports.

After editing `docker-compose.yml`, rebuild or restart the container for
the port mapping to take effect.

### Adding a PostgreSQL sidecar

The `docker-compose.yml` contains a commented-out PostgreSQL service.
To enable it:

1. Uncomment the `db` service and the `pgdata` volume
2. Add `depends_on` to the `dev` service:
   ```yaml
   depends_on:
     db:
       condition: service_healthy
   ```
3. Add a `DATABASE_URL` to the `dev` service's `environment`
4. Rebuild the container

## Opinions and assumptions

- **uv manages Python** — the base image has no system Python. uv installs
  the exact version specified by `PYTHON_VERSION` and manages the virtualenv.
- **Non-root user** — the container runs as `dev` (UID 1000) to match
  typical host UIDs and avoid permission issues on bind mounts.
- **Network-first security** — rather than trying to restrict what Claude
  Code can do locally, the firewall limits where it can connect. It can
  read/write any file in the workspace, but it can only talk to the
  internet through the whitelist.
- **Volume isolation** — `.venv` is a named volume, not synced to the host.
  This means `uv sync` must run inside the container. The tradeoff is
  zero permission conflicts between host and container toolchains.
- **Git HTTPS rewrite** — the container rewrites `git@github.com:` URLs to
  HTTPS so that `GH_TOKEN` authentication works without SSH keys in the
  container.
- **Firewall runs on every start** — `postStartCommand` re-applies the
  firewall on each container start, not just first creation. This ensures
  the firewall survives container restarts.
