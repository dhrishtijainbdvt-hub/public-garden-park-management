# Davangere Park and Garden Management

A Flask and SQLite demo for exploring parks, reporting issues, and managing maintenance.
It is **not safe for a public production launch** in its current form: the API has no
authentication, so anyone who can reach a deployed server can read and change its data,
including submitted contact details and photos. The admin password is also exposed through
the API. Do not use real personal data or deploy this demo publicly until server-side
authentication and authorization, and appropriate data protection, have been implemented.

## What's in this folder

- `app.py` — the Flask server and its API routes.
- `index.html` — the same site you already reviewed (same look, same features, same
  bilingual support), with one change: every place it used to read/write only to the
  browser's local storage now also syncs with the server below.
- `requirements.txt` — the two Python packages needed to run it.
- `data.db` — the local SQLite database, created automatically when the server starts.
  It is intentionally excluded from Git so local records are not published.

## How the sync works (plain-English version)

- Every "save" action (submitting a complaint, adding a technician, assigning someone,
  marking something resolved, etc.) still updates the browser's local copy instantly
  *and* sends the update to the server in the background.
- Opening the Municipality or Technician dashboard pulls the latest data from the server
  right away, and then every 5 seconds while that dashboard stays open — so a complaint a
  citizen just submitted on their own phone shows up on the municipality's screen on its
  own, without anyone needing to refresh.
- If the server can't be reached for a moment, the page keeps working off its last-known
  local copy and a small indicator in the header shows "🔴 Offline" until it reconnects.

## Running it locally first (to prove it works before you deploy)

```bash
cd park_tracker_server
pip install -r requirements.txt
python3 app.py
```

Then open `http://127.0.0.1:5000/` in a browser. Try it in two different browsers (or one
normal window + one incognito window) at the same time — submit a complaint as a citizen in
one, and watch it appear in the Municipality dashboard in the other.

## Deploying it for real (step by step)

See the deployment guide sent alongside this project. In short: push this folder to a free
GitHub repo, create a free Web Service on Render (or Railway/PythonAnywhere) pointed at it,
set the start command to `gunicorn app:app`, and you'll get a real public URL anyone can
visit from any device.

## Honest limitations of this version (so nothing surprises you later)

- **Data model is simple by design.** Instead of a separate SQL table per field
  (citizens, complaints, technicians as normalized tables), each collection (complaints,
  technicians, suggestions, extra parks) is stored as one JSON blob per row in a single
  `store` table. This keeps the browser-side code nearly identical to the version you
  already tested, and it is a real, working, shared SQLite database — but if your course
  specifically wants to see normalized relational tables with foreign keys, that's an
  upgrade from here, not what's currently in `app.py`.
- **Login is simple, not bank-grade.** The admin password and technician credentials are
  checked in the browser against data fetched from the server, the same way the original
  preview worked — there's no server-side session/token security. Fine for a course
  project demo; not something to reuse for a system handling sensitive data.
- **Photos are stored as base64 text inside the database**, not as separate image files.
  Simple and fully working at this scale; a larger real deployment would normally store
  images as files (or in cloud storage) instead.
- **Free hosting tiers sleep when idle.** On a free Render/Railway plan, the server may
  take ~30-50 seconds to wake up on the very first visit after a period of inactivity.
  That's a hosting-plan characteristic, not a bug in the app.
