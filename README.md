# TrustTrack

TrustTrack is a small local Django application for tracking family debts between trusted users.

The project is intentionally simple: classic Django, SQLite, and server-rendered pages. It is designed for a private/local environment, not for production banking use.

## Core Features
- **Obligations Tracking:** Record who owes whom and for what (e.g., Rent, Hardware).
- **Scheduled Charges:** Track one-time and recurring charges with future amount changes (for example rent moving from $1000 to $1100).
- **Rate History & Interest:** Track changing interest rates, calculate daily interest from dated balances, and post it monthly.

## Planning

- [TrustTrack Logic Map](docs/trusttrack-logic.md) defines the domain scheme, backend dependency map, calculation boundaries, and initial GitHub Projects backlog.

## Tech Stack

- Python >= 3.14
- Django 6
- SQLite via `data/db.sqlite3`
- Dependency management with `uv`
- Classic Django views, forms, templates, and Django admin

## Requirements

Before running the project, install:

- Python 3.14 or newer
- `uv` [doc*](https://docs.astral.sh/uv/) (A fast Python package installer and resolver)

## Setup

Clone the repository and open the project directory:

```bash
cd TrustTrack
```

Install dependencies:

```bash
uv sync
```

Create a local environment file:

```bash
copy .env.example .env
```

Keep `.env` local. It is ignored by Git and is the right place for values like `DJANGO_SECRET_KEY` and `TELEGRAM_BOT_TOKEN`.

## Database Setup

The project uses a local SQLite database file at `data/db.sqlite3`.

This file is not meant to be committed to the repository. Django will create it locally when migrations are applied.

Run migrations:

```bash
uv run python manage.py migrate
```

This creates or updates `data/db.sqlite3` with the required Django tables, including authentication and admin tables.

## Create an Admin User

Create your first local admin user:

```bash
uv run python manage.py createsuperuser
```

Follow the prompts and choose a username, email, and password.

This account is used only for your local Django app.

## Run The App

Start the development server:

```bash
uv run python manage.py runserver
```

Open the app in your browser:

```text
http://127.0.0.1:8000/
```

Open the Django admin:

```text
http://127.0.0.1:8000/admin/
```

Use the admin username and password created with `createsuperuser`.

## Run The Local Site, Telegram Bot, And Scheduler

For the local family setup, TrustTrack can run the Django site, Telegram polling bot, and due job scheduler together without exposing the site to the internet.

Make sure `.env` contains `TELEGRAM_BOT_TOKEN`, and each allowed user has their Telegram ID saved in Profile.
Set `TELEGRAM_BOT_USERNAME` without `@` to enable QR/deep-link Telegram login from the web login page.

Start all local processes with one foreground command:

```bash
uv run python manage.py run_trusttrack
```

On Windows, start the local stack in the background with logs:

```powershell
.\scripts\start-trusttrack.ps1
```

Check or stop it:

```powershell
.\scripts\status-trusttrack.ps1 -Tail 20
.\scripts\stop-trusttrack.ps1
```

The site stays local at `http://127.0.0.1:8000/`. The bot uses Telegram polling and only responds to Telegram IDs saved in TrustTrack profiles.

The due job scheduler runs automatically while the local stack is alive. It generates due recurring events and posts due monthly interest without rebuilding the app. If the computer was off, the next scheduler run catches up through the current date without duplicating already generated items.

The SQLite backup scheduler also starts with the local stack. It creates dated backups in `data/backups/` and keeps the newest 30 by default. Backups are local-only and ignored by Git.

## Run With Docker On Linux

TrustTrack includes a Docker setup for a Linux server with the web app exposed only on your home LAN. Telegram access still works globally through the bot polling process. The SQLite database and backups live outside the image in a mounted host directory.

Read the server guide:

```text
docs/docker-linux.md
```

Default host data layout:

```text
/opt/trusttrack/data/db.sqlite3
/opt/trusttrack/data/backups/
```

Run due jobs manually once:

```bash
uv run python manage.py run_due_jobs --once
```

Run through a specific date:

```bash
uv run python manage.py run_due_jobs --once --date 2026-06-18
```

Create a SQLite backup manually once:

```bash
uv run python manage.py backup_sqlite --once
```

Use a custom backup directory or retention count:

```bash
uv run python manage.py backup_sqlite --once --output-dir D:\TrustTrackBackups --keep 60
```

## Local Development Notes

- Keep `data/db.sqlite3` local.
- Do not commit `.venv/`, `data/`, logs, caches, or IDE files.
- Use Django migrations whenever models change.
- This project should stay simple and easy to run locally.

## Backup and Restore
Because `data/db.sqlite3` is ignored by Git to prevent merge conflicts, keep backup copies of your financial data.

The recommended local backup command creates a consistent SQLite snapshot with a timestamped filename:

```bash
uv run python manage.py backup_sqlite --once
```

The older JSON export is still useful when you want a portable text fixture:

```bash
uv run python manage.py dumpdata > backup_data.json
```
Keep backups in a safe place, preferably outside the project folder or copied to another drive/cloud folder.

Restore from a SQLite backup:

```powershell
.\scripts\stop-trusttrack.ps1
copy .\data\backups\trusttrack-YYYYMMDD-HHMMSS.sqlite3 .\data\db.sqlite3
.\scripts\start-trusttrack.ps1
```

Restore from a JSON backup (on a fresh installation):
```bash
uv run python manage.py migrate
uv run python manage.py loaddata backup_data.json
```

## Useful Commands

Run database migrations:

```bash
uv run python manage.py migrate
```

Create an admin user:

```bash
uv run python manage.py createsuperuser
```

Start the local server:

```bash
uv run python manage.py runserver
```

Run Django checks:

```bash
uv run python manage.py check
```
