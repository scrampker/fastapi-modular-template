#!/usr/bin/env bash
# pre-commit-promote-nudge — suggest /promote invocations when the staged diff
# contains code that might belong in scottycore.
#
# Installation:
#   Symlink or copy this into each app's .git/hooks/pre-commit. Safe to run as
#   a wrapper around an existing pre-commit hook — this script never blocks the
#   commit (always exits 0).
#
# Behaviour:
#   - Runs `claude -p` on the staged diff with a narrow extraction-candidate prompt.
#   - If the model flags a candidate, prints a single suggestion line.
#   - Otherwise prints nothing.
#   - Caches results by diff hash (5 min TTL) to avoid re-querying on identical
#     repeat commits (amend loops, rebase, etc.).
#   - If `claude` is missing, the hook silently no-ops — commits still work.
#
# Toggle off entirely:
#   export SCOTTYCORE_SKIP_PROMOTE_NUDGE=1

set -u

# Early outs
[ "${SCOTTYCORE_SKIP_PROMOTE_NUDGE:-}" = "1" ] && exit 0
command -v claude >/dev/null 2>&1 || exit 0

DIFF=$(git diff --cached --no-color -U0 -- '*.py' 2>/dev/null || true)
if [ -z "$DIFF" ]; then
    exit 0
fi

# Cache by diff hash
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/scottycore-promote-nudge"
mkdir -p "$CACHE_DIR"
HASH=$(printf '%s' "$DIFF" | sha256sum | cut -d' ' -f1)
CACHE_FILE="$CACHE_DIR/$HASH"
CACHE_TTL=300

if [ -f "$CACHE_FILE" ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y "$CACHE_FILE" 2>/dev/null || echo 0) ))
    if [ "$AGE" -lt "$CACHE_TTL" ]; then
        cat "$CACHE_FILE"
        exit 0
    fi
fi

PROMPT='You are a terse classifier for a pre-commit hook. Below is a git diff staged for commit in a Scotty-family app repo. Decide whether any of the added code is generic enough to belong in the shared scottycore library.

Look for:
- Middleware, utilities, security helpers, config patterns, notification helpers
- Anything that does NOT reference app-specific domain concepts (parsers, scans, scribes, sync protocols, etc.)

Be strict — err on the side of KEEP. A new domain handler, a parser, a scan module, a domain-specific service is NOT a candidate.

Output exactly one line. No prose, no markdown.
- If you find a candidate: `PROMOTE <path> — <3-to-8-word reason>`
- Otherwise: `KEEP`

Diff:
'"$DIFF"

RESULT=$(claude -p "$PROMPT" --output-format json 2>/dev/null \
    | python3 -c '
import json, sys
try:
    env = json.loads(sys.stdin.read())
    text = env.get("result") or ""
except Exception:
    sys.exit(0)
for line in text.splitlines():
    line = line.strip()
    if line.startswith("PROMOTE "):
        print(line)
        break
    if line == "KEEP":
        print("KEEP")
        break
')

if [ -z "$RESULT" ] || [ "$RESULT" = "KEEP" ]; then
    # Cache negative result too (saves a re-query on amend)
    echo "" > "$CACHE_FILE"
    exit 0
fi

# Parse: PROMOTE <path> — <reason>
PATH_PART=$(echo "$RESULT" | sed -E 's/^PROMOTE +([^ ]+).*/\1/')
REASON=$(echo "$RESULT" | sed -E 's/^PROMOTE +[^ ]+ *[—-]+ *//')

MESSAGE=$(cat <<EOF

──────────────────────────────────────────────
  extraction candidate: $PATH_PART
  reason: $REASON
  to promote: /promote $PATH_PART
  (commit not blocked; this is a nudge only)
──────────────────────────────────────────────
EOF
)

echo "$MESSAGE"
echo "$MESSAGE" > "$CACHE_FILE"
exit 0
