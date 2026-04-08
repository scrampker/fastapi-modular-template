#!/usr/bin/env bash
# =============================================================================
# install.sh — Automated installer for myapp
# =============================================================================
# Usage:
#   sudo bash install.sh [--domain app.example.com] [--certbot] [--postgres]
#
# What this script does:
#   1. Creates a dedicated system user (myapp)
#   2. Installs system dependencies
#   3. Copies the application to /opt/myapp
#   4. Creates a Python virtual environment and installs dependencies
#   5. Generates a secure JWT secret
#   6. Prompts for the initial admin password
#   7. Runs database migrations
#   8. Installs systemd service units
#   9. Configures Nginx
#  10. Optionally provisions a TLS certificate with Certbot
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
APP_USER="myapp"
APP_GROUP="myapp"
APP_DIR="/opt/myapp"
LOG_DIR="/var/log/myapp"
DOMAIN="app.example.com"
USE_CERTBOT=false
USE_POSTGRES=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)   DOMAIN="$2";     shift 2 ;;
        --certbot)  USE_CERTBOT=true; shift   ;;
        --postgres) USE_POSTGRES=true; shift  ;;
        *)          echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()    { echo -e "\033[0;32m[INFO]\033[0m  $*"; }
warn()    { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
error()   { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; }
require() { command -v "$1" &>/dev/null || { error "Required command not found: $1"; exit 1; }; }

check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)."
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# 1. Pre-flight checks
# ---------------------------------------------------------------------------
check_root

require python3
require pip3
require nginx
require systemctl

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if python3 -c 'import sys; exit(0 if sys.version_info >= (3,10) else 1)'; then
    info "Python $PYTHON_VERSION detected."
else
    error "Python 3.10+ is required. Found: $PYTHON_VERSION"
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. System dependencies
# ---------------------------------------------------------------------------
info "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    libssl-dev \
    libffi-dev \
    nginx \
    curl \
    git

if [[ "$USE_POSTGRES" == true ]]; then
    info "Installing PostgreSQL client libraries..."
    apt-get install -y -qq libpq-dev postgresql-client
fi

# ---------------------------------------------------------------------------
# 3. Create application user
# ---------------------------------------------------------------------------
if ! id "$APP_USER" &>/dev/null; then
    info "Creating system user: $APP_USER"
    useradd --system --shell /usr/sbin/nologin --home-dir "$APP_DIR" --create-home "$APP_USER"
else
    info "User $APP_USER already exists, skipping."
fi

# ---------------------------------------------------------------------------
# 4. Copy application files
# ---------------------------------------------------------------------------
info "Deploying application to $APP_DIR..."
mkdir -p "$APP_DIR"
rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.env' --exclude='*.db' --exclude='uploads/' \
    "$REPO_ROOT/" "$APP_DIR/"

# Create required directories
mkdir -p "$LOG_DIR" "$APP_DIR/uploads"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR" "$LOG_DIR"
chmod 750 "$APP_DIR" "$LOG_DIR"
chmod 770 "$APP_DIR/uploads"

# ---------------------------------------------------------------------------
# 5. Python virtual environment
# ---------------------------------------------------------------------------
info "Creating Python virtual environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip wheel

info "Installing Python dependencies..."
"$APP_DIR/venv/bin/pip" install --quiet -e "$APP_DIR[$(if $USE_POSTGRES; then echo 'postgres'; else echo ''; fi)]"

# Install Gunicorn (not in pyproject.toml since it's deployment-only)
"$APP_DIR/venv/bin/pip" install --quiet "gunicorn>=21.0"

chown -R "$APP_USER:$APP_GROUP" "$APP_DIR/venv"

# ---------------------------------------------------------------------------
# 6. Environment configuration
# ---------------------------------------------------------------------------
if [[ ! -f "$APP_DIR/.env" ]]; then
    info "Generating .env file..."
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"

    # Generate a cryptographically secure JWT secret
    JWT_SECRET=$(openssl rand -hex 32)
    sed -i "s|CHANGE_ME_generate_with_openssl_rand_hex_32|$JWT_SECRET|g" "$APP_DIR/.env"

    # Set production environment
    sed -i "s|^APP_ENV=.*|APP_ENV=production|" "$APP_DIR/.env"
    sed -i "s|^APP_DEBUG=.*|APP_DEBUG=false|" "$APP_DIR/.env"
    sed -i "s|^APP_BASE_URL=.*|APP_BASE_URL=https://$DOMAIN|" "$APP_DIR/.env"

    if [[ "$USE_POSTGRES" == true ]]; then
        info "Configure DATABASE_URL in $APP_DIR/.env for PostgreSQL."
        sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://myapp:CHANGE_PASSWORD@localhost:5432/myapp|" "$APP_DIR/.env"
    fi

    chmod 600 "$APP_DIR/.env"
    chown "$APP_USER:$APP_GROUP" "$APP_DIR/.env"
    info "Generated $APP_DIR/.env — review and adjust before starting the service."
