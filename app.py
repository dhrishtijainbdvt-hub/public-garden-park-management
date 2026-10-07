"""Davangere Park and Garden Management Flask application."""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(APP_DIR, "data.db"))
_lock = threading.RLock()
configured_secret_key = os.environ.get("SECRET_KEY")
if os.environ.get("APP_ENV", "").lower() == "production" and not configured_secret_key:
    raise RuntimeError("Set a persistent SECRET_KEY before starting in production.")

app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=configured_secret_key or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=3 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "COOKIE_SECURE", "true" if os.environ.get("APP_ENV") == "production" else "false"
    ).lower() == "true",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)

DEFAULTS = {
    "complaints": "[]",
    "technicians": "[]",
    "suggestions": "[]",
    "extra_parks": "[]",
}

ISSUE_TYPES = {
    "issue_bench", "issue_lighting", "issue_landscaping", "issue_sanitation",
    "issue_equipment", "issue_water", "issue_other",
}
PRIORITIES = {"low", "medium", "high", "critical"}
STATUSES = {"pending", "assigned", "progress", "resolved", "verified"}
BUILTIN_PARKS = {
    1: "Visvesvaraya Park",
    2: "Water Tank Park",
    3: "Vidyanagar Park",
    4: "Dangey Park (Bull Park)",
    5: "Public Park / Gasagase Park (Kundavada Tank Road)",
    6: "Amritha Nair's Children's Park",
    7: "Netravathi Park",
    8: "S. Nijalingappa Park",
    9: "Kunduvada Lake",
    10: "Glass House Garden",
    11: "Mathru Chaya Children's Park",
}
SUGGESTION_CATEGORIES = {
    "sug_seating", "sug_play", "sug_greenery", "sug_lighting", "sug_facilities", "sug_other",
}
SUGGESTION_STATUSES = {"new", "review", "planned", "done"}
PHOTO_RE = re.compile(r"^data:image/jpeg;base64,([A-Za-z0-9+/]+={0,2})$")


class InvalidTechnicianUpdate(ValueError):
    pass


def get_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def escape_stored_fields(records, fields):
    if not isinstance(records, list):
        return
    for record in records:
        if not isinstance(record, dict):
            continue
        for field in fields:
            value = record.get(field)
            if isinstance(value, str):
                record[field] = html.escape(html.unescape(value), quote=True)


