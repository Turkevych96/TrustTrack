# TrustTrack Docker On Linux

This setup runs TrustTrack as one Docker service:

- Django site served by Gunicorn on port 8000
- Telegram polling bot
- due job scheduler
- SQLite backup scheduler

## Files On The Linux Host

Use one persistent host directory for TrustTrack data:

```text
/opt/trusttrack/data/
```

Inside that directory:

```text
/opt/trusttrack/data/db.sqlite3
/opt/trusttrack/data/backups/trusttrack-YYYYMMDD-HHMMSS.sqlite3
```

Docker mounts this directory into the container as:

```text
/data
```

The app reads SQLite from:

```text
/data/db.sqlite3
```

The backup scheduler writes backups to:

```text
/data/backups
```

## First Server Setup

Create the data directory:

```bash
sudo mkdir -p /opt/trusttrack/data
sudo chown -R "$USER:$USER" /opt/trusttrack
```

Create the environment file:

```bash
cp .env.example .env
```

Edit `.env` for the Linux server:

```text
DJANGO_SECRET_KEY=use-a-long-random-secret
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=192.168.1.50,trusttrack.home
DJANGO_CSRF_TRUSTED_ORIGINS=http://192.168.1.50:8000,http://trusttrack.home:8000
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
TRUSTTRACK_DATA_DIR=/opt/trusttrack/data
TRUSTTRACK_BIND_IP=192.168.1.50
TRUSTTRACK_PORT=8000
```

Build the image:

```bash
bash scripts/docker-build.sh
```

Start TrustTrack:

```bash
docker compose up -d
```

Open from a device inside your home network:

```text
http://your-lan-server-ip:8000/
```

The compose file is intentionally safe by default. If `TRUSTTRACK_BIND_IP` is not set, Docker binds the site to `127.0.0.1` only. To expose the site to your home LAN, bind it to the server LAN IP:

```text
192.168.1.50:8000 -> container:8000
```

Set the LAN address in `.env`:

```text
TRUSTTRACK_BIND_IP=192.168.1.50
TRUSTTRACK_PORT=8000
DJANGO_ALLOWED_HOSTS=192.168.1.50,trusttrack.home
DJANGO_CSRF_TRUSTED_ORIGINS=http://192.168.1.50:8000,http://trusttrack.home:8000
```

Do not use `0.0.0.0` if the server also has a public internet interface. Telegram access does not need an inbound public web port; the bot uses outbound polling.

If the server firewall is active, allow only your home subnet:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp
```

## Future Cloudflare Tunnel Mode

If you later publish TrustTrack through Cloudflare Tunnel, keep Docker bound to localhost:

```text
TRUSTTRACK_BIND_IP=127.0.0.1
TRUSTTRACK_PORT=8000
DJANGO_ALLOWED_HOSTS=trusttrack.your-domain.example
DJANGO_CSRF_TRUSTED_ORIGINS=https://trusttrack.your-domain.example
```

Then point Cloudflare Tunnel to:

```text
http://127.0.0.1:8000
```

This keeps the app closed to direct internet traffic. You do not need router port forwarding for the website, and Telegram still works through outbound bot polling.

## Existing SQLite Database

If you already have a local database, stop the container and copy it to the host data directory:

```bash
docker compose down
cp data/db.sqlite3 /opt/trusttrack/data/db.sqlite3
docker compose up -d
```

Do not put `db.sqlite3` inside the Docker image. Keep it in `/opt/trusttrack/data`.

## Backups

Backups are automatic. The container starts:

```text
backup_sqlite --interval 86400 --keep 30
```

That means one backup roughly every 24 hours, keeping the newest 30 files.

Run a manual backup:

```bash
docker compose exec trusttrack python manage.py backup_sqlite --once
```

List backups:

```bash
ls -lh /opt/trusttrack/data/backups
```

Restore a backup:

```bash
docker compose down
cp /opt/trusttrack/data/backups/trusttrack-YYYYMMDD-HHMMSS.sqlite3 /opt/trusttrack/data/db.sqlite3
docker compose up -d
```

## Useful Commands

Show logs:

```bash
docker compose logs -f trusttrack
```

Run migrations manually:

```bash
docker compose exec trusttrack python manage.py migrate
```

Create an admin user:

```bash
docker compose exec trusttrack python manage.py createsuperuser
```

Run due jobs manually:

```bash
docker compose exec trusttrack python manage.py run_due_jobs --once
```

Stop:

```bash
docker compose down
```

Rebuild after code changes:

```bash
bash scripts/docker-build.sh
docker compose up -d
```
