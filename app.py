"""
Davangere Park and Garden Management — shared backend.

This is the piece that was missing from the static HTML preview: a server with a real
SQLite database that every visitor's browser talks to, so a citizen's phone and the
municipality's laptop see the exact same data instead of two separate local copies.

The frontend (index.html) is unchanged in look and behaviour — it still has all the same
functions (loadComplaints/saveComplaints/loadTechnicians/etc.) and still keeps a local
localStorage copy for instant rendering. The only difference is that every save() now also
POSTs to one of the API routes below, and the page pulls fresh data from here whenever a
dashboard is opened, on a poll while it's open, and once on page load.

Data model: rather than a separate SQL table per field (which would require rewriting all
of the existing JavaScript that filters/sorts/searches these arrays), each collection is
stored as a single JSON blob in one row of the `store` table — e.g. the `complaints` row
holds the full JSON array of every complaint, exactly like the array that used to live only
in localStorage. This keeps the browser-side code almost identical while making SQLite the
single shared source of truth. It is a deliberate simplification, not an accident — see
README.md for the normalized-table upgrade path if a course project needs one.
"""

from flask import Flask, request, jsonify, send_from_directory
import sqlite3
import os
import json
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "data.db")
_lock = threading.Lock()

app = Flask(__name__, static_folder=None)

DEFAULTS = {
    "complaints": "[]",
    "technicians": "[]",
    "suggestions": "[]",
    "extra_parks": "[]",
    "admin_password": json.dumps("davangere2026"),
}

# URL path -> store key
COLLECTIONS = {
    "complaints": "complaints",
    "technicians": "technicians",
    "suggestions": "suggestions",
    "extra-parks": "extra_parks",
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock:
        conn = get_db()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS store (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO store (key, value) VALUES (?, ?)", (key, value)
            )
        conn.commit()
        conn.close()


def get_value(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM store WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else DEFAULTS.get(key, "null")


def set_value(key, value_json_text):
    with _lock:
        conn = get_db()
        conn.execute(
            """INSERT INTO store (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value_json_text),
        )
        conn.commit()
        conn.close()


# Make sure the table + default rows exist as soon as the module is imported, so this
# works both under `python app.py` and under a production server like gunicorn, which
# imports the module instead of running the __main__ block.
init_db()


@app.route("/api/admin-password", methods=["GET"])
def get_admin_password():
    return jsonify({"value": json.loads(get_value("admin_password"))})


@app.route("/api/admin-password", methods=["POST"])
def post_admin_password():
    body = request.get_json(force=True, silent=True) or {}
    pw = body.get("value", "davangere2026")
    set_value("admin_password", json.dumps(pw))
    return jsonify({"ok": True})


@app.route("/api/<name>", methods=["GET"])
def get_collection(name):
    key = COLLECTIONS.get(name)
    if not key:
        return jsonify({"error": "unknown collection"}), 404
    return jsonify(json.loads(get_value(key)))


@app.route("/api/<name>", methods=["POST"])
def post_collection(name):
    key = COLLECTIONS.get(name)
    if not key:
        return jsonify({"error": "unknown collection"}), 404
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "expected a JSON array"}), 400
    set_value(key, json.dumps(body))
    return jsonify({"ok": True})


# ---------- serve the frontend ----------
# index.html (and any file next to it, e.g. a favicon) is served directly — no separate
# "static/" folder needed since the whole app is the one HTML file.
@app.route("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.route("/<path:filename>")
def other_files(filename):
    # Never let this fall through to serving app.py, data.db, etc.
    if filename in ("app.py", "data.db", "requirements.txt") or filename.startswith("."):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(APP_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
