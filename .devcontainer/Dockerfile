FROM ghcr.io/astral-sh/uv:latest AS uv
FROM debian:bookworm-slim

# ── Build args ──────────────────────────────────────────────────────
ARG PYTHON_VERSION=3.12
ARG USERNAME=dev
ARG USER_UID=1000
ARG USER_GID=${USER_UID}

# ── Layer 1: System packages (changes very rarely) ──────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Dev tools
    git zsh fzf jq vim sudo less curl ca-certificates gpg make \
    # Firewall
    iptables ipset iproute2 dnsutils dnsmasq \
    # Claude Code dependencies
    sox socat \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 2: GitHub CLI (not in default Debian repos) ───────────────
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 3: uv + Python ────────────────────────────────────────────
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install ${PYTHON_VERSION}

# ── Layer 4: Non-root user + firewall sudo ──────────────────────────
RUN groupadd --gid ${USER_GID} ${USERNAME} \
    && useradd --uid ${USER_UID} --gid ${USER_GID} -m -s /bin/zsh ${USERNAME} \
    && echo "${USERNAME} ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh, /usr/local/bin/fix-volume-ownership.sh" \
        > /etc/sudoers.d/${USERNAME}-firewall \
    && chmod 0440 /etc/sudoers.d/${USERNAME}-firewall

COPY init-firewall.sh /usr/local/bin/init-firewall.sh
COPY scripts/fix-volume-ownership.sh /usr/local/bin/fix-volume-ownership.sh
RUN printf '#!/bin/sh\nexec claude --dangerously-skip-permissions "$@"\n' > /usr/local/bin/claudesp \
    && chmod +x /usr/local/bin/init-firewall.sh /usr/local/bin/fix-volume-ownership.sh /usr/local/bin/claudesp

# Pre-create directories that will be volume mount targets so they're
# owned by dev, not root (Docker creates missing mount targets as root).
RUN mkdir -p /home/${USERNAME}/.claude /commandhistory /workspace/.venv \
    && chown -R ${USERNAME}:${USERNAME} /home/${USERNAME}/.claude /commandhistory /workspace/.venv

USER ${USERNAME}

# ── Oh My Zsh + Powerlevel10k (installed as dev) ────────────────────
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended \
    && git clone --depth=1 https://github.com/romkatv/powerlevel10k.git \
        ${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k \
    && git clone --depth=1 https://github.com/zsh-users/zsh-autosuggestions \
        ${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-autosuggestions \
    && git clone --depth=1 https://github.com/zsh-users/zsh-syntax-highlighting \
        ${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting

COPY --chown=${USERNAME}:${USERNAME} dotfiles/.zshrc dotfiles/.p10k.zsh /home/${USERNAME}/

# ── Claude Code (native binary) ────────────────────────────────────
WORKDIR /tmp
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/${USERNAME}/.local/bin:${PATH}"

# ── pre-commit (installed via uv as a standalone tool) ──────────────
RUN uv tool install pre-commit

WORKDIR /workspace
