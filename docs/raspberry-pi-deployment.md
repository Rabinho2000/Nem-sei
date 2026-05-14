# Raspberry Pi Deployment Guide

This guide describes a simple production deployment for the Monitoring Board on
a Raspberry Pi 5 in the office, with remote access through Cloudflare Tunnel and
Cloudflare Access.

## 1. Required Hardware

- Raspberry Pi 5.
- Official or high-quality USB-C power supply.
- Raspberry Pi OS-compatible 64-bit microSD card or SSD.
- Reliable network connection, preferably Ethernet.
- Enough storage for SQLite data, uploads, backups and logs.
- Optional but recommended: UPS or stable power source.

## 2. Raspberry Pi OS 64-bit Setup

Install Raspberry Pi OS 64-bit using Raspberry Pi Imager.

Recommended options in Raspberry Pi Imager:

- Enable SSH.
- Set hostname, for example `monitoring-board`.
- Set a non-default username and password.
- Configure Wi-Fi only if Ethernet is not available.
- Use the 64-bit Raspberry Pi OS image.

After first boot:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

After reboot, check architecture:

```bash
uname -m
```

Expected output should be `aarch64`.

Install small host tools used by backup/restore checks:

```bash
sudo apt install -y sqlite3 tar
```

## 3. Installing Docker

Install Docker:

```bash
curl -fsSL https://get.docker.com | sh
```

Allow your user to run Docker:

```bash
sudo usermod -aG docker "$USER"
```

Log out and back in, then verify:

```bash
docker --version
docker compose version
```

## 4. Cloning the Repository

Choose a stable folder, for example:

```bash
cd /home/pi
git clone <REPOSITORY_URL> Nem-sei
cd Nem-sei
```

Replace `<REPOSITORY_URL>` with the actual Git repository URL.

## 5. Creating `.env`

Create the production environment file:

```bash
cp .env.example .env
nano .env
```

Required values:

```env
FLASK_SECRET_KEY=<long-random-secret>
APP_USERNAME=admin
APP_PASSWORD=<strong-password>
DATA_DIR=/data
SESSION_COOKIE_SECURE=true
```

Recommended:

```env
APP_PASSWORD_HASH=<password-hash>
MAX_UPLOAD_MB=10
```

FusionSolar and Telegram credentials should also be set in `.env` or as
environment variables. Do not commit `.env`.

## 6. Starting with Docker Compose

Build the image:

```bash
docker compose build
```

Start the app:

```bash
docker compose up -d
```

Check containers:

```bash
docker compose ps
```

The compose file binds the app to localhost only:

```text
127.0.0.1:5000:5000
```

This is intentional for Cloudflare Tunnel. The app should not be exposed
directly to the internet.

The container command is intentionally one Gunicorn worker with threads:

```text
gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 app:app
```

Keep exactly one worker while APScheduler runs inside the Flask/Gunicorn
process. Threads are acceptable for this deployment because they stay inside
that single worker process. Do not increase `-w`, set worker-count shortcuts
such as `WEB_CONCURRENCY`, run `docker compose up --scale monitoring-board=2`,
or start a second app instance against the same `./data` unless scheduling is
redesigned.

## 7. Checking Logs

Docker logs:

```bash
docker compose logs -f
```

Application logs in the data directory:

```bash
tail -f ./data/logs/monitoring_board.log
```

## 8. Updating After `git pull`

From the repository folder:

```bash
git pull
docker compose build
docker compose up -d
docker compose logs -f
```

If dependencies changed, rebuilding is required.

## 9. Backup and Restore

Runtime data is stored in `./data` on the Raspberry Pi and mounted into the
container as `/data`. Docker Compose sets `DATA_DIR=/data` inside the
container; host-side backup and restore commands use `DATA_DIR=./data`.

### Manual Backup

```bash
chmod +x scripts/backup.sh
DATA_DIR=./data ./scripts/backup.sh
```

Backup files are stored in:

```bash
./data/backups
```

The script backs up:

- `monitoring_board.db`
- committed database content that may currently live in SQLite sidecars such as
  `monitoring_board.db-wal` and `monitoring_board.db-shm`
- `uploads/` as a `.tar.gz` archive when `INCLUDE_UPLOADS=1`

The database backup uses `sqlite3 ".backup"` so the backup file is consistent
even when WAL mode is active.

### Daily Cron Backup

Edit cron:

```bash
crontab -e
```

Example daily backup at 03:15:

```cron
15 3 * * * cd /home/pi/Nem-sei && DATA_DIR=./data ./scripts/backup.sh >> ./data/logs/backup.log 2>&1
```

### Check Backup Files