def init_db():
    with _lock:
        conn = get_db()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS store (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS rate_limits "
                "(key TEXT PRIMARY KEY, window_start INTEGER NOT NULL, count INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS counters "
                "(key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            for key, value in DEFAULTS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO store (key, value) VALUES (?, ?)", (key, value)
                )

            tech_row = conn.execute(
                "SELECT value FROM store WHERE key = 'technicians'"
            ).fetchone()
            technicians = json.loads(tech_row["value"]) if tech_row else []
            migrated = False
            for tech in technicians:
                if tech.get("password") and not tech.get("password_hash"):
                    tech["password_hash"] = generate_password_hash(tech.pop("password"))
                    migrated = True
            if migrated:
                conn.execute(
                    "UPDATE store SET value = ? WHERE key = 'technicians'",
                    (json.dumps(technicians),),
                )

            if not conn.execute(
                "SELECT 1 FROM store WHERE key = 'content_sanitized_v1'"
            ).fetchone():
                complaints_row = conn.execute(
                    "SELECT value FROM store WHERE key = 'complaints'"
                ).fetchone()
                complaints = json.loads(complaints_row["value"]) if complaints_row else []
                complaints = [item for item in complaints if isinstance(item, dict)]
                escape_stored_fields(
                    complaints,
                    ("citizen_name", "citizen_phone", "park_en", "park_kn", "description",
                     "repair_notes", "feedback_comment", "date_submitted", "date_resolved"),
                )
                used_complaint_ids = set()
                for complaint in complaints:
                    if type(complaint.get("id")) is not int or complaint["id"] < 1 \
                            or complaint["id"] in used_complaint_ids:
                        complaint["id"] = max(used_complaint_ids, default=0) + 1
                    used_complaint_ids.add(complaint["id"])
                    if complaint.get("status") not in STATUSES:
                        complaint["status"] = "pending"
                    if complaint.get("priority") not in PRIORITIES:
                        complaint["priority"] = "low"
                    if complaint.get("issue_type") not in ISSUE_TYPES:
                        complaint["issue_type"] = "issue_other"
                    if type(complaint.get("technician_id")) is not int:
                        complaint["technician_id"] = None
                    for photo_field in ("photo_before", "photo_after"):
                        if not is_valid_photo(complaint.get(photo_field)):
                            complaint[photo_field] = None
                    complaint["photo_consent"] = complaint.get("photo_consent") is True
                    if type(complaint.get("reopen_count")) is not int or complaint["reopen_count"] < 0:
                        complaint["reopen_count"] = 0
                    if not isinstance(complaint.get("history"), list):
                        complaint["history"] = []
                    complaint["history"] = [
                        item for item in complaint["history"] if isinstance(item, dict)
                    ]
                    escape_stored_fields(complaint["history"], ("key", "extra", "time"))
                conn.execute(
                    "UPDATE store SET value = ? WHERE key = 'complaints'",
                    (json.dumps(complaints, ensure_ascii=False),),
                )

                technicians = [item for item in technicians if isinstance(item, dict)]
                used_technician_ids = set()
                for technician in technicians:
                    if type(technician.get("id")) is not int or technician["id"] < 1 \
                            or technician["id"] in used_technician_ids:
                        technician["id"] = max(used_technician_ids, default=0) + 1
                    used_technician_ids.add(technician["id"])
                escape_stored_fields(technicians, ("name", "phone", "username"))
                conn.execute(
                    "UPDATE store SET value = ? WHERE key = 'technicians'",
                    (json.dumps(technicians, ensure_ascii=False),),
                )

                for key, fields in (
                    ("suggestions", ("park_en", "park_kn", "text", "category",
                                     "citizen_name", "date_submitted")),
                    ("extra_parks", ("slug", "en", "kn", "loc", "loc_kn", "desc")),
                ):
                    row = conn.execute("SELECT value FROM store WHERE key = ?", (key,)).fetchone()
                    records = json.loads(row["value"]) if row else []
                    records = [item for item in records if isinstance(item, dict)]
                    escape_stored_fields(records, fields)
                    if key == "suggestions":
                        used_suggestion_ids = set()
                        for record in records:
                            if type(record.get("id")) is not int or record["id"] < 1 \
                                    or record["id"] in used_suggestion_ids:
                                record["id"] = max(used_suggestion_ids, default=0) + 1
                            used_suggestion_ids.add(record["id"])
                            if record.get("status") not in SUGGESTION_STATUSES:
                                record["status"] = "new"
                    elif key == "extra_parks":
                        used_park_ids = set(BUILTIN_PARKS)
                        for record in records:
                            if type(record.get("id")) is not int \
                                    or record["id"] <= max(BUILTIN_PARKS) \
                                    or record["id"] in used_park_ids:
                                record["id"] = max(used_park_ids) + 1
                            used_park_ids.add(record["id"])
                            if not is_valid_photo(record.get("photo")):
                                record["photo"] = None
                    conn.execute(
                        "UPDATE store SET value = ? WHERE key = ?",
                        (json.dumps(records, ensure_ascii=False), key),
                    )
                conn.execute(
                    "INSERT INTO store (key, value) VALUES ('content_sanitized_v1', 'true')"
                )

            conn.execute("DELETE FROM store WHERE key = 'admin_password'")
            complaint_row = conn.execute(
                "SELECT value FROM store WHERE key = 'complaints'"
            ).fetchone()
            complaints = json.loads(complaint_row["value"]) if complaint_row else []
            next_id = max((int(c.get("id", 0)) for c in complaints), default=0)
            conn.execute(
                "INSERT INTO counters (key, value) VALUES ('complaints', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value)",
                (next_id,),
            )

            configured_password = os.environ.get("ADMIN_PASSWORD")
            if configured_password:
                if not 12 <= len(configured_password) <= 256:
                    raise RuntimeError("ADMIN_PASSWORD must be between 12 and 256 characters.")
                conn.execute(
                    "INSERT OR IGNORE INTO store (key, value) VALUES (?, ?)",
                    ("admin_password_hash", generate_password_hash(configured_password)),
                )
            conn.commit()
        finally:
            conn.close()


