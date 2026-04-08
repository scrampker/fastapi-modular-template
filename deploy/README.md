# Deployment Guide

This guide covers deploying myapp to a production Ubuntu/Debian server using
Gunicorn + Uvicorn workers, Nginx as a reverse proxy, and systemd for process
management.

---

## Requirements

| Component | Minimum version |
|-----------|----------------|
| Ubuntu / Debian | 20.04 LTS / Bullseye |
| Python | 3.10+ |
| Nginx | 1.18+ |
| PostgreSQL (optional) | 14+ |
| Redis (optional, for Celery) | 6.0+ |

---

## Quick Start (automated installer)

```bash
# Clone the repository
git clone https://github.com/yourorg/myapp.git /tmp/myapp
cd /tmp/myapp

# Run the installer (as root)
sudo bash deploy/install.sh \
    --domain app.example.com \
    --certbot \
    --postgres        # optional: omit for SQLite
```

The installer:
1. Creates a `myapp` system user
2. Installs system dependencies
3. Creates `/opt/myapp/` with a Python venv
4. Generates a secure `SECRET_KEY`
5. Prompts for the initial admin password
6. Runs Alembic migrations
7. Installs and enables systemd service units
8. Configures Nginx
9. (With `--certbot`) provisions a Let's Encrypt TLS certificate

---

## Manual Deployment

### 1. Create the application user

```bash
sudo useradd --system --shell /usr/sbin/nologin \
    --home-dir /opt/myapp --create-home myapp
```

### 2. Copy application files

```bash
sudo rsync -a --exclude='.git' --exclude='*.pyc' \
    /tmp/myapp/ /opt/myapp/
sudo chown -R myapp:myapp /opt/myapp
```

### 3. Create virtual environment

```bash
sudo python3 -m venv /opt/myapp/venv
sudo /opt/myapp/venv/bin/pip install -e /opt/myapp
sudo /opt/myapp/venv/bin/pip install gunicorn
```

### 4. Configure environment

```bash
sudo cp /opt/myapp/deploy/.env.production /opt/myapp/.env
sudo nano /opt/myapp/.env          # Fill in REQUIRED values
sudo chmod 600 /opt/myapp/.env
sudo chown myapp:myapp /opt/myapp/.env
```

### 5. PostgreSQL setup (if using PostgreSQL)

```bash
sudo -u postgres psql <<EOF
CREATE USER myapp WITH PASSWORD 'your_password';
CREATE DATABASE myapp OWNER myapp;
GRANT ALL PRIVILEGES ON DATABASE myapp TO myapp;
EOF
```

Update `DATABASE_URL` in `/opt/myapp/.env`:
```
DATABASE_URL=postgresql+asyncpg://myapp:your_password@localhost:5432/myapp
```

### 6. Run migrations

```bash
cd /opt/myapp
sudo -u myapp /opt/myapp/venv/bin/alembic upgrade head
```

### 7. Install systemd units

```bash
sudo cp /opt/myapp/deploy/app-web.service /etc/systemd/system/myapp-web.service
sudo cp /opt/myapp/deploy/app-celery.service /etc/systemd/system/myapp-celery.service
sudo systemctl daemon-reload
sudo systemctl enable --now myapp-web
sudo systemctl enable --now myapp-celery   # only if using Celery
```

### 8. Configure Nginx

```bash
sudo cp /opt/myapp/deploy/nginx.conf /etc/nginx/sites-available/myapp
# Edit the domain name
sudo sed -i 's/app.example.com/yourdomain.com/g' /etc/nginx/sites-available/myapp
sudo ln -sf /etc/nginx/sites-available/myapp /etc/nginx/sites-enabled/myapp
sudo nginx -t && sudo systemctl reload nginx
```

### 9. DNS

Point your domain's A record to the server's public IP before running Certbot.

### 10. TLS certificate

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx --non-interactive --agree-tos \
    --email admin@example.com \
    --domains yourdomain.com \
    --redirect
```

---

## First Run Checklist

- [ ] `SECRET_KEY` is set to a 64-char random hex string
- [ ] `DATABASE_URL` points to a real database
- [ ] `APP_BASE_URL` matches your actual domain (with `https://`)
- [ ] SMTP credentials set (if email features are enabled)
- [ ] Initial admin user created
- [ ] `myapp-web` service is active: `systemctl status myapp-web`
- [ ] Nginx is serving HTTPS: `curl -I https://yourdomain.com/health`
- [ ] Backups are scheduled (see Backups section)

---

## Backups

### Schedule daily backups

```bash
# Edit root crontab
sudo crontab -e

# Add:
0 2 * * * /opt/myapp/deploy/backup.sh \
    --keep-days 30 \
    --output-dir /var/backups/myapp \
    >> /var/log/myapp/backup.log 2>&1
```

### Manual backup

```bash
sudo bash /opt/myapp/deploy/backup.sh
```

Backups are stored in `/var/backups/myapp/`. PostgreSQL backups use `pg_dump`
custom format; SQLite backups use SQLite's online backup API.

---

## Upgrades

```bash
# 1. Pull latest code
cd /opt/myapp && sudo git pull

# 2. Update dependencies
sudo -u myapp /opt/myapp/venv/bin/pip install -e /opt/myapp

# 3. Run migrations
sudo -u myapp /opt/myapp/venv/bin/alembic upgrade head

# 4. Reload the web service (zero-downtime with Gunicorn)
sudo systemctl reload myapp-web

# Or full restart if needed:
sudo systemctl restart myapp-web myapp-celery
```

---

## Monitoring

### Service status

```bash
systemctl status myapp-web
systemctl status myapp-celery
```

### Live logs

```bash
# Application logs
journalctl -u myapp-web -f
journalctl -u myapp-celery -f

# File-based logs
tail -f /var/log/myapp/access.log
tail -f /var/log/myapp/error.log
tail -f /var/log/myapp/celery.log

# Nginx logs
tail -f /var/log/nginx/myapp_access.log
tail -f /var/log/nginx/myapp_error.log
```

### Health check endpoint

```
GET https://yourdomain.com/health
```

Returns `{"status": "ok"}` when the application is running correctly.

---

## Scaling

To handle more traffic, increase `--workers` in `app-web.service`:

```
# Rule of thumb: (2 x CPU cores) + 1
--workers 9    # for a 4-core server
```

Then reload:

```bash
sudo systemctl daemon-reload
sudo systemctl restart myapp-web
```

For horizontal scaling, place multiple instances behind a load balancer and
switch to PostgreSQL. Celery workers scale independently — deploy additional
`myapp-celery` units or use Celery's autoscaler.
