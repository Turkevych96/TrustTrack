# TrustTrack

TrustTrack is a small local Django application for tracking family debts between trusted users.

The project is intentionally simple: classic Django, SQLite, and server-rendered pages. It is designed for a private/local environment, not for production banking use.

## Core Features
- **Obligations Tracking:** Record who owes whom and for what (e.g., Rent, Hardware).
- **Monthly Charges:** Automatically add fixed amounts (like $625 rent) on the 1st of every month.
- **Accurate Interest:** Calculates daily interest based on the current balance and capitalizes it monthly.

## Tech Stack

- Python >= 3.14
- Django 6
- SQLite via `db.sqlite3`
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

## Database Setup

The project uses a local SQLite database file named `db.sqlite3`.

This file is not meant to be committed to the repository. Django will create it locally when migrations are applied.

Run migrations:

```bash
uv run python manage.py migrate
```

This creates or updates `db.sqlite3` with the required Django tables, including authentication and admin tables.

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

## Local Development Notes

- Keep `db.sqlite3` local.
- Do not commit `.venv/`, `db.sqlite3`, logs, caches, or IDE files.
- Use Django migrations whenever models change.
- This project should stay simple and easy to run locally.

## Backup and Restore
Because `db.sqlite3` is ignored by Git to prevent merge conflicts, you should manually backup your financial data.

**Create a backup:**
```bash
uv run python manage.py dumpdata > backup_data.json
```
Keep ```backup_data.json``` in a safe place (this file CAN be committed to Git or saved on a flash drive).

Restore from a backup (on a fresh installation):
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