def get_value(key, default="null"):
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM store WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_value(key, value_json_text):
    with _lock:
        conn = get_db()
        try:
            conn.execute(
                """INSERT INTO store (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value_json_text),
            )
            conn.commit()
        finally:
            conn.close()


def get_collection(key):
    return json.loads(get_value(key, DEFAULTS.get(key, "[]")))


def set_collection(key, value):
    with _lock:
        set_value(key, json.dumps(value, ensure_ascii=False))


def update_collection(key, update):
    with _lock:
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM store WHERE key = ?", (key,)).fetchone()
            current = json.loads(row["value"]) if row else []
            updated, result = update(current)
            conn.execute(
                "INSERT INTO store (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(updated, ensure_ascii=False)),
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def merge_by_id(existing, updates):
    merged = {item.get("id"): item for item in existing}
    merged.update({item.get("id"): item for item in updates})
    return list(merged.values())


def json_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def require_csrf():
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify({"error": "Invalid or expired request token. Refresh the page."}), 403
    return None


def csrf_protected(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        error = require_csrf()
        return error if error else view(*args, **kwargs)
    return wrapped


def login_required(role=None):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            current_role = session.get("role")
            if not current_role or (role and current_role != role):
                return jsonify({"error": "Authentication required."}), 401
            return view(*args, **kwargs)
        return wrapped
    return decorate


def rate_limited(category, limit, window_seconds):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if request.method != "POST":
                return view(*args, **kwargs)
            client_ip = request.remote_addr or "unknown"
            identity = hashlib.sha256(
                f"{category}:{client_ip}".encode("utf-8")
            ).hexdigest()
            now = int(time.time())
            conn = get_db()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT window_start, count FROM rate_limits WHERE key = ?",
                    (identity,),
                ).fetchone()
                if row and now - row["window_start"] < window_seconds:
                    if row["count"] >= limit:
                        conn.rollback()
                        return jsonify({"error": "Too many requests. Please try again later."}), 429
                    conn.execute(
                        "UPDATE rate_limits SET count = count + 1 WHERE key = ?",
                        (identity,),
                    )
                else:
                    conn.execute(
                        "INSERT INTO rate_limits (key, window_start, count) VALUES (?, ?, 1) "
                        "ON CONFLICT(key) DO UPDATE SET window_start = excluded.window_start, count = 1",
                        (identity, now),
                    )
                conn.execute(
                    "DELETE FROM rate_limits WHERE window_start < ?",
                    (now - 24 * 60 * 60,),
                )
                conn.commit()
            finally:
                conn.close()
            return view(*args, **kwargs)
        return wrapped
    return decorate


def public_complaint(complaint):
    return {
        key: complaint.get(key)
        for key in (
            "id", "park_id", "park_en", "park_kn", "issue_type", "priority",
            "description", "status", "date_submitted", "date_submitted_ts",
            "date_resolved", "date_resolved_ts", "repair_notes", "history",
            "reopen_count", "feedback_rating", "feedback_comment",
        )
    } | {
        "photo_before": complaint.get("photo_before"),
        "photo_after": complaint.get("photo_after"),
    }


def is_valid_photo(photo):
    if not isinstance(photo, str):
        return False
    match = PHOTO_RE.fullmatch(photo)
    if not match:
        return False
    try:
        image = base64.b64decode(match.group(1), validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    return len(image) <= 1_500_000 and image.startswith(b"\xff\xd8\xff")


def find_park(park_id):
    if park_id in BUILTIN_PARKS:
        return {"id": park_id, "en": BUILTIN_PARKS[park_id]}
    return next(
        (park for park in get_collection("extra_parks") if park.get("id") == park_id),
        None,
    )


def clean_text(value, max_length, required=False):
    if not isinstance(value, str):
        return None if required else ""
    value = html.escape(value.strip(), quote=True)
    if (required and not value) or len(value) > max_length:
        return None
    return value


def normalize_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    return digits


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "Request is too large. Please use a smaller photo."}), 413


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/session", methods=["GET"])
def get_session():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    if session.get("role") == "technician":
        technician = next(
            (item for item in get_collection("technicians")
             if item.get("id") == session.get("technician", {}).get("id")),
            None,
        )
        if technician:
            session["technician"] = {
                "id": technician["id"], "name": technician.get("name", ""),
            }
        else:
            session.clear()
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
    return jsonify({
        "csrf_token": token,
        "role": session.get("role"),
        "technician": session.get("technician"),
    })


@app.route("/api/login/admin", methods=["POST"])
@csrf_protected
@rate_limited("login", 10, 15 * 60)
def admin_login():
    password = json_body().get("password")
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        return jsonify({"error": "Enter the municipality password."}), 400
    password_hash = get_value("admin_password_hash", "")
    if not password_hash:
        return jsonify({"error": "Municipality login is not configured on the server."}), 503
    if not check_password_hash(password_hash, password):
        return jsonify({"error": "Incorrect password."}), 401
    csrf_token = session.get("csrf_token")
    session.clear()
    session["role"] = "admin"
    session["csrf_token"] = csrf_token or secrets.token_urlsafe(32)
    session.permanent = True
    return jsonify({"role": "admin", "csrf_token": session["csrf_token"]})


@app.route("/api/login/technician", methods=["POST"])
@csrf_protected
@rate_limited("login", 10, 15 * 60)
def technician_login():
    body = json_body()
    username = clean_text(body.get("username"), 80, required=True)
    password = body.get("password")
    if username is None or not isinstance(password, str) or not 1 <= len(password) <= 256:
        return jsonify({"error": "Enter a username and password."}), 400
    technician = next(
        (item for item in get_collection("technicians")
         if item.get("username") == username
         and item.get("password_hash")
         and check_password_hash(item["password_hash"], password)),
        None,
    )
    if not technician:
        return jsonify({"error": "Incorrect username or password."}), 401
    csrf_token = session.get("csrf_token")
    session.clear()
    session["role"] = "technician"
    session["technician"] = {
        "id": technician["id"],
        "name": technician.get("name", ""),
    }
    session["csrf_token"] = csrf_token or secrets.token_urlsafe(32)
    session.permanent = True
    return jsonify({
        "role": "technician",
        "technician": session["technician"],
        "csrf_token": session["csrf_token"],
    })


@app.route("/api/logout", methods=["POST"])
@csrf_protected
def logout():
    session.clear()
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify({"ok": True, "csrf_token": session["csrf_token"]})


@app.route("/api/complaints", methods=["GET", "POST"])
def complaints_api():
    if request.method == "GET":
        role = session.get("role")
        if role == "admin":
            return jsonify(get_collection("complaints"))
        if role == "technician":
            technician_id = session["technician"]["id"]
            if not any(t.get("id") == technician_id for t in get_collection("technicians")):
                session.clear()
                return jsonify({"error": "Technician account is no longer active."}), 401
            assigned = [
                item for item in get_collection("complaints")
                if item.get("technician_id") == technician_id
            ]
            return jsonify([
                {key: value for key, value in item.items()
                 if key not in {"citizen_name", "citizen_phone", "feedback_comment"}}
                for item in assigned
            ])
        return jsonify({"error": "Authentication required."}), 401

    error = require_csrf()
    if error:
        return error
    role = session.get("role")
    body = request.get_json(silent=True)
    if role == "admin":
        if not isinstance(body, list):
            return jsonify({"error": "Expected a JSON array."}), 400
        for item in body:
            if not isinstance(item, dict) or item.get("status") not in STATUSES:
                return jsonify({"error": "Invalid complaint data."}), 400
        update_collection(
            "complaints", lambda stored: (merge_by_id(stored, body), None)
        )
        return jsonify({"ok": True})
    if role == "technician":
        if not isinstance(body, list):
            return jsonify({"error": "Expected a JSON array."}), 400
        technician_id = session["technician"]["id"]
        if not any(t.get("id") == technician_id for t in get_collection("technicians")):
            session.clear()
            return jsonify({"error": "Technician account is no longer active."}), 401
        def update_assigned_complaints(stored):
            submitted = {item.get("id"): item for item in body if isinstance(item, dict)}
            for item in stored:
                update = submitted.get(item.get("id"))
                if item.get("technician_id") != technician_id or not update:
                    continue
                if item.get("status") == "assigned" and update.get("status") == "progress":
                    item["status"] = "progress"
                    item.setdefault("history", []).append({
                        "key": "hist_progress", "ts": int(time.time() * 1000),
                    })
                elif item.get("status") == "progress" and update.get("status") == "resolved":
                    notes = clean_text(update.get("repair_notes", ""), 1200)
                    photo = update.get("photo_after")
                    if notes is None or not is_valid_photo(photo):
                        raise InvalidTechnicianUpdate(
                            "Add repair notes and a valid JPEG after-photo."
                        )
                    now_ts = int(time.time() * 1000)
                    item.update({
                        "status": "resolved",
                        "repair_notes": notes,
                        "photo_after": photo,
                        "date_resolved": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
                        "date_resolved_ts": now_ts,
                    })
                    item.setdefault("history", []).append({
                        "key": "hist_resolved", "ts": now_ts, "extra": notes or None,
                    })
            return stored, None
        try:
            update_collection("complaints", update_assigned_complaints)
        except InvalidTechnicianUpdate as error:
            return jsonify({"error": str(error)}), 400
        return jsonify({"ok": True})
    return jsonify({"error": "Authentication required."}), 401


@app.route("/api/complaints", methods=["PUT"])
@login_required("admin")
@csrf_protected
def replace_complaints():
    body = request.get_json(silent=True)
    if not isinstance(body, list) or any(
        not isinstance(item, dict) or item.get("status") not in STATUSES for item in body
    ):
        return jsonify({"error": "Invalid complaint data."}), 400
    update_collection(
        "complaints", lambda stored: (merge_by_id(stored, body), None)
    )
    return jsonify({"ok": True})


@app.route("/api/complaints", methods=["OPTIONS"])
def complaints_options():
    return "", 204


@app.route("/api/complaints/new", methods=["POST"])
@csrf_protected
@rate_limited("complaint", 10, 60 * 60)
def create_complaint():
    body = json_body()
    name = clean_text(body.get("citizen_name"), 100, required=True)
    phone = clean_text(body.get("citizen_phone"), 30, required=True)
    description = clean_text(body.get("description"), 1200, required=True)
    photo = body.get("photo_before")
    park_id = body.get("park_id")
    if type(park_id) is not int:
        park_id = 0
    park = find_park(park_id)
    digits = normalize_phone(phone)
    if (name is None or description is None or len(digits) != 10
            or not is_valid_photo(photo) or not park
            or body.get("issue_type") not in ISSUE_TYPES
            or body.get("priority") not in PRIORITIES):
        return jsonify({"error": "Check the report details and attach a valid JPEG photo."}), 400
    park_en = clean_text(park["en"], 120, required=True)
    park_kn = clean_text(body.get("park_kn"), 120, required=True)
    if park_kn is None:
        return jsonify({"error": "Invalid park name."}), 400
    now_ts = int(time.time() * 1000)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM counters WHERE key = 'complaints'"
        ).fetchone()
        complaint_id = (row["value"] if row else 0) + 1
        conn.execute(
            "INSERT INTO counters (key, value) VALUES ('complaints', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (complaint_id,),
        )
        complaint = {
            "id": complaint_id,
            "park_id": park_id,
            "park_en": park_en,
            "park_kn": park_kn,
            "citizen_name": name,
            "citizen_phone": phone,
            "issue_type": body["issue_type"],
            "priority": body["priority"],
            "description": description,
            "photo_before": photo,
            "photo_after": None,
            "photo_consent": body.get("photo_consent") is True,
            "status": "pending",
            "technician_id": None,
            "date_submitted": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
            "date_submitted_ts": now_ts,
            "date_resolved": None,
            "date_resolved_ts": None,
            "reopen_count": 0,
            "repair_notes": "",
            "feedback_rating": None,
            "feedback_comment": None,
            "history": [{"key": "hist_submitted", "ts": now_ts, "extra": None}],
        }
        row = conn.execute(
            "SELECT value FROM store WHERE key = 'complaints'"
        ).fetchone()
        complaints = json.loads(row["value"]) if row else []
        complaints.append(complaint)
        conn.execute(
            "INSERT INTO store (key, value) VALUES ('complaints', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(complaints, ensure_ascii=False),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify({"id": complaint_id}), 201


@app.route("/api/track", methods=["POST"])
@csrf_protected
@rate_limited("track", 30, 60 * 60)
def track_complaint():
    body = json_body()
    try:
        complaint_id = int(body.get("id"))
    except (TypeError, ValueError):
        complaint_id = 0
    phone = body.get("phone")
    if not isinstance(phone, str):
        phone = ""
    complaint = next(
        (item for item in get_collection("complaints")
         if item.get("id") == complaint_id
         and hmac.compare_digest(
             normalize_phone(item.get("citizen_phone", "")),
             normalize_phone(phone),
         )),
        None,
    )
    if not complaint:
        return jsonify({"error": "Report not found. Check the report number and phone number."}), 404
    result = public_complaint(complaint)
    technicians = get_collection("technicians")
    tech = next((item for item in technicians
                 if item.get("id") == complaint.get("technician_id")), None)
    result["technician_name"] = tech.get("name") if tech else None
    return jsonify(result)


@app.route("/api/feedback", methods=["POST"])
@csrf_protected
@rate_limited("feedback", 10, 60 * 60)
def submit_feedback():
    body = json_body()
    try:
        complaint_id = int(body.get("id"))
        rating = int(body.get("rating"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid feedback."}), 400
    phone = body.get("phone", "")
    comment = clean_text(body.get("comment", ""), 500)
    if not 1 <= rating <= 5 or comment is None or not isinstance(phone, str):
        return jsonify({"error": "Invalid feedback."}), 400
    def add_feedback(complaints):
        found = next((item for item in complaints
                      if item.get("id") == complaint_id
                      and hmac.compare_digest(
                          normalize_phone(item.get("citizen_phone", "")),
                          normalize_phone(phone),
                      )), None)
        if not found:
            return complaints, "not_found"
        if found.get("status") != "verified":
            return complaints, "not_verified"
        found["feedback_rating"] = rating
        found["feedback_comment"] = comment
        return complaints, None

    result = update_collection("complaints", add_feedback)
    if result == "not_found":
        return jsonify({"error": "Report not found."}), 404
    if result == "not_verified":
        return jsonify({"error": "Feedback is available after a report is verified."}), 409
    return jsonify({"ok": True})


@app.route("/api/gallery", methods=["GET"])
def gallery():
    resolved = [
        {
            "id": item.get("id"),
            "park_en": item.get("park_en"),
            "park_kn": item.get("park_kn"),
            "issue_type": item.get("issue_type"),
            "photo_before": item.get("photo_before"),
            "photo_after": item.get("photo_after"),
            "status": item.get("status"),
        }
        for item in get_collection("complaints")
        if item.get("photo_consent") is True
        and item.get("photo_after")
        and item.get("status") in {"resolved", "verified"}
    ]
    return jsonify(resolved)


@app.route("/api/technicians", methods=["GET", "POST"])
def technicians_api():
    if request.method == "GET":
        if session.get("role") != "admin":
            return jsonify({"error": "Authentication required."}), 401
        return jsonify([
            {key: value for key, value in tech.items()
             if key not in {"password", "password_hash"}}
            for tech in get_collection("technicians")
        ])
    error = require_csrf()
    if error:
        return error
    if session.get("role") != "admin":
        return jsonify({"error": "Authentication required."}), 401
    body = request.get_json(silent=True)
    if not isinstance(body, list) or len(body) > 500:
        return jsonify({"error": "Invalid technician list."}), 400
    existing = {tech.get("id"): tech for tech in get_collection("technicians")}
    clean = []
    for tech in body:
        if not isinstance(tech, dict):
            return jsonify({"error": "Invalid technician data."}), 400
        name = clean_text(tech.get("name"), 100, required=True)
        username = clean_text(tech.get("username"), 80, required=True)
        if name is None or username is None:
            return jsonify({"error": "Technician name and username are required."}), 400
        old = existing.get(tech.get("id"), {})
        password_hash = old.get("password_hash")
        password = tech.get("password")
        if password:
            if not isinstance(password, str) or not 12 <= len(password) <= 256:
                return jsonify({"error": "Technician passwords must be between 12 and 256 characters."}), 400
            password_hash = generate_password_hash(password)
        if not password_hash:
            return jsonify({"error": "Set a password for each technician."}), 400
        clean.append({
            "id": tech.get("id"),
            "name": name,
            "phone": clean_text(tech.get("phone"), 30),
            "username": username,
            "password_hash": password_hash,
            "park_id": tech.get("park_id"),
        })
    if len({tech["username"].casefold() for tech in clean}) != len(clean):
        return jsonify({"error": "Technician usernames must be unique."}), 400
    set_collection("technicians", clean)
    return jsonify({"ok": True})


@app.route("/api/suggestions", methods=["GET", "POST"])
@rate_limited("suggestion", 5, 60 * 60)
def suggestions_api():
    if request.method == "GET":
        if session.get("role") != "admin":
            return jsonify({"error": "Authentication required."}), 401
        return jsonify(get_collection("suggestions"))
    error = require_csrf()
    if error:
        return error
    body = request.get_json(silent=True)
    if session.get("role") == "admin":
        if not isinstance(body, list) or len(body) > 5000:
            return jsonify({"error": "Invalid suggestions list."}), 400
        set_collection("suggestions", body)
        return jsonify({"ok": True})
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid suggestion."}), 400
    park_name = clean_text(body.get("park_en"), 120, required=True)
    suggestion = clean_text(body.get("suggestion"), 1000, required=True)
    park_id = body.get("park_id")
    if type(park_id) is not int:
        park_id = 0
    park = find_park(park_id)
    category = body.get("category")
    if (park is None or park_name != park.get("en")
            or suggestion is None or category not in SUGGESTION_CATEGORIES):
        return jsonify({"error": "Enter a park and a suggestion (up to 1,000 characters)."}), 400
    def append_suggestion(suggestions):
        suggestion_id = max((int(item.get("id", 0)) for item in suggestions), default=0) + 1
        suggestions.append({
            "id": suggestion_id,
            "park_id": park_id,
            "park_en": park_name,
            "park_kn": clean_text(body.get("park_kn"), 120) or park_name,
            "text": suggestion,
            "category": clean_text(body.get("category"), 50),
            "date_submitted": time.strftime("%Y-%m-%d %H:%M", time.localtime()),
            "status": "new",
        })
        return suggestions, suggestion_id

    suggestion_id = update_collection("suggestions", append_suggestion)
    return jsonify({"id": suggestion_id}), 201


@app.route("/api/extra-parks", methods=["GET", "POST"])
def extra_parks_api():
    if request.method == "GET":
        return jsonify(get_collection("extra_parks"))
    error = require_csrf()
    if error:
        return error
    if session.get("role") != "admin":
        return jsonify({"error": "Authentication required."}), 401
    body = request.get_json(silent=True)
    if not isinstance(body, list) or len(body) > 500:
        return jsonify({"error": "Invalid park list."}), 400
    existing_ids = set(BUILTIN_PARKS)
    clean_parks = []
    for park in body:
        if (not isinstance(park, dict) or type(park.get("id")) is not int
                or park["id"] <= max(BUILTIN_PARKS)):
            return jsonify({"error": "Each park needs a unique ID, name, and location."}), 400
        if park["id"] in existing_ids:
            return jsonify({"error": "Park IDs must be unique."}), 400
        existing_ids.add(park["id"])
        name = clean_text(park.get("en"), 120, required=True)
        location = clean_text(park.get("loc"), 250, required=True)
        photo = park.get("photo")
        if (name is None or location is None
                or (photo is not None and not is_valid_photo(photo))):
            return jsonify({"error": "Each park needs a name and location."}), 400
        clean_parks.append({
            "id": park["id"],
            "slug": clean_text(park.get("slug"), 80),
            "en": name,
            "kn": clean_text(park.get("kn"), 120) or name,
            "loc": location,
            "loc_kn": clean_text(park.get("loc_kn"), 250) or location,
            "desc": clean_text(park.get("desc"), 1000),
            "photo": photo,
        })
    set_collection("extra_parks", clean_parks)
    return jsonify({"ok": True})


@app.route("/api/admin-password", methods=["POST"])
@login_required("admin")
@csrf_protected
def update_admin_password():
    password = json_body().get("value")
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        return jsonify({"error": "Choose a password between 12 and 256 characters."}), 400
    set_value("admin_password_hash", generate_password_hash(password))
    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_from_directory(APP_DIR, "index.html")


@app.route("/<path:filename>")
def other_files(filename):
    extension = os.path.splitext(filename)[1].lower()
    public_assets = {".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".woff2"}
    if (filename.startswith(".") or filename.startswith("api/")
            or (filename != "index.html" and extension not in public_assets)):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(APP_DIR, filename)


init_db()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
