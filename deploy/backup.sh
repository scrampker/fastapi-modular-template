#!/usr/bin/env bash
# =============================================================================
# backup.sh — Database backup with automatic SQLite/PostgreSQL detection
# =============================================================================
# Usage:
#   bash backup.sh [--keep-days N] [--output-dir /path/to/backups]
#
# Schedule with cron (daily at 2 AM):
#   0 2 * * * /opt/myapp/deploy/backup.sh >> /var/log/myapp/backup.log 2>&1
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
APP_DIR="/opt/myapp"
ENV_FILE="$APP_DIR/.env"
BACKUP_DIR="/var/backups/myapp"
KEEP_DAYS=30
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_NAME="myapp_backup_$TIMESTAMP"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-days)   KEEP_DAYS="$2";   shift 2 ;;
        --output-dir)  BACKUP_DIR="$2";  shift 2 ;;
        *)             echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO]  $*"; }
warn()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN]  $*"; }
error() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*" >&2; }

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    error "Environment file not found: $ENV_FILE"
    exit 1
fi

# Source only the DATABASE_URL variable to avoid polluting the shell
DATABASE_URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | cut -d '=' -f2- | tr -d '"' | tr -d "'")

if [[ -z "$DATABASE_URL" ]]; then
    error "DATABASE_URL not found in $ENV_FILE"
    exit 1
fi

# ---------------------------------------------------------------------------
# Detect database type
# ---------------------------------------------------------------------------
if [[ "$DATABASE_URL" == sqlite* ]]; then
    DB_TYPE="sqlite"
elif [[ "$DATABASE_URL" == postgresql* ]] || [[ "$DATABASE_URL" == postgres* ]]; then
    DB_TYPE="postgresql"
else
    error "Unsupported DATABASE_URL scheme: $DATABASE_URL"
    exit 1
fi

info "Database type detected: $DB_TYPE"

# ---------------------------------------------------------------------------
# Create backup directory
# ---------------------------------------------------------------------------
mkdir -p "$BACKUP_DIR"

# ---------------------------------------------------------------------------
# Perform backup
# ---------------------------------------------------------------------------
DUMP_FILE="$BACKUP_DIR/${BACKUP_NAME}.dump"

case "$DB_TYPE" in

    sqlite)
        # Extract the file path from the URL
        # Handles: sqlite+aiosqlite:///./myapp.db  or  sqlite:////abs/path.db
        DB_FILE=$(echo "$DATABASE_URL" | sed -E 's|sqlite(\+[a-z]+)?:///||' | sed 's|^\./||')

        if [[ "$DB_FILE" != /* ]]; then
            DB_FILE="$APP_DIR/$DB_FILE"
        fi

        if [[ ! -f "$DB_FILE" ]]; then
            error "SQLite database file not found: $DB_FILE"
            exit 1
        fi

        info "Backing up SQLite database: $DB_FILE"

        # Use SQLite's online backup to avoid corruption on live databases
        sqlite3 "$DB_FILE" ".backup '$DUMP_FILE.sqlite'"
        mv "$DUMP_FILE.sqlite" "$DUMP_FILE"
        ;;

    postgresql)
        # Parse connection string
        # postgresql+asyncpg://user:pass@host:port/dbname
        CLEAN_URL=$(echo "$DATABASE_URL" | sed -E 's|\+[a-z]+://|://|')
        PG_USER=$(echo "$CLEAN_URL" | sed -E 's|postgresql://([^:]+):.*|\1|')
        PG_PASS=$(echo "$CLEAN_URL" | sed -E 's|postgresql://[^:]+:([^@]+)@.*|\1|')
        PG_HOST=$(echo "$CLEAN_URL" | sed -E 's|postgresql://[^@]+@([^:/]+).*|\1|')
        PG_PORT=$(echo "$CLEAN_URL" | sed -E 's|.*:([0-9]+)/.*|\1|')
        PG_NAME=$(echo "$CLEAN_URL" | sed -E 's|.*/([^?]+).*|\1|')

        PG_PORT="${PG_PORT:-5432}"

        info "Backing up PostgreSQL database: $PG_NAME at $PG_HOST:$PG_PORT"

        PGPASSWORD="$PG_PASS" pg_dump \
            --host="$PG_HOST" \
            --port="$PG_PORT" \
            --username="$PG_USER" \
            --format=custom \
            --compress=9 \
            --file="$DUMP_FILE" \
            "$PG_NAME"
        ;;
esac

# ---------------------------------------------------------------------------
# Compress SQLite backup (PostgreSQL pg_dump already compresses)
# ---------------------------------------------------------------------------
if [[ "$DB_TYPE" == "sqlite" ]]; then
    info "Compressing backup..."
    gzip -9 "$DUMP_FILE"
    DUMP_FILE="$DUMP_FILE.gz"
fi

BACKUP_SIZE=$(du -sh "$DUMP_FILE" | cut -f1)
info "Backup written: $DUMP_FILE ($BACKUP_SIZE)"

# ---------------------------------------------------------------------------
# Rotate old backups
# ---------------------------------------------------------------------------
info "Removing backups older than $KEEP_DAYS days..."
REMOVED=$(find "$BACKUP_DIR" -name "myapp_backup_*" -mtime "+$KEEP_DAYS" -print -delete | wc -l)
info "Removed $REMOVED old backup(s)."

# ---------------------------------------------------------------------------
# Verify backup integrity
# ---------------------------------------------------------------------------
info "Verifying backup integrity..."
case "$DB_TYPE" in
    sqlite)
        # Verify the gzip is intact
        if gzip -t "$DUMP_FILE" 2>/dev/null; then
            info "Backup integrity check: PASSED"
        else
            error "Backup integrity check: FAILED — gzip test failed"
            exit 1
        fi
        ;;
    postgresql)
        # List contents of the custom-format dump
        if pg_restore --list "$DUMP_FILE" &>/dev/null; then
            info "Backup integrity check: PASSED"
        else
            error "Backup integrity check: FAILED — pg_restore --list failed"
            exit 1
        fi
        ;;
esac

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
REMAINING=$(find "$BACKUP_DIR" -name "myapp_backup_*" | wc -l)
info "Backup complete. $REMAINING backup(s) retained in $BACKUP_DIR."