```bash
ls -lh ./data/backups
sqlite3 ./data/backups/monitoring_board_YYYYMMDD_HHMMSS.db "PRAGMA integrity_check;"
tar -tzf ./data/backups/uploads_YYYYMMDD_HHMMSS.tar.gz | head
```

### Restore Procedure

Stop the app:

```bash
docker compose down
```

Keep a copy of the current database:

```bash
cp ./data/monitoring_board.db ./data/monitoring_board.db.before_restore
```

Restore the database:

```bash
cp ./data/backups/monitoring_board_YYYYMMDD_HHMMSS.db ./data/monitoring_board.db
```

Restore uploads if needed:

```bash
rm -rf ./data/uploads
tar -C ./data -xzf ./data/backups/uploads_YYYYMMDD_HHMMSS.tar.gz
```

Start again:

```bash
docker compose up -d
docker compose logs -f
```

## 10. Cloudflare Tunnel Setup Overview

Install `cloudflared` on the Raspberry Pi using Cloudflare's current package
instructions for Debian/Raspberry Pi OS.

High-level flow:

1. Create a tunnel in the Cloudflare Zero Trust dashboard.
2. Install and authenticate `cloudflared` on the Raspberry Pi.
3. Route the tunnel hostname, for example `monitoring.example.com`.
4. Set the tunnel service/origin to:

```text
http://127.0.0.1:5000
```

5. Run `cloudflared` as a service.

Check tunnel status:

```bash
sudo systemctl status cloudflared
journalctl -u cloudflared -f
```

## 11. Recommended Cloudflare Access Setup

Use Cloudflare Access in front of the tunnel hostname.

Recommended policy:

- Allow only named internal users or an approved email domain.
- Require MFA if available.
- Keep session duration reasonable.
- Do not make the application public.
- Keep the app's own password login enabled as a second layer.

The app does not include OAuth and does not need internal user roles at this
phase.

## 12. Troubleshooting

### App Does Not Start

Check logs:

```bash
docker compose logs --tail=200
```

Common causes:

- Missing or invalid `FLASK_SECRET_KEY`.
- Missing `APP_PASSWORD` or `APP_PASSWORD_HASH`.
- Syntax error in `.env`.
- Port `5000` already in use.
- Build failed after dependency changes.

Rebuild:

```bash
docker compose build
docker compose up -d
```

### Database Locked

SQLite is acceptable for 2-3 internal users, but long writes can still block.

Check:

```bash
docker compose logs --tail=200
ls -lh ./data
```

Actions:

- Confirm Docker Compose is running exactly one Gunicorn worker.
- `--threads 4` is acceptable because it remains one worker process.
- Do not use `docker compose up --scale monitoring-board=2`.
- Do not start a second copy of the app against the same `./data`.
- Let background jobs finish before retrying large sync/backfill operations.
- Reboot the container if a stuck process is suspected:

```bash
docker compose restart
```

### FusionSolar Sync Fails

Check:

```bash
docker compose logs -f
```

Common causes:

- Wrong `FUSIONSOLAR_BASE_URL`.
- Expired or changed FusionSolar credentials.
- API endpoint path mismatch.
- FusionSolar rate limits.
- Missing station mappings.

Update `.env`, then restart:

```bash
docker compose up -d
```

### No Remote Access

Check local app first:

```bash
curl -I http://127.0.0.1:5000
```

Check container:

```bash
docker compose ps
```

Check Cloudflare tunnel:

```bash
sudo systemctl status cloudflared
journalctl -u cloudflared --tail=100
```

Common causes:

- Tunnel origin not set to `http://127.0.0.1:5000`.
- Cloudflare Access policy blocks the user.
- DNS route not attached to the tunnel.
- `cloudflared` service stopped.

### Disk Full

Check disk usage:

```bash
df -h
du -h -d 2 ./data | sort -h
docker system df
```

Actions:

- Remove old backups after confirming newer backups exist.
- Review `./data/logs`.
- Prune unused Docker images:

```bash
docker image prune
```

Do not delete `./data/monitoring_board.db` or `./data/uploads` unless restoring
from backup.

## 13. What Not To Do

- Do not expose port `5000` directly to the internet.
- Do not publish Docker as `0.0.0.0:5000:5000` for remote access.
- Do not run the Flask development server in production.
- Do not use `python app.py --debug` in production.
- Do not store production data only inside the container filesystem.
- Do not run multiple Gunicorn workers while APScheduler is in-process.
- Do not set worker-count shortcuts such as `WEB_CONCURRENCY`.
- Do not run `docker compose up --scale monitoring-board=2`.
- Do not start a second app instance against the same `./data` while
  scheduling is in-process.
- Do not add Postgres, Redis, Celery, RQ or cloud backup integration for this
  phase.
