# scottycore-runner: Forgejo Actions image for ScottyCore CI/CD workflows
#
# Contains everything the per-app upgrade-review workflows need:
#   - Python 3.11 + pip + build tooling (to build and test the scottycore package)
#   - Node.js 20 (required by Claude Code CLI)
#   - Claude Code CLI (for autonomous upgrade-PR review agents)
#   - git, curl, jq, gh (for repo ops, Forgejo API calls, mirror-to-GitHub)
#
# Build (on CT118):
#   docker build -t scottycore-runner:latest deploy/runner/
#
# Use from workflows:
#   jobs:
#     review:
#       runs-on: [self-hosted, linux]
#       container:
#         image: scottycore-runner:latest
#         options: >-
#           -v /root/.config/scottycore-hubitat.json:/root/.config/scottycore-hubitat.json:ro
#
# Auth model:
#   - Claude Code: bind-mount host /root/.claude into the container (rw).
#     Uses the host's existing Max subscription. No ANTHROPIC_API_KEY, no
#     separate billing. Re-auth on the host when the session expires.
#   - Forgejo API: FORGEJO_TOKEN secret per repo.
#   - Hubitat: config mounted read-only from host (lifted out of sync-watcher.py).

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_MAJOR=20

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git jq python3 python3-pip python3-venv \
        build-essential \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        nodejs gh \
    && rm -rf /var/lib/apt/lists/*

# Python build tooling for cutting scottycore releases (python -m build)
RUN pip install --break-system-packages --no-cache-dir \
        build twine

# Claude Code CLI — used by per-app manager agents for upgrade-PR review
RUN npm install -g @anthropic-ai/claude-code \
    && claude --version

# Hubitat alert helper lifted out of sync-watcher.py (standalone so it outlives the watcher)
COPY notify-hubitat.sh /usr/local/bin/notify-hubitat
RUN chmod +x /usr/local/bin/notify-hubitat

WORKDIR /workspace

# Sanity: everything the workflows call must be on PATH
RUN for cmd in git python3 pip node npm claude gh jq curl notify-hubitat; do \
        command -v "$cmd" >/dev/null || { echo "MISSING: $cmd"; exit 1; }; \
    done \
    && python3 --version && node --version && claude --version

CMD ["/bin/bash"]