else
    warn "$APP_DIR/.env already exists, skipping generation."
fi

# ---------------------------------------------------------------------------
# 7. Database migrations
# ---------------------------------------------------------------------------
info "Running database migrations..."
cd "$APP_DIR"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/alembic" upgrade head

# ---------------------------------------------------------------------------
# 8. Create initial admin user
# ---------------------------------------------------------------------------
info "Creating initial admin user..."
read -rp "Admin email [admin@example.com]: " ADMIN_EMAIL
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"

while true; do
    read -rsp "Admin password (min 12 chars): " ADMIN_PASSWORD
    echo
    read -rsp "Confirm password: " ADMIN_PASSWORD2
    echo
    if [[ "$ADMIN_PASSWORD" == "$ADMIN_PASSWORD2" && ${#ADMIN_PASSWORD} -ge 12 ]]; then
        break
    fi
    warn "Passwords do not match or too short. Try again."
done

sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" -c "
import asyncio
from app.core.database import get_db_context
from app.services.users.service import create_admin_user

async def main():
    async with get_db_context() as db:
        await create_admin_user(db, email='$ADMIN_EMAIL', password='$ADMIN_PASSWORD')
        print('Admin user created.')

asyncio.run(main())
" || warn "Could not create admin user automatically. Run manually after startup."

# ---------------------------------------------------------------------------
# 9. Systemd service units
# ---------------------------------------------------------------------------
info "Installing systemd service units..."
cp "$SCRIPT_DIR/app-web.service" /etc/systemd/system/myapp-web.service
cp "$SCRIPT_DIR/app-celery.service" /etc/systemd/system/myapp-celery.service

systemctl daemon-reload
systemctl enable myapp-web
systemctl enable myapp-celery

# ---------------------------------------------------------------------------
# 10. Nginx configuration
# ---------------------------------------------------------------------------
info "Configuring Nginx..."
NGINX_CONF="/etc/nginx/sites-available/myapp"
cp "$SCRIPT_DIR/nginx.conf" "$NGINX_CONF"
sed -i "s|app.example.com|$DOMAIN|g" "$NGINX_CONF"

# Update cert paths if domain changed
sed -i "s|/etc/letsencrypt/live/app.example.com|/etc/letsencrypt/live/$DOMAIN|g" "$NGINX_CONF"

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/myapp
rm -f /etc/nginx/sites-enabled/default

nginx -t && systemctl reload nginx

# ---------------------------------------------------------------------------
# 11. Optional: Certbot TLS
# ---------------------------------------------------------------------------
if [[ "$USE_CERTBOT" == true ]]; then
    info "Provisioning TLS certificate with Certbot..."
    if ! command -v certbot &>/dev/null; then
        apt-get install -y -qq certbot python3-certbot-nginx
    fi

    read -rp "Email for Let's Encrypt notifications: " CERTBOT_EMAIL
    certbot --nginx \
        --non-interactive \
        --agree-tos \
        --email "$CERTBOT_EMAIL" \
        --domains "$DOMAIN" \
        --redirect

    # Ensure auto-renewal is enabled
    systemctl enable certbot.timer 2>/dev/null || true
    info "TLS certificate provisioned. Auto-renewal is enabled."
else
    warn "Certbot skipped. Update the TLS certificate paths in $NGINX_CONF manually."
    warn "If testing without TLS, remove the ssl_* lines and the HTTPS redirect block."
fi

# ---------------------------------------------------------------------------
# 12. Logrotate
# ---------------------------------------------------------------------------
info "Installing logrotate configuration..."
cp "$SCRIPT_DIR/logrotate.conf" /etc/logrotate.d/myapp

# ---------------------------------------------------------------------------
# 13. Start services
# ---------------------------------------------------------------------------
info "Starting services..."
systemctl start myapp-web
systemctl start myapp-celery

sleep 2

if systemctl is-active --quiet myapp-web; then
    info "myapp-web is running."
else
    error "myapp-web failed to start. Check: journalctl -u myapp-web -n 50"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "=================================================================="
echo " Installation complete!"
echo ""
echo " Application: https://$DOMAIN"
echo " Config file: $APP_DIR/.env"
echo " Log dir:     $LOG_DIR"
echo ""
echo " Useful commands:"
echo "   systemctl status myapp-web"
echo "   journalctl -u myapp-web -f"
echo "   journalctl -u myapp-celery -f"
echo "=================================================================="
