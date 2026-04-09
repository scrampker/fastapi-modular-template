#!/bin/bash
# ScottyCore Sync Watcher — Tmux Pane
# ====================================
# Run this in a dedicated tmux pane to see sync reports as they arrive.
#
# Usage:
#   ./scripts/sync-pane.sh
#
# Or from tmux:
#   Ctrl+B, % (split pane)
#   /script/scottycore/scripts/sync-pane.sh

REPORTS_DIR="/script/scottycore/data/sync-reports"
LOG_FILE="/script/scottycore/data/sync-watcher.log"

echo "╔══════════════════════════════════════════════╗"
echo "║     ScottyCore Sync Watcher — Live Feed      ║"
echo "╠══════════════════════════════════════════════╣"
echo "║  Reports: $REPORTS_DIR"
echo "║  Log:     $LOG_FILE"
echo "║  Ctrl+C to stop                              ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Show last report if exists
LAST_REPORT=$(ls -t "$REPORTS_DIR"/sync_*.md 2>/dev/null | head -1)
if [ -n "$LAST_REPORT" ]; then
    echo "--- Last Report: $(basename "$LAST_REPORT") ---"
    cat "$LAST_REPORT"
    echo ""
    echo "--- Waiting for new activity... ---"
    echo ""
fi

# Tail the log file (create if missing)
touch "$LOG_FILE"
tail -f "$LOG_FILE"
