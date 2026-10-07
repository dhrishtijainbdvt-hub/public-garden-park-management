# Davangere Park and Garden Management

A bilingual (English/Kannada) Flask website for exploring Davangere parks, reporting
maintenance issues, suggesting improvements, and following repair progress. The site
uses a shared SQLite database; it is a Flask application and cannot be hosted as a
static GitHub Pages site.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ADMIN_PASSWORD = "replace-with-a-private-password-of-at-least-12-characters"
$env:SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
python app.py
```

Open <http://127.0.0.1:5000>. The first run creates `data.db` and stores a hash of
`ADMIN_PASSWORD`; the password itself is never returned by an API. Change it later
from the municipality dashboard. Add technician accounts there too; technician
passwords must be at least 12 characters.

If an existing database already has an administrator password hash, changing
`ADMIN_PASSWORD` does not overwrite it. Use the dashboard to change that password.

## Deploying the Flask app

Use a Python web host that supports persistent disks. Set the start command to:

```sh
gunicorn app:app
```

Configure these environment variables in the hosting provider's secret settings,
not in Git:

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | A long, random, stable Flask session-signing key. Keep it unchanged between restarts. |
| `ADMIN_PASSWORD` | Initial municipality password, at least 12 characters. Used only to initialize a new database. |
| `DATABASE_PATH` | Path to `data.db` on the provider's persistent disk, for example `/var/data/data.db`. |
| `COOKIE_SECURE` | Set to `true` when the site is served over HTTPS. |
| `APP_ENV` | Set to `production`; the app then requires `SECRET_KEY` and enables secure cookies by default. |

Enable HTTPS before inviting users. The host must retain the database disk across
deploys/restarts, and you should schedule and test database backups. Do not put
`data.db`, passwords, session keys, or real citizen records in the public repository.
For a one-off SQLite backup, Python's online backup API can copy a live database:

```python
import sqlite3

with sqlite3.connect("data.db") as source, sqlite3.connect("data-backup.db") as backup:
    source.backup(backup)
```

## Privacy and safeguards

- Municipality and technician dashboards require server-side sessions. Technician
  passwords are hashed; dashboard write requests require a CSRF token.
- Citizens can submit reports and suggestions without an account. Report tracking
  requires both the report number and the submitting phone number.
- Names and phone numbers are not stored in browser local storage and are omitted
  from technician responses and the public repair gallery. Only authorized staff can
  retrieve the full complaint list.
- Public report and suggestion endpoints validate input and apply per-IP rate limits.
  Report photos are resized to JPEG in the browser and size-limited on the server.
- Report photos stay private by default. The submitter must opt in before the before
  and after repair photos can appear in the public gallery.
- The public gallery shows photos only for resolved repairs whose submitter opted in.
  Follow the municipality's data-retention and
  privacy requirements before using this with real citizen information.

These safeguards are an improvement, not a substitute for a production security and
privacy review. In particular, review access policies, backups, data retention,
account recovery, hosting proxy/rate-limit behavior, and applicable local privacy law.
Only publish verified park details; confirm facilities, hours, and accessibility with
the relevant authority before adding them to park listings. Have Kannada copy reviewed
by a fluent speaker before a public launch.

## Tests

```sh
python -m unittest discover -s tests -v
```
