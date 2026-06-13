"""Barkeep's Ledger — a lightweight bar back-office.

A single self-hosted Flask app:
  * Dashboard   — sales, labor %, COGS %, prime cost (Square + logged invoices)
  * Invoices    — photograph an invoice; Claude reads it; you confirm & save
  * Inventory   — par levels and a fast walk-around count
  * Settings    — Square + targets + AI model

Run:  python app.py    (see README for configuration)
"""
import hashlib
import hmac
import math
import os
import secrets
import sqlite3
import threading
import time
import uuid

# Load .env before importing modules that read the environment at import time.
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
# .env holds APP_SECRET (the session-token signing key) and APP_PASSWORD in plaintext.
# The auth model rests on filesystem perms (see db._restrict_db_perms for the DB), so
# tighten .env to owner-only too — by umask it can land world-readable.
try:
    if os.path.exists(_ENV_PATH) and (os.stat(_ENV_PATH).st_mode & 0o077):
        os.chmod(_ENV_PATH, 0o600)
except OSError:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(_ENV_PATH)
except ImportError:
    pass

from flask import (
    Flask, g, jsonify, request, send_from_directory, abort, Response,
)
from werkzeug.exceptions import HTTPException

import db
import cogs
import exports
import money
import recipes
import reports
import square_client
import invoice_ai
from invoice_ai import parse_invoice, InvoiceError

BASE_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.teardown_appcontext(db.close_db)

# Fail closed at IMPORT time (so it fires under gunicorn/any WSGI server, not just
# `python app.py`): refuse to run unauthenticated unless open mode is explicitly
# acknowledged. run.sh sets ALLOW_OPEN for local dev; production sets APP_PASSWORD.
if not os.environ.get("APP_PASSWORD") and not os.environ.get("ALLOW_OPEN"):
    raise RuntimeError(
        "Refusing to start without authentication. Set APP_PASSWORD, or set "
        "ALLOW_OPEN=1 to run open on a trusted local network.")

# Warn (don't hard-fail — that could lock the owner out of a running install) when
# the passcode is short: the global 10/60s login throttle and the secret tunnel URL
# are the only brute-force defenses, so a weak passcode is the main residual risk.
if os.environ.get("APP_PASSWORD") and len(os.environ["APP_PASSWORD"]) < 12:
    print("  [!] APP_PASSWORD is shorter than 12 characters. Use a longer passphrase: "
          "it's the main defense behind the public tunnel.")

# Cap uploads so a giant (or malicious) file can't exhaust memory/disk. 32 MB
# covers a full-resolution phone photo while still bounding abuse.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024


@app.errorhandler(413)
def _too_large(_e):
    mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify({"error": f"That file is too large (max {mb} MB)."}), 413


@app.errorhandler(HTTPException)
def _json_http_error(e):
    # Every error matches the {"error": ...} shape the SPA expects (abort(404, description="Not found."),
    # a malformed-JSON 400, etc.), not Werkzeug's default HTML page.
    return jsonify({"error": e.description}), e.code


@app.errorhandler(Exception)
def _json_error(_e):
    # Last resort for an uncaught bug: a clean JSON 500, never an HTML traceback.
    return jsonify({"error": "Internal server error."}), 500


def body():
    """request.json coerced to a dict. A syntactically valid but non-object body
    (a JSON array/string/number) would pass `or {}` and then AttributeError on
    .get(); this returns {} for anything that isn't an object. A body that is
    present but UNPARSEABLE is a client error — reject it with 400 rather than
    silently treating corrupt JSON as {} and creating an all-defaults row."""
    j = request.get_json(silent=True)
    if j is None and request.data:
        abort(400, description="Invalid JSON body.")
    return j if isinstance(j, dict) else {}


# --- auth -------------------------------------------------------------------
# Single shared passcode for a personal tool. Set APP_PASSWORD to enable it;
# leave it unset to run open (fine on a private LAN, noted in the README).
#
# Design tradeoffs, deliberate for a single-user self-hosted app (not bugs):
#  * The bearer token is HMAC(secret, passcode) — deterministic and non-expiring.
#    There's one user and one passcode; rotating APP_SECRET invalidates all
#    tokens (tested). A random session-id + TTL store would add a moving part
#    without a real benefit at this scale.
#  * app_secret and the Square token live in the SQLite file in plaintext,
#    protected by filesystem permissions (the DB also holds all the business
#    data). Encrypting them needs a key stored outside data/, which just moves
#    the secret — not worth it for a self-hosted personal tool.

def _app_secret():
    """The key the session token is signed with. Prefer an explicit APP_SECRET;
    otherwise use a random per-install secret persisted in the DB. Never a shipped
    constant — a known signing key would let anyone forge a session token."""
    env = os.environ.get("APP_SECRET", "").strip()
    if env:
        return env
    s = db.get_setting("app_secret")
    if not s:
        # Insert-if-absent then re-read, so concurrent first-boot requests all
        # converge on whichever random secret landed first (no token churn).
        db.set_setting_default("app_secret", secrets.token_hex(32))
        s = db.get_setting("app_secret")
    return s


def _token_for(pw):
    return hmac.new(_app_secret().encode(), pw.encode(), hashlib.sha256).hexdigest()


def _expected_token():
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw:
        return None
    return _token_for(pw)


def _authed():
    expected = _expected_token()
    if expected is None:
        return True
    sent = request.headers.get("Authorization", "")
    if sent.startswith("Bearer "):
        sent = sent[7:]
    # Compare as bytes: Werkzeug decodes headers as latin-1, so a non-ASCII
    # Authorization value would make compare_digest raise TypeError (-> 500)
    # instead of returning a clean False. Bytes comparison tolerates any input.
    return hmac.compare_digest(sent.encode("utf-8", "ignore"), expected.encode())


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@app.before_request
def _open_mode_loopback_only():
    """Open mode (no APP_PASSWORD) disables auth entirely, so it must never be
    reachable off-host. `python app.py` already forces a 127.0.0.1 bind, but under
    gunicorn the app can't choose the bind — so enforce loopback at the request
    layer too: in open mode, reject any non-loopback client. With APP_PASSWORD set
    this is a no-op (auth governs access and trusted remote clients are allowed)."""
    if _expected_token() is not None:
        return
    addr = request.remote_addr or ""
    if not (addr in _LOOPBACK or addr.startswith("127.")):
        return jsonify({"error": "Running without authentication; serving localhost "
                        "only. Set APP_PASSWORD to allow remote access."}), 403
    # remote_addr is loopback even when a reverse proxy / tunnel fronts us from
    # localhost, so the check above can't tell a real local client from a proxied
    # remote one. ANY forwarding header (or a multi-hop access_route) means we're
    # behind a proxy — refuse to serve open mode rather than trust a spoofable hop.
    # Cover the common forwarders, not just X-Forwarded-For (cloudflared uses
    # CF-Connecting-IP and doesn't populate access_route).
    _PROXY_HEADERS = ("X-Forwarded-For", "CF-Connecting-IP", "X-Real-IP",
                      "Forwarded", "True-Client-IP")
    if len(request.access_route) > 1 or any(request.headers.get(h) for h in _PROXY_HEADERS):
        return jsonify({"error": "Running without authentication and detected behind "
                        "a proxy; refusing to serve. Set APP_PASSWORD."}), 403
    # Defense-in-depth against a header-stripping proxy that adds no forwarding
    # header at all: open mode is localhost-developer-only, so the Host must be a
    # loopback name too. A public proxy forwards its own (non-loopback) Host.
    host = (request.host or "").rsplit(":", 1)[0]
    if host and host not in _LOOPBACK:
        return jsonify({"error": "Running without authentication; only reachable as "
                        "localhost. Set APP_PASSWORD to allow remote access."}), 403


@app.before_request
def _guard():
    p = request.path
    # /api/config is readable pre-login so the SPA can learn whether auth is on.
    if (p == "/" or p.startswith("/static/") or p == "/api/login"
            or p == "/api/health" or p == "/api/config"):
        return
    if p.startswith("/api/") or p.startswith("/uploads/"):
        if not _authed():
            return jsonify({"error": "unauthorized"}), 401


@app.before_request
def _resolve_location():
    """Resolve the per-request store from the X-Location-Id header (the SPA sends
    it on every call) and stash it on g, validated against the locations table.
    Left unset when the header is absent/invalid, so db.active_location_id() falls
    back to the persisted default. Runs after _guard, so it never fires on a
    request that failed auth."""
    # Gate behind auth: don't run a DB query for an unauthenticated caller (the
    # pre-login allowlisted routes don't need a store anyway).
    if not _authed():
        return
    h = request.headers.get("X-Location-Id")
    if not h:
        return
    # int() accepts arbitrarily large values, but SQLite raises OverflowError
    # (not ValueError) when binding anything outside signed-64-bit — keep the DB
    # lookup inside the guard so a junk/oversized header falls back to the default
    # store instead of 500ing every endpoint via this before_request hook.
    try:
        lid = int(h)
        row = db.get_db().execute(
            "SELECT 1 FROM locations WHERE id=? AND archived=0", (lid,)).fetchone()
    except (TypeError, ValueError, OverflowError):
        return
    if row:
        g.location_override = lid


# Throttle passcode guessing. Behind the Cloudflare tunnel every client connects
# from loopback and CF-Connecting-IP is attacker-spoofable, so per-client keying
# is impossible — we throttle GLOBALLY with a short sliding window. Once the
# window's wrong-guess budget is spent we SHORT-CIRCUIT (429) BEFORE hashing the
# guess, so it's a real rate limit, not a cosmetic message swap.
#
# Tradeoff (deliberate): a sustained flood can make the owner wait out the
# WINDOW too. That's acceptable here because the tunnel URL is itself a random
# per-install secret (the real first line of defense); the window is short so
# any lockout self-heals in seconds, and a brief stall under active attack is
# preferable to leaving guessing unthrottled. The counter is in PROCESS memory,
# so deploy with a single worker (the README notes this) — multiple workers each
# keep their own budget and weaken the limit.
_LOGIN_FAILS = []          # recent wrong-guess timestamps (monotonic)
_LOGIN_MAX = 10            # wrong guesses per window before requests are 429'd
_LOGIN_WINDOW = 60         # seconds (short, so a throttle self-heals quickly)
_LOGIN_LOCK = threading.Lock()   # the threaded dev server can race the budget check


def _login_recent_fails():
    now = time.monotonic()
    _LOGIN_FAILS[:] = [t for t in _LOGIN_FAILS if now - t < _LOGIN_WINDOW]
    return len(_LOGIN_FAILS)


@app.post("/api/login")
def login():
    expected = _expected_token()
    if expected is None:
        return jsonify({"token": "", "auth_required": False})
    pw = body().get("password", "")
    if not isinstance(pw, str):
        # A non-string password (number/list/bool) would AttributeError in
        # _token_for(pw).encode() -> 500, slipping past the throttle. Treat it as
        # an ordinary wrong guess so it 401s AND increments the rate limit.
        pw = ""
    # Hold the lock across the budget check AND the mutation so concurrent
    # requests can't both slip past the threshold or corrupt the list.
    with _LOGIN_LOCK:
        if _login_recent_fails() >= _LOGIN_MAX:   # refuse BEFORE evaluating the guess
            return jsonify({"error": "Too many attempts. Wait a minute and try again."}), 429
        if hmac.compare_digest(_token_for(pw), expected):
            _LOGIN_FAILS.clear()
            return jsonify({"token": _token_for(pw), "auth_required": True})
        _LOGIN_FAILS.append(time.monotonic())
    return jsonify({"error": "Wrong passcode."}), 401


# --- pages / static ---------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/uploads/<path:name>")
def uploaded(name):
    # Only serve an image that belongs to an invoice in the active store, so a
    # guessed/scraped filename can't pull another store's invoice photo.
    if not db.get_db().execute(
        "SELECT 1 FROM invoices WHERE image_path=? AND location_id IS ?",
        (name, db.active_location_id()),
    ).fetchone():
        abort(404, description="Not found.")
    return send_from_directory(UPLOAD_DIR, name)


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


# --- config / settings ------------------------------------------------------

@app.get("/api/config")
def config():
    # Readable pre-login ONLY so the SPA can learn whether a passcode is required;
    # don't disclose store/Square/target config to an unauthenticated caller.
    if not _authed():
        return jsonify({"auth_required": True, "square_configured": False})
    s = db.all_settings()
    loc_row = db.get_db().execute(
        "SELECT square_location_id FROM locations WHERE id=?", (db.active_location_id(),)
    ).fetchone()
    return jsonify({
        "auth_required": _expected_token() is not None,
        "square_configured": square_client.is_configured(),
        "square_env": s.get("square_env", "production"),
        "square_location_id": (loc_row["square_location_id"] if loc_row else "") or "",
        "square_version": s.get("square_version", ""),
        "has_square_token": bool((s.get("square_token") or "").strip()),
        "ai_model": s.get("ai_model", "claude-opus-4-8"),
        "ai_key_present": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "target_cogs_pct": s.get("target_cogs_pct", "30"),
        "target_labor_pct": s.get("target_labor_pct", "25"),
        "default_hourly_wage": s.get("default_hourly_wage", "0"),
        "price_alert_pct": s.get("price_alert_pct", "10"),
    })


@app.post("/api/settings")
def save_settings():
    data = body()
    # Reject a non-numeric value for the numeric settings rather than store it
    # verbatim: consumers defensively fall back to the hard-coded default, so a bad
    # value would be silently dropped and the user's save would appear to succeed.
    _NUMERIC_SETTINGS = ("target_cogs_pct", "target_labor_pct", "default_hourly_wage",
                         "price_alert_pct")
    for key in _NUMERIC_SETTINGS:
        if key in data and _scalar(data[key]):
            v = _f(data[key])
            if v is None:
                return jsonify({"error": f"{key} must be a number."}), 400
            # Reject negatives: a negative price_alert_pct would fire an alert on
            # every change, a negative target would render nonsensically. Percent
            # fields also can't exceed 100.
            if v < 0:
                return jsonify({"error": f"{key} must not be negative."}), 400
            if key in ("target_cogs_pct", "target_labor_pct") and v > 100:
                return jsonify({"error": f"{key} must be between 0 and 100."}), 400
    for key in ("square_env", "square_version", "ai_model") + _NUMERIC_SETTINGS:
        if key in data and _scalar(data[key]):
            db.set_setting(key, data[key])
    # The Square location is per-store now: write it onto the active store's row,
    # not a shared global setting.
    if "square_location_id" in data:
        sqid = _s(data["square_location_id"])
        if sqid and db.get_db().execute(
            "SELECT 1 FROM locations WHERE square_location_id=? AND id<>?",
            (sqid, db.active_location_id())
        ).fetchone():
            return jsonify({"error": "That Square location is already assigned to another store."}), 400
        db.get_db().execute("UPDATE locations SET square_location_id=? WHERE id=?",
                            (sqid, db.active_location_id()))
        # Drop any cached daily_sales for the newly-assigned id: the cache keys only
        # on square_location_id, so a sequentially-reused id could otherwise serve a
        # previous store's stale net_sales without a historical refetch.
        if sqid:
            db.get_db().execute("DELETE FROM daily_sales WHERE square_location_id=?", (sqid,))
        db.get_db().commit()
    # Only persist a Square token if a non-blank one is supplied (so the UI can
    # show "set" without round-tripping the secret).
    tok = _s(data.get("square_token"))
    if tok:
        db.set_setting("square_token", tok)
    return jsonify({"ok": True})


@app.post("/api/backup")
def backup_now():
    """Snapshot the database on demand (also runs at startup and periodically)."""
    path = db.backup()
    if not path:
        return jsonify({"error": "No database to back up yet."}), 400
    return jsonify({"ok": True, "file": os.path.basename(path)})


@app.get("/api/square-locations")
def square_locations():
    return jsonify(square_client.list_locations())


# --- stores / active location ----------------------------------------------

@app.get("/api/locations")
def location_list():
    rows = db.get_db().execute(
        "SELECT id, name, square_location_id FROM locations WHERE archived=0 ORDER BY id"
    ).fetchall()
    return jsonify({"locations": [dict(r) for r in rows], "active": db.active_location_id()})


@app.get("/api/active-location")
def active_location_get():
    return jsonify({"active": db.active_location_id()})


@app.put("/api/active-location")
def active_location_set():
    """Persist the default store (for a fresh device / non-SPA callers). The SPA's
    per-request X-Location-Id header is the real source of truth; the Square id is
    now resolved per-request from the locations table, so nothing is mirrored into
    a shared global setting here."""
    loc_id = _i(body().get("location_id"))
    row = db.get_db().execute(
        "SELECT id FROM locations WHERE id=? AND archived=0", (loc_id,)
    ).fetchone() if loc_id is not None else None
    if not row:
        return jsonify({"error": "Unknown location."}), 400
    db.set_setting("active_location_id", row["id"])
    return jsonify({"ok": True, "active": row["id"]})


# --- dashboard --------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard():
    start, end = cogs.parse_range(request.args.get("start"), request.args.get("end"))
    return jsonify(cogs.summary(start, end))


# --- invoices ---------------------------------------------------------------

@app.post("/api/invoices/parse")
def invoice_parse():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded."}), 400
    f = request.files["image"]
    ext = os.path.splitext(f.filename or "")[1].lower() or ".jpg"
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"Unsupported image type: {ext}"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"error": "Empty image."}), 400
    # Don't trust the client extension alone — confirm the bytes are a real image
    # before persisting (a renamed payload shouldn't land in uploads/).
    if invoice_ai.HAVE_PIL:
        import io as _io
        try:
            invoice_ai.Image.open(_io.BytesIO(raw)).verify()
        except Exception:
            return jsonify({"error": "That file isn't a readable image."}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(UPLOAD_DIR, fname), "wb") as out:
        out.write(raw)

    try:
        parsed = parse_invoice(raw, f.mimetype)
    except InvoiceError as e:
        # Keep the image so the user can still log it by hand.
        return jsonify({"error": str(e), "image_path": fname}), 422
    return jsonify({"image_path": fname, "parsed": parsed})


@app.post("/api/invoices")
def invoice_create():
    d = body()
    items = d.get("line_items")
    items = items if isinstance(items, list) else []
    vendor = _s(d.get("vendor"))
    # Validate the date (400 on blank/non-ISO): an unvalidated invoice_date is
    # compared lexically downstream, so a bad value would silently drop the invoice
    # from COGS, the dashboard and every date-ranged report while still showing in
    # date-agnostic totals — a number-trust inconsistency.
    inv_date = str(cogs._iso_or_400(d.get("invoice_date")))
    database = db.get_db()
    loc = db.active_location_id()
    # Guard against logging the same delivery twice (a re-snapped photo or a
    # double-tap), which would silently double-count purchases and inflate COGS.
    # The client can re-submit with confirm_duplicate=true to override.
    if not d.get("confirm_duplicate"):
        dup = _find_duplicate_invoice(
            database, loc, vendor, d.get("invoice_number", ""), inv_date, _f(d.get("total")))
        if dup:
            return jsonify({"error": "duplicate", "duplicate": dup}), 409
    cur = database.execute(
        "INSERT INTO invoices(location_id, vendor, invoice_date, invoice_number, category, "
        "subtotal, tax, total, image_path, notes, raw_json, status, payment_account) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            loc, vendor, inv_date, _s(d.get("invoice_number")),
            _s(d.get("category")) or None, money.normalize(d.get("subtotal")), money.normalize(d.get("tax")),
            money.normalize(d.get("total")), _image_name(d.get("image_path")), _s(d.get("notes")),
            _s(d.get("raw_json")), _s(d.get("status")) or "closed", _s(d.get("payment_account")) or None,
        ),
    )
    inv_id = cur.lastrowid
    for it in items:
        if not isinstance(it, dict):
            continue
        vi_id, cat_id = _resolve_vendor_item(database, vendor, inv_date, it, loc)
        database.execute(
            "INSERT INTO invoice_items(invoice_id, name, qty, unit, unit_cost, total, "
            "vendor_item_id, category_id) VALUES(?,?,?,?,?,?,?,?)",
            (inv_id, _s(it.get("name")), _f(it.get("qty")), _s(it.get("unit")) or None,
             _f(it.get("unit_cost")), money.normalize(it.get("total")), vi_id, cat_id),
        )
    database.commit()
    return jsonify({"id": inv_id})


@app.get("/api/invoices")
def invoice_list():
    """The Orders view. Optional filters: start, end, vendor, status, q (search)."""
    where, params = ["location_id IS ?"], [db.active_location_id()]
    if request.args.get("start"):
        # Validate like the report endpoints (400 on garbage) instead of binding a
        # bad string into a lexical comparison that silently returns nothing.
        where.append("invoice_date >= ?"); params.append(str(cogs._iso_or_400(request.args["start"])))
    if request.args.get("end"):
        where.append("invoice_date <= ?"); params.append(str(cogs._iso_or_400(request.args["end"])))
    if request.args.get("vendor"):
        where.append("lower(vendor) = lower(?)"); params.append(request.args["vendor"])
    if request.args.get("status"):
        where.append("status = ?"); params.append(request.args["status"])
    if request.args.get("q"):
        where.append("(vendor LIKE ? OR invoice_number LIKE ?)")
        params += [f"%{request.args['q']}%"] * 2
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db.get_db().execute(
        "SELECT id, vendor, invoice_date, invoice_number, category, total, image_path, "
        "status, payment_account, upload_date "
        f"FROM invoices {clause} ORDER BY invoice_date DESC, id DESC LIMIT 500",
        params,
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/invoices/<int:inv_id>")
def invoice_get(inv_id):
    db_ = db.get_db()
    row = db_.execute("SELECT * FROM invoices WHERE id=? AND location_id IS ?",
                      (inv_id, db.active_location_id())).fetchone()
    if not row:
        abort(404, description="Not found.")
    items = db_.execute(
        "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (inv_id,)
    ).fetchall()
    out = dict(row)
    out["line_items"] = [dict(i) for i in items]
    out["reconciliation"] = _reconcile(
        row["subtotal"], row["tax"], row["total"], out["line_items"])
    return jsonify(out)


@app.delete("/api/invoices/<int:inv_id>")
def invoice_delete(inv_id):
    db_ = db.get_db()
    loc = db.active_location_id()
    row = db_.execute("SELECT image_path FROM invoices WHERE id=? AND location_id IS ?",
                      (inv_id, loc)).fetchone()
    if not row:
        abort(404, description="Not found.")
    # Which SKUs does this invoice touch? Capture before the cascade so we can
    # recompute their last price afterward (deleting the newest delivery must not
    # strand a now-wrong "last price" on the vendor item).
    affected = [r["vendor_item_id"] for r in db_.execute(
        "SELECT DISTINCT vendor_item_id FROM invoice_items "
        "WHERE invoice_id=? AND vendor_item_id IS NOT NULL", (inv_id,))]
    db_.execute("DELETE FROM invoices WHERE id=? AND location_id IS ?", (inv_id, loc))
    _recompute_last_price(db_, affected, loc)
    db_.commit()
    if row["image_path"]:
        try:
            # basename-guard even on read: never let a stored traversal value
            # reach os.remove outside UPLOAD_DIR.
            os.remove(os.path.join(UPLOAD_DIR, os.path.basename(row["image_path"])))
        except OSError:
            pass
    return jsonify({"ok": True})


@app.put("/api/invoices/<int:inv_id>")
def invoice_update(inv_id):
    """Edit a saved invoice: update the header and replace its line items. Lets
    the owner fix an AI misparse instead of delete-and-re-enter. The image and
    the original AI audit (raw_json) are preserved."""
    d = body()
    database = db.get_db()
    loc = db.active_location_id()
    if not database.execute(
        "SELECT 1 FROM invoices WHERE id=? AND location_id IS ?", (inv_id, loc)
    ).fetchone():
        abort(404, description="Not found.")
    vendor = _s(d.get("vendor"))
    # Validate the date (400 on blank/non-ISO): an unvalidated invoice_date is
    # compared lexically downstream, so a bad value would silently drop the invoice
    # from COGS, the dashboard and every date-ranged report while still showing in
    # date-agnostic totals — a number-trust inconsistency.
    inv_date = str(cogs._iso_or_400(d.get("invoice_date")))
    # Editing an invoice to collide with ANOTHER existing one (same vendor+number, or
    # vendor+date+total) would create two records for one delivery — the same
    # double-count the create guard prevents. Check, excluding this invoice itself.
    if not d.get("confirm_duplicate"):
        dup = _find_duplicate_invoice(database, loc, vendor, d.get("invoice_number", ""),
                                      inv_date, _f(d.get("total")), exclude_id=inv_id)
        if dup:
            return jsonify({"error": "duplicate", "duplicate": dup}), 409
    database.execute(
        "UPDATE invoices SET vendor=?, invoice_date=?, invoice_number=?, category=?, "
        "subtotal=?, tax=?, total=?, notes=?, status=?, payment_account=? "
        "WHERE id=? AND location_id IS ?",
        (vendor, inv_date, _s(d.get("invoice_number")), _s(d.get("category")) or None,
         money.normalize(d.get("subtotal")), money.normalize(d.get("tax")), money.normalize(d.get("total")),
         _s(d.get("notes")), _s(d.get("status")) or "closed", _s(d.get("payment_account")) or None,
         inv_id, loc),
    )
    items = d.get("line_items")
    items = [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []
    # SKUs whose lines are being replaced — recompute their last price after, so a
    # line edited OUT can't strand a stale price (mirrors invoice_delete).
    old_vis = {r["vendor_item_id"] for r in database.execute(
        "SELECT DISTINCT vendor_item_id FROM invoice_items "
        "WHERE invoice_id=? AND vendor_item_id IS NOT NULL", (inv_id,))}
    database.execute("DELETE FROM invoice_items WHERE invoice_id=?", (inv_id,))
    new_vis = set()
    for it in items:
        vi_id, cat_id = _resolve_vendor_item(database, vendor, inv_date, it, loc)
        new_vis.add(vi_id)
        database.execute(
            "INSERT INTO invoice_items(invoice_id, name, qty, unit, unit_cost, total, "
            "vendor_item_id, category_id) VALUES(?,?,?,?,?,?,?,?)",
            (inv_id, _s(it.get("name")), _f(it.get("qty")), _s(it.get("unit")) or None,
             _f(it.get("unit_cost")), money.normalize(it.get("total")), vi_id, cat_id),
        )
    _recompute_last_price(database, old_vis - new_vis, loc)   # only the orphaned SKUs
    database.commit()
    return jsonify({"id": inv_id,
                    "reconciliation": _reconcile(d.get("subtotal"), d.get("tax"),
                                                 d.get("total"), items)})


# --- inventory --------------------------------------------------------------

@app.get("/api/inventory")
def inventory_list():
    rows = db.get_db().execute(
        "SELECT * FROM inventory_items WHERE archived=0 AND location_id IS ? "
        "ORDER BY category, sort_order, name", (db.active_location_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


def _revive_archived_item(database, loc, name, field_updates):
    """If a SOFT-DELETED (archived) product with this case-insensitive name already
    exists in the store, revive it — un-archive and apply the new fields — and
    return its id; else None. The uq_inv_loc_name index spans archived rows, so
    without this a deleted-then-recreated name would hit a misleading 'already
    exists' 400 for a product hidden everywhere in the UI (no unarchive path)."""
    row = database.execute(
        "SELECT id FROM inventory_items WHERE location_id IS ? AND name = ? COLLATE NOCASE "
        "AND archived=1", (loc, name)).fetchone()
    if not row:
        return None
    fu = {**field_updates, "archived": 0}
    sets = ",".join(f"{k}=?" for k in fu)
    database.execute(f"UPDATE inventory_items SET {sets} WHERE id=?",
                     list(fu.values()) + [row["id"]])
    return row["id"]


def _reclaim_archived_name(database, loc, name, keep_id):
    """Hard-delete a SOFT-DELETED (archived) product holding `name` in this store
    (other than keep_id), so a LIVE product can be RENAMED to it. The archived row
    is invisible with no unarchive path, so otherwise the rename would 400 on the
    unique index with no recovery. Child refs survive via the original ON DELETE
    SET NULL FKs (invoice_items.inventory_item_id / recipe_items.product_id /
    count_lines.item_id). Returns True if it removed one."""
    if not name:
        return False
    cur = database.execute(
        "DELETE FROM inventory_items WHERE location_id IS ? AND name = ? COLLATE NOCASE "
        "AND archived=1 AND id <> ?", (loc, name, keep_id))
    return cur.rowcount > 0


@app.post("/api/inventory")
def inventory_create():
    d = body()
    name = _s(d.get("name"))
    if not name:   # name is NOT NULL; reject a blank like every sibling create does
        return jsonify({"error": "Name is required."}), 400
    database, loc = db.get_db(), db.active_location_id()
    fields = {"name": name, "category": _s(d.get("category")) or "other", "unit": _s(d.get("unit")),
              "par_level": _f(d.get("par_level"), 0), "last_count": _f(d.get("last_count"), 0),
              "unit_cost": _f(d.get("unit_cost"), 0), "vendor": _s(d.get("vendor")),
              "sort_order": _i(d.get("sort_order"), 0)}
    revived = _revive_archived_item(database, loc, name, fields)
    if revived is not None:
        database.commit()
        return jsonify({"id": revived})
    try:
        cur = database.execute(
            "INSERT INTO inventory_items(location_id, name, category, unit, par_level, last_count, "
            "unit_cost, vendor, sort_order) VALUES(?,?,?,?,?,?,?,?,?)",
            (loc, fields["name"], fields["category"], fields["unit"], fields["par_level"],
             fields["last_count"], fields["unit_cost"], fields["vendor"], fields["sort_order"]),
        )
    except sqlite3.IntegrityError:   # UNIQUE(location_id, name) on a LIVE row — clean 400
        return jsonify({"error": "That name already exists in this store."}), 400
    database.commit()
    return jsonify({"id": cur.lastrowid})


@app.put("/api/inventory/<int:item_id>")
def inventory_update(item_id):
    d = body()
    if "name" in d and not _s(d["name"]):   # name is NOT NULL — 400, not a 500 on the constraint
        return jsonify({"error": "Name is required."}), 400
    fields = ["name", "category", "unit", "par_level", "last_count", "unit_cost",
              "vendor", "sort_order", "archived"]
    sets, vals = [], []
    for key in fields:
        if key in d:
            sets.append(f"{key}=?")
            vals.append(_coerce_col(key, d[key]))
    if not sets:
        return jsonify({"ok": True})
    loc = db.active_location_id()
    # Renaming to a name held by a SOFT-DELETED product: reclaim it (it's invisible
    # with no unarchive path) so the rename succeeds instead of a misleading 400.
    if "name" in d:
        _reclaim_archived_name(db.get_db(), loc, _s(d["name"]), item_id)
    vals += [item_id, loc]
    try:
        cur = db.get_db().execute(
            f"UPDATE inventory_items SET {','.join(sets)} WHERE id=? AND location_id IS ?", vals)
    except sqlite3.IntegrityError:   # renamed to a name held by a LIVE product
        return jsonify({"error": "That name already exists in this store."}), 400
    if cur.rowcount == 0:
        abort(404, description="Not found.")
    db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/inventory/<int:item_id>")
def inventory_delete(item_id):
    cur = db.get_db().execute(
        "UPDATE inventory_items SET archived=1 WHERE id=? AND location_id IS ?",
        (item_id, db.active_location_id()))
    if cur.rowcount == 0:
        abort(404, description="Not found.")
    db.get_db().commit()
    return jsonify({"ok": True})


@app.get("/api/inventory/order-list")
def order_list():
    """Items at or below par, with how many units to bring back up to par."""
    rows = db.get_db().execute(
        # Match reports.order_guide's predicate (strict below-par, par>0) so the two
        # reorder surfaces agree — no at-par $0 rows here that the guide omits.
        "SELECT * FROM inventory_items WHERE archived=0 AND location_id IS ? "
        "AND par_level > 0 AND COALESCE(last_count,0) < par_level "
        "ORDER BY category, name", (db.active_location_id(),)
    ).fetchall()
    out = []
    for r in rows:
        need = max((r["par_level"] or 0) - (r["last_count"] or 0), 0)
        out.append({**dict(r), "order_qty": round(need, 2),
                    "order_cost": round(need * (r["unit_cost"] or 0), 2)})
    return jsonify(out)


@app.get("/api/inventory/order-guide")
def order_guide():
    """Below-par products grouped by vendor (one order per distributor)."""
    return jsonify(reports.order_guide())


@app.post("/api/counts")
def count_save():
    """Record a walk-around count. lines: [{item_id, qty}]. Updates last_count
    and snapshots the total inventory $ value for usage-based COGS."""
    d = body()
    lines = d.get("lines")
    lines = lines if isinstance(lines, list) else []
    database = db.get_db()
    loc = db.active_location_id()
    # Stamp the count on the BUSINESS day (5am ET), not UTC — usage-COGS brackets
    # counts by date, and a UTC default would put a late-evening-ET count on the
    # wrong calendar day vs. how the rest of the app dates things.
    taken_at = square_client.business_today().isoformat() + " 12:00:00"
    cur = database.execute(
        "INSERT INTO counts(location_id, note, value, taken_at) VALUES(?, ?, 0, ?)",
        (loc, _s(d.get("note")), taken_at),
    )
    count_id = cur.lastrowid
    # Collapse to one entry per item_id (last qty wins) before summing — a client
    # that submits the same item twice would otherwise double-count it in the $
    # snapshot (counts.value) while last_count keeps only the final qty, leaving the
    # stored inventory value inconsistent with the line set (it feeds usage-COGS).
    deduped = {}
    for ln in lines:
        if isinstance(ln, dict) and _i(ln.get("item_id")) is not None:
            deduped[_i(ln.get("item_id"))] = ln
    total_value = 0.0
    for item_id, ln in deduped.items():
        item = database.execute(
            "SELECT unit_cost FROM inventory_items WHERE id=? AND location_id IS ?",
            (item_id, loc),
        ).fetchone()
        if not item:
            continue  # ignore items that don't belong to the active store
        unit_cost = item["unit_cost"] or 0
        # Clamp negatives: a count is a physical on-hand quantity (>=0). A negative
        # would deflate counts.value, the begin/end bracket that feeds usage-COGS.
        qty = max(_f(ln.get("qty"), 0) or 0, 0.0)
        total_value += qty * unit_cost
        database.execute(
            "INSERT INTO count_lines(count_id, item_id, qty, unit_cost) VALUES(?,?,?,?)",
            (count_id, item_id, qty, unit_cost),
        )
        database.execute(
            "UPDATE inventory_items SET last_count=? WHERE id=? AND location_id IS ?",
            (qty, item_id, loc),
        )
    value = money.normalize(total_value)
    database.execute("UPDATE counts SET value=? WHERE id=?", (value, count_id))
    database.commit()
    return jsonify({"id": count_id, "value": value})


@app.get("/api/counts")
def count_list():
    rows = db.get_db().execute(
        "SELECT id, taken_at, note, value FROM counts WHERE location_id IS ? "
        "ORDER BY taken_at DESC LIMIT 100", (db.active_location_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# --- vendors ----------------------------------------------------------------
# Invoices and inventory items reference a vendor by name (free text), so spend
# and links are matched case-insensitively on the vendor's name.

def _period_bounds(today=None):
    import datetime as _dt
    today = today or square_client.business_today()
    month_start = today.replace(day=1)
    last_month_end = month_start - _dt.timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    year_start = today.replace(month=1, day=1)
    return {
        "today": today.isoformat(),
        "month_start": month_start.isoformat(),
        "last_month_start": last_month_start.isoformat(),
        "last_month_end": last_month_end.isoformat(),
        "year_start": year_start.isoformat(),
    }


@app.get("/api/vendors")
def vendor_list():
    p = _period_bounds()
    rows = db.get_db().execute(
        "SELECT v.*, "
        "  (SELECT COUNT(*) FROM vendor_items vi "
        "     WHERE vi.archived=0 AND vi.location_id IS v.location_id "
        "       AND lower(COALESCE(vi.vendor_name,'')) = lower(v.name)) AS item_count, "
        "  COALESCE(s.total,0) AS spend, COALESCE(s.cnt,0) AS invoice_count, s.last_date AS last_order, "
        "  COALESCE(s.tp_total,0) AS period_purchases, COALESCE(s.tp_cnt,0) AS period_invoices, "
        "  COALESCE(s.lp_total,0) AS last_period_purchases, COALESCE(s.lp_cnt,0) AS last_period_invoices, "
        "  COALESCE(s.yr_total,0) AS year_purchases, COALESCE(s.yr_cnt,0) AS year_invoices "
        "FROM vendors v "
        "LEFT JOIN ("
        "  SELECT lower(vendor) AS vn, SUM(total) AS total, COUNT(*) AS cnt, MAX(invoice_date) AS last_date, "
        "    SUM(CASE WHEN invoice_date >= :ms THEN total ELSE 0 END) AS tp_total, "
        "    SUM(CASE WHEN invoice_date >= :ms THEN 1 ELSE 0 END) AS tp_cnt, "
        "    SUM(CASE WHEN invoice_date >= :lms AND invoice_date <= :lme THEN total ELSE 0 END) AS lp_total, "
        "    SUM(CASE WHEN invoice_date >= :lms AND invoice_date <= :lme THEN 1 ELSE 0 END) AS lp_cnt, "
        "    SUM(CASE WHEN invoice_date >= :ys THEN total ELSE 0 END) AS yr_total, "
        "    SUM(CASE WHEN invoice_date >= :ys THEN 1 ELSE 0 END) AS yr_cnt "
        "  FROM invoices WHERE location_id IS :loc GROUP BY lower(vendor)"
        ") s ON s.vn = lower(v.name) "
        "WHERE v.archived = 0 AND v.location_id IS :loc ORDER BY v.name COLLATE NOCASE",
        {"ms": p["month_start"], "lms": p["last_month_start"],
         "lme": p["last_month_end"], "ys": p["year_start"], "loc": db.active_location_id()},
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/vendors/summary")
def vendor_summary():
    d = db.get_db()
    loc = db.active_location_id()
    return jsonify({
        "total_vendors": d.execute("SELECT COUNT(*) c FROM vendors WHERE archived=0 AND location_id IS ?", (loc,)).fetchone()["c"],
        "vendor_items": d.execute("SELECT COUNT(*) c FROM vendor_items WHERE archived=0 AND location_id IS ?", (loc,)).fetchone()["c"],
        "invoices_processed": d.execute("SELECT COUNT(*) c FROM invoices WHERE location_id IS ?", (loc,)).fetchone()["c"],
        "total_purchased": round(d.execute("SELECT COALESCE(SUM(total),0) t FROM invoices WHERE location_id IS ?", (loc,)).fetchone()["t"], 2),
    })


@app.post("/api/vendors")
def vendor_create():
    d = body()
    name = _s(d.get("name"))
    if not name:
        return jsonify({"error": "Vendor needs a name."}), 400
    cur = db.get_db().execute(
        "INSERT INTO vendors(location_id, name, contact_name, phone, email, account_number, "
        "order_days, notes) VALUES(?,?,?,?,?,?,?,?)",
        (db.active_location_id(), name, _s(d.get("contact_name")), _s(d.get("phone")),
         _s(d.get("email")), _s(d.get("account_number")), _s(d.get("order_days")), _s(d.get("notes"))),
    )
    db.get_db().commit()
    return jsonify({"id": cur.lastrowid})


@app.get("/api/vendors/<int:vid>")
def vendor_get(vid):
    db_ = db.get_db()
    v = db_.execute("SELECT * FROM vendors WHERE id=? AND location_id IS ?",
                    (vid, db.active_location_id())).fetchone()
    if not v:
        abort(404, description="Not found.")
    out = dict(v)
    invoices = db_.execute(
        "SELECT id, invoice_date, invoice_number, category, total FROM invoices "
        "WHERE location_id IS ? AND lower(vendor) = lower(?) ORDER BY invoice_date DESC, id DESC LIMIT 50",
        (v["location_id"], v["name"]),
    ).fetchall()
    items = db_.execute(
        "SELECT id, name, category, unit, par_level, last_count, unit_cost FROM inventory_items "
        "WHERE archived = 0 AND location_id IS ? AND lower(vendor) = lower(?) ORDER BY name COLLATE NOCASE",
        (v["location_id"], v["name"]),
    ).fetchall()
    out["invoices"] = [dict(r) for r in invoices]
    out["items"] = [dict(r) for r in items]
    # Total spend over ALL invoices, not just the 50 shown — summing the capped
    # `invoices` list would understate the headline and disagree with the vendor list.
    out["spend"] = round(db_.execute(
        "SELECT COALESCE(SUM(total),0) AS t FROM invoices "
        "WHERE location_id IS ? AND lower(vendor) = lower(?)",
        (v["location_id"], v["name"]),
    ).fetchone()["t"], 2)
    return jsonify(out)


@app.put("/api/vendors/<int:vid>")
def vendor_update(vid):
    d = body()
    if "name" in d and not _s(d["name"]):   # name is NOT NULL — 400, not a 500 on the constraint
        return jsonify({"error": "Name is required."}), 400
    fields = ["name", "contact_name", "phone", "email", "account_number",
              "order_days", "notes", "archived"]
    sets, vals = [], []
    for key in fields:
        if key in d:
            sets.append(f"{key}=?")
            vals.append(_coerce_col(key, d[key]))
    if not sets:
        return jsonify({"ok": True})
    vals += [vid, db.active_location_id()]
    cur = db.get_db().execute(
        f"UPDATE vendors SET {','.join(sets)} WHERE id=? AND location_id IS ?", vals)
    if cur.rowcount == 0:
        abort(404, description="Not found.")
    db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/vendors/<int:vid>")
def vendor_delete(vid):
    cur = db.get_db().execute(
        "UPDATE vendors SET archived=1 WHERE id=? AND location_id IS ?",
        (vid, db.active_location_id()))
    if cur.rowcount == 0:
        abort(404, description="Not found.")
    db.get_db().commit()
    return jsonify({"ok": True})


# --- categories -------------------------------------------------------------

@app.get("/api/categories")
def category_list():
    rows = db.get_db().execute(
        "SELECT id, name, category_type, sort_order, archived FROM categories "
        "WHERE archived=0 ORDER BY sort_order"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/categories")
def category_create():
    d = body()
    name = _s(d.get("name"))
    ctype = _s(d.get("category_type"))
    if not name or not ctype:
        return jsonify({"error": "Category needs a name and a type."}), 400
    if ctype not in reports.CATEGORY_TYPES:
        return jsonify({"error": "Unknown category type."}), 400
    try:
        cur = db.get_db().execute(
            "INSERT INTO categories(name, category_type, sort_order) VALUES(?,?,?)",
            (name, ctype, _i(d.get("sort_order"), 999)),
        )
    except Exception:
        return jsonify({"error": "That category already exists."}), 400
    db.get_db().commit()
    return jsonify({"id": cur.lastrowid})


@app.put("/api/categories/<int:cid>")
def category_update(cid):
    d = body()
    # Guard the NOT NULL / taxonomy columns up front so a malformed value yields a
    # precise 400 instead of falling through to the broad except below, which would
    # mislabel a NOT NULL violation as "That category already exists."
    if "name" in d and not _s(d["name"]):
        return jsonify({"error": "Category needs a name."}), 400
    if "category_type" in d:
        ct = _s(d["category_type"])
        if not ct or ct not in reports.CATEGORY_TYPES:
            return jsonify({"error": "Unknown category type."}), 400
    sets, vals = [], []
    for key in ("name", "category_type", "sort_order", "archived"):
        if key in d:
            sets.append(f"{key}=?"); vals.append(_coerce_col(key, d[key]))
    if sets:
        vals.append(cid)
        try:
            cur = db.get_db().execute(f"UPDATE categories SET {','.join(sets)} WHERE id=?", vals)
        except Exception:
            return jsonify({"error": "That category already exists."}), 400   # UNIQUE(name)
        if cur.rowcount == 0:   # unknown/archived id -> 404, like every sibling update
            abort(404, description="Not found.")
        db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/categories/<int:cid>")
def category_delete(cid):
    cur = db.get_db().execute("UPDATE categories SET archived=1 WHERE id=?", (cid,))
    if cur.rowcount == 0:   # match every other delete: a missing id is a 404, not a silent 200
        abort(404, description="Not found.")
    db.get_db().commit()
    return jsonify({"ok": True})


# --- products ---------------------------------------------------------------
# "Products" are stored in inventory_items (shared with the Stock/Count screen).

_PRODUCT_COLS = ("name", "category", "category_id", "unit", "report_by_unit",
                 "accounting_code", "on_inventory", "tax_exempt", "par_level",
                 "last_count", "unit_cost", "vendor", "sort_order", "archived",
                 "size_qty", "size_unit")


@app.get("/api/products")
def product_list():
    where = ["p.archived = 0", "p.location_id IS ?"]
    params = [db.active_location_id()]
    if request.args.get("category_type"):
        where.append("c.category_type = ?"); params.append(request.args["category_type"])
    if request.args.get("category"):
        where.append("c.name = ?"); params.append(request.args["category"])
    if request.args.get("q"):
        where.append("p.name LIKE ?"); params.append(f"%{request.args['q']}%")
    rows = db.get_db().execute(
        "SELECT p.*, c.name AS category_name, c.category_type "
        "FROM inventory_items p LEFT JOIN categories c ON c.id = p.category_id "
        f"WHERE {' AND '.join(where)} ORDER BY p.name COLLATE NOCASE",
        params,
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/products")
def product_create():
    d = body()
    name = _s(d.get("name"))
    if not name:
        return jsonify({"error": "Product needs a name."}), 400
    cols = [k for k in _PRODUCT_COLS if k in d]
    if "name" not in cols:
        cols.append("name")
    cols.append("location_id")
    database, loc = db.get_db(), db.active_location_id()
    vals = [name if k == "name" else (loc if k == "location_id"
                                      else _coerce_col(k, d.get(k))) for k in cols]
    # Revive a soft-deleted same-name product instead of erroring on the index.
    revived = _revive_archived_item(database, loc, name,
                                    {k: v for k, v in zip(cols, vals) if k != "location_id"})
    if revived is not None:
        database.commit()
        return jsonify({"id": revived})
    placeholders = ",".join("?" * len(cols))
    try:
        cur = database.execute(
            f"INSERT INTO inventory_items({','.join(cols)}) VALUES({placeholders})", vals
        )
    except sqlite3.IntegrityError:   # UNIQUE(location_id, name) on a LIVE row — clean 400
        return jsonify({"error": "That name already exists in this store."}), 400
    database.commit()
    return jsonify({"id": cur.lastrowid})


@app.get("/api/products/purchase-report")
def product_purchase_report():
    start, end = cogs.parse_range(request.args.get("start"), request.args.get("end"))
    rows = db.get_db().execute(
        "SELECT ii.name AS product, c.category_type, c.name AS category, "
        "       MIN(ii.unit) AS report_by, COALESCE(SUM(ii.qty),0) AS units, "
        "       COALESCE(SUM(ii.total),0) AS spend, "
        # one product bought in different units (case vs btl) makes SUM(qty) meaningless;
        # count distinct units so the UI can flag "(mixed)" instead of a bogus total.
        "       COUNT(DISTINCT NULLIF(TRIM(COALESCE(ii.unit,'')),'')) AS unit_count "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN categories c ON c.id = ii.category_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ? "
        "  AND ii.name IS NOT NULL AND TRIM(ii.name) <> '' "
        "GROUP BY ii.name, ii.category_id ORDER BY spend DESC",
        (db.active_location_id(), start.isoformat(), end.isoformat()),
    ).fetchall()
    out = [{**dict(r), "units": round(r["units"], 2), "spend": round(r["spend"], 2),
            "mixed_units": (r["unit_count"] or 0) > 1}
           for r in rows]
    return jsonify({"rows": out,
                    "period": {"start": start.isoformat(), "end": end.isoformat()}})


@app.get("/api/products/new-items")
def product_new_items():
    loc = db.active_location_id()
    rows = db.get_db().execute(
        "SELECT vi.*, c.name AS category_name, c.category_type, p.name AS product_name "
        "FROM vendor_items vi LEFT JOIN categories c ON c.id = vi.category_id "
        # scope the product join to this store so a stray cross-store product_id
        # can't surface another store's product name
        "LEFT JOIN inventory_items p ON p.id = vi.product_id AND p.location_id IS ? "
        "WHERE vi.archived=0 AND vi.status='new' AND vi.location_id IS ? "
        "ORDER BY vi.created_at DESC, vi.id DESC", (loc, loc)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/products/new-items/<int:vid>/accept")
def product_new_item_accept(vid):
    """Mark a new vendor item reviewed; optionally set its category/product."""
    d = body()
    sets, vals = ["status='reviewed'"], []
    if "category_id" in d:
        sets.append("category_id=?"); vals.append(_valid_id("categories", d.get("category_id")))
    if "product_id" in d:
        sets.append("product_id=?"); vals.append(_own_product_id(db.get_db(), d.get("product_id")))
    vals += [vid, db.active_location_id()]
    cur = db.get_db().execute(
        f"UPDATE vendor_items SET {','.join(sets)} WHERE id=? AND location_id IS ?", vals)
    if cur.rowcount == 0:
        abort(404, description="Not found.")
    db.get_db().commit()
    return jsonify({"ok": True})


@app.get("/api/products/<int:pid>")
def product_get(pid):
    d = db.get_db()
    row = d.execute(
        "SELECT p.*, c.name AS category_name, c.category_type "
        "FROM inventory_items p LEFT JOIN categories c ON c.id = p.category_id "
        "WHERE p.id=? AND p.location_id IS ?", (pid, db.active_location_id()),
    ).fetchone()
    if not row:
        abort(404, description="Not found.")
    out = dict(row)
    out["purchase_history"] = [dict(r) for r in d.execute(
        "SELECT inv.invoice_date, inv.vendor, ii.qty, ii.unit, ii.unit_cost, ii.total "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "WHERE inv.location_id IS ? AND (ii.inventory_item_id=? OR lower(ii.name)=lower(?)) "
        "ORDER BY inv.invoice_date DESC, ii.id DESC LIMIT 50",
        (row["location_id"], pid, row["name"]),
    ).fetchall()]
    return jsonify(out)


@app.put("/api/products/<int:pid>")
def product_update(pid):
    d = body()
    if "name" in d and not _s(d["name"]):   # name is NOT NULL — 400, not a 500 on the constraint
        return jsonify({"error": "Name is required."}), 400
    sets, vals = [], []
    for key in _PRODUCT_COLS:
        if key in d:
            sets.append(f"{key}=?"); vals.append(_coerce_col(key, d[key]))
    if sets:
        loc = db.active_location_id()
        # Reclaim a soft-deleted same-name product so a rename to it succeeds (see
        # _reclaim_archived_name) instead of a misleading 'already exists' 400.
        if "name" in d:
            _reclaim_archived_name(db.get_db(), loc, _s(d["name"]), pid)
        vals += [pid, loc]
        try:
            cur = db.get_db().execute(
                f"UPDATE inventory_items SET {','.join(sets)} WHERE id=? AND location_id IS ?", vals)
        except sqlite3.IntegrityError:   # renamed to a name held by a LIVE product
            return jsonify({"error": "That name already exists in this store."}), 400
        if cur.rowcount == 0:
            abort(404, description="Not found.")
        db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/products/<int:pid>")
def product_delete(pid):
    cur = db.get_db().execute(
        "UPDATE inventory_items SET archived=1 WHERE id=? AND location_id IS ?",
        (pid, db.active_location_id()))
    if cur.rowcount == 0:
        abort(404, description="Not found.")
    db.get_db().commit()
    return jsonify({"ok": True})


# --- vendor items -----------------------------------------------------------

@app.get("/api/vendor-items")
def vendor_item_list():
    loc = db.active_location_id()
    where = ["vi.archived = 0", "vi.location_id IS ?"]
    params = [loc]
    if request.args.get("status"):
        where.append("vi.status = ?"); params.append(request.args["status"])
    if request.args.get("category"):
        where.append("c.name = ?"); params.append(request.args["category"])
    if request.args.get("vendor"):
        where.append("lower(vi.vendor_name) = lower(?)"); params.append(request.args["vendor"])
    if request.args.get("q"):
        where.append("(vi.vendor_item_name LIKE ? OR vi.item_code LIKE ?)")
        params += [f"%{request.args['q']}%"] * 2
    rows = db.get_db().execute(
        "SELECT vi.*, c.name AS category_name, c.category_type, p.name AS product_name "
        "FROM vendor_items vi LEFT JOIN categories c ON c.id = vi.category_id "
        # scope the product join to this store (the join param precedes the WHERE params)
        "LEFT JOIN inventory_items p ON p.id = vi.product_id AND p.location_id IS ? "
        f"WHERE {' AND '.join(where)} ORDER BY vi.vendor_item_name COLLATE NOCASE LIMIT 5000",
        [loc] + params,
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/vendor-items")
def vendor_item_create():
    d = body()
    name = _s(d.get("vendor_item_name"))
    if not name:
        return jsonify({"error": "Vendor item needs a name."}), 400
    cur = db.get_db().execute(
        "INSERT INTO vendor_items(location_id, vendor_id, vendor_name, vendor_item_name, product_id, "
        "category_id, item_code, order_guide, status) VALUES(?,?,?,?,?,?,?,?, 'reviewed')",
        (db.active_location_id(), _valid_id("vendors", d.get("vendor_id"), loc_scoped=True),
         _s(d.get("vendor_name")), name,
         _own_product_id(db.get_db(), d.get("product_id")), _valid_id("categories", d.get("category_id")),
         _s(d.get("item_code")), _i(d.get("order_guide"), 0)),
    )
    db.get_db().commit()
    return jsonify({"id": cur.lastrowid})


@app.put("/api/vendor-items/<int:vid>")
def vendor_item_update(vid):
    d = body()
    sets, vals = [], []
    for key in ("vendor_name", "vendor_item_name", "product_id", "category_id",
                "item_code", "order_guide", "status", "archived"):
        if key in d:
            # a product_id must belong to the active store (else drop to NULL)
            val = _own_product_id(db.get_db(), d[key]) if key == "product_id" else _coerce_col(key, d[key])
            sets.append(f"{key}=?"); vals.append(val)
    if sets:
        vals += [vid, db.active_location_id()]
        cur = db.get_db().execute(
            f"UPDATE vendor_items SET {','.join(sets)} WHERE id=? AND location_id IS ?", vals)
        if cur.rowcount == 0:
            abort(404, description="Not found.")
        db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/vendor-items/<int:vid>")
def vendor_item_delete(vid):
    cur = db.get_db().execute(
        "UPDATE vendor_items SET archived=1 WHERE id=? AND location_id IS ?",
        (vid, db.active_location_id()))
    if cur.rowcount == 0:
        abort(404, description="Not found.")
    db.get_db().commit()
    return jsonify({"ok": True})


# --- sales mix (per reporting period) ---------------------------------------

@app.get("/api/sales-mix")
def sales_mix_get():
    start, end = cogs.parse_range(request.args.get("start"), request.args.get("end"))
    rows = db.get_db().execute(
        "SELECT category_type, pct FROM sales_mix "
        "WHERE location_id=? AND period_start=? AND period_end=?",
        (db.active_location_id(), start.isoformat(), end.isoformat()),
    ).fetchall()
    mix = {r["category_type"]: r["pct"] for r in rows}
    return jsonify({
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "category_types": reports.CATEGORY_TYPES,
        "mix": {t: mix.get(t, 0) for t in reports.CATEGORY_TYPES},
    })


@app.put("/api/sales-mix")
def sales_mix_put():
    start, end = cogs.parse_range(request.args.get("start"), request.args.get("end"))
    mix = body().get("mix") or {}
    if not isinstance(mix, dict):
        abort(400, description="mix must be an object of category_type -> percent.")
    # Reject a non-numeric/non-finite percent rather than silently coercing it to
    # 0 (which would slip past the 100% total guard and store a wrong 0% split).
    # Also bound each type to [0,100]: e.g. {Food:150, Beer:-50} sums to 100 but
    # makes per-type income negative / over-100% even though the grand total ties.
    for t in reports.CATEGORY_TYPES:
        if t in mix:
            v = _f(mix[t])
            if v is None:
                abort(400, description=f"{t} percent must be a number.")
            if not (0 <= v <= 100):
                abort(400, description=f"{t} percent must be between 0 and 100.")
    # A mix that doesn't total ~100% would make Income (sales x mix%) not reconcile
    # to total sales and skew every per-type COGS%. Allow all-zero (clearing).
    total = round(sum(_f(mix[t], 0) or 0 for t in reports.CATEGORY_TYPES if t in mix), 2)
    if total and abs(total - 100) > 0.5:
        abort(400, description=f"Sales mix must total 100% (got {total}%).")
    database = db.get_db()
    loc = db.active_location_id()
    for ctype in reports.CATEGORY_TYPES:
        if ctype in mix:
            database.execute(
                "INSERT INTO sales_mix(location_id, period_start, period_end, category_type, pct) "
                "VALUES(?,?,?,?,?) ON CONFLICT(location_id,period_start,period_end,category_type) "
                "DO UPDATE SET pct=excluded.pct",
                (loc, start.isoformat(), end.isoformat(), ctype, _f(mix[ctype], 0)),
            )
    database.commit()
    return jsonify({"ok": True})


# --- performance reports ----------------------------------------------------

@app.get("/api/reports/category")
def report_category():
    start, end = cogs.parse_range(request.args.get("start"), request.args.get("end"))
    return jsonify(reports.category_report(
        start, end,
        vendor=request.args.get("vendor"),
        status=request.args.get("status"),
        search=request.args.get("q"),
    ))


@app.get("/api/reports/controllable-pl")
def report_controllable_pl():
    start, end = cogs.parse_range(request.args.get("start"), request.args.get("end"))
    return jsonify(reports.controllable_pl(start, end))


@app.get("/api/reports/sales")
def report_sales():
    return jsonify(reports.sales_report())


@app.get("/api/reports/price-movers")
def report_price_movers():
    start, end = cogs.parse_range(request.args.get("start"), request.args.get("end"))
    return jsonify(reports.price_movers(start, end))


def _csv_response(text, filename):
    return Response(
        text, mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export/purchases.csv")
def export_purchases():
    start, end = cogs.parse_range(request.args.get("start"), request.args.get("end"))
    return _csv_response(exports.purchases_csv(start, end),
                         f"purchases_{start}_{end}.csv")


@app.get("/api/export/category-summary.csv")
def export_category_summary():
    start, end = cogs.parse_range(request.args.get("start"), request.args.get("end"))
    return _csv_response(exports.category_summary_csv(start, end),
                         f"category-summary_{start}_{end}.csv")


@app.get("/api/export/order-guide.csv")
def export_order_guide():
    return _csv_response(exports.order_guide_csv(), "order-guide.csv")


@app.get("/api/export/recipes.csv")
def export_recipes():
    return _csv_response(exports.recipes_csv(), "recipe-costing.csv")


# --- recipes / plate costing ------------------------------------------------

def _save_recipe_items(database, rid, items):
    loc = db.active_location_id()
    for it in (items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        pid = _i(it.get("product_id"))
        # Never let a recipe reference another store's product (the API accepts
        # any id; the editor only offers active-store ones). Foreign -> drop to
        # an unlinked line rather than cost against a different store's price.
        if pid is not None and not database.execute(
            "SELECT 1 FROM inventory_items WHERE id=? AND location_id IS ?", (pid, loc)
        ).fetchone():
            pid = None
        database.execute(
            "INSERT INTO recipe_items(recipe_id, product_id, qty, unit, note) VALUES(?,?,?,?,?)",
            (rid, pid, _f(it.get("qty"), 0) or 0,
             _s(it.get("unit")), _s(it.get("note"))),
        )


@app.get("/api/recipes")
def recipe_list():
    return jsonify(recipes.list_costed())


@app.post("/api/recipes")
def recipe_create():
    d = body()
    if not _s(d.get("name")):   # name is NOT NULL; reject a blank like every sibling create
        return jsonify({"error": "Recipe needs a name."}), 400
    database = db.get_db()
    cur = database.execute(
        "INSERT INTO recipes(location_id, name, menu_price, yield_qty, notes) "
        "VALUES(?,?,?,?,?)",
        (db.active_location_id(), _s(d.get("name")),
         money.normalize(d.get("menu_price")) or 0, _f(d.get("yield_qty"), 1) or 1,
         _s(d.get("notes"))),
    )
    rid = cur.lastrowid
    _save_recipe_items(database, rid, d.get("items"))
    database.commit()
    return jsonify(recipes.cost(rid))


@app.get("/api/recipes/<int:rid>")
def recipe_get(rid):
    r = recipes.cost(rid)          # location-scoped; None when foreign/missing
    if r is None:
        abort(404, description="Not found.")
    return jsonify(r)


@app.put("/api/recipes/<int:rid>")
def recipe_update(rid):
    d = body()
    if not _s(d.get("name")):   # name is NOT NULL; reject a blank like every sibling update
        return jsonify({"error": "Recipe needs a name."}), 400
    database = db.get_db()
    loc = db.active_location_id()
    if not database.execute(
        "SELECT 1 FROM recipes WHERE id=? AND location_id IS ?", (rid, loc)
    ).fetchone():
        abort(404, description="Not found.")
    database.execute(
        "UPDATE recipes SET name=?, menu_price=?, yield_qty=?, notes=? "
        "WHERE id=? AND location_id IS ?",
        (_s(d.get("name")), money.normalize(d.get("menu_price")) or 0,
         _f(d.get("yield_qty"), 1) or 1, _s(d.get("notes")), rid, loc),
    )
    database.execute("DELETE FROM recipe_items WHERE recipe_id=?", (rid,))
    _save_recipe_items(database, rid, d.get("items"))
    database.commit()
    return jsonify(recipes.cost(rid))


@app.delete("/api/recipes/<int:rid>")
def recipe_delete(rid):
    database = db.get_db()
    cur = database.execute(
        "DELETE FROM recipes WHERE id=? AND location_id IS ?",
        (rid, db.active_location_id()),
    )
    if cur.rowcount == 0:
        abort(404, description="Not found.")
    database.commit()
    return jsonify({"ok": True})


@app.get("/api/alerts/price-increases")
def alerts_price_increases():
    """Proactive: vendor items whose latest price recently jumped >= the
    configured threshold. Surfaced on the dashboard so the owner sees a silent
    price hike without opening the Price Movers report."""
    try:
        min_pct = float(db.get_setting("price_alert_pct") or 10)
    except (TypeError, ValueError):
        min_pct = 10.0
    try:
        days = max(1, min(int(request.args.get("days", 30)), 3650))   # clamp: a huge value overflows date math
    except (TypeError, ValueError):
        days = 30
    return jsonify(reports.price_alerts(lookback_days=days, min_pct=min_pct))


# --- helpers ----------------------------------------------------------------

def _category_id_by_name(database, name):
    if not name:
        return None
    r = database.execute(
        "SELECT id FROM categories WHERE lower(name)=lower(?)", (name,)
    ).fetchone()
    return r["id"] if r else None


def _reconcile(subtotal, tax, total, line_items):
    """Sanity-check that the line items represent the invoice, so an AI misparse
    (a missing or mis-keyed line) is visible instead of silently skewing the
    category breakdown that feeds the reports.

    Vendors print line totals either tax-exclusive (summing to the subtotal) or
    tax-inclusive (summing to the grand total), so a match to EITHER base counts
    as reconciled. Returns {line_sum, expected, delta, ok}; ok is None when
    there's nothing to compare (no amounts, or a header-only invoice with no
    line items)."""
    # Work in integer cents so the line sum and the comparison are exact (float
    # summation of many line totals otherwise drifts below the penny).
    items = line_items or []
    line_c = sum(money.to_cents(li.get("total"), 0) for li in items)
    sub_c, tot_c, tx_c = money.cents_or_none(subtotal), money.cents_or_none(total), money.to_cents(tax, 0)
    targets = []
    if sub_c is not None:
        targets.append(sub_c)
    if tot_c is not None:
        targets.append(tot_c)
        if tx_c:
            targets.append(tot_c - tx_c)   # pre-tax base when lines exclude tax
    if not targets or not items:
        exp = targets[0] / 100.0 if targets else None
        return {"line_sum": line_c / 100.0, "expected": exp, "delta": None, "ok": None}
    expected_c = min(targets, key=lambda t: abs(line_c - t))   # whichever convention fits
    delta_c = line_c - expected_c
    # Tolerate sub-cent rounding: >=2c, or 0.5% of the invoice. Compared as exact
    # integers (x1000, no rounding) so this verdict is bit-identical to the JS
    # preview in reconRead — no half-even/half-up drift between the two.
    ok = abs(delta_c) * 1000 <= max(2000, abs(expected_c) * 5)
    return {"line_sum": line_c / 100.0, "expected": expected_c / 100.0,
            "delta": delta_c / 100.0, "ok": ok}


def _find_duplicate_invoice(database, loc, vendor, invoice_number, inv_date, total, exclude_id=None):
    """Return a brief {id, vendor, invoice_date, invoice_number, total} for an
    existing invoice in this location that looks like the same one, else None.
    `exclude_id` skips a specific invoice (so an EDIT doesn't match itself).

    Strong signal: same vendor + a non-empty invoice number. Fallback when there's
    no number: same vendor + date + total (within a cent)."""
    vendor = _s(vendor)
    num = _s(invoice_number)
    cols = "id, vendor, invoice_date, invoice_number, total"
    excl = " AND id <> ?" if exclude_id is not None else ""
    excl_args = [exclude_id] if exclude_id is not None else []
    if vendor and num:
        row = database.execute(
            f"SELECT {cols} FROM invoices WHERE location_id IS ? AND lower(vendor)=lower(?) "
            f"AND invoice_number <> '' AND lower(invoice_number)=lower(?){excl} ORDER BY id DESC LIMIT 1",
            (loc, vendor, num, *excl_args),
        ).fetchone()
        if row:
            return dict(row)
    if vendor and inv_date and total is not None:
        # Compare to the penny exactly (CAST avoids float fuzz from REAL storage).
        row = database.execute(
            f"SELECT {cols} FROM invoices WHERE location_id IS ? AND lower(vendor)=lower(?) "
            "AND invoice_date=? AND total IS NOT NULL "
            f"AND CAST(ROUND(total*100) AS INTEGER) = CAST(ROUND(?*100) AS INTEGER){excl} "
            "ORDER BY id DESC LIMIT 1",
            (loc, vendor, inv_date, total, *excl_args),
        ).fetchone()
        if row:
            return dict(row)
    return None


def _resolve_vendor_item(database, vendor, inv_date, line, loc):
    """Match an invoice line to a vendor_item within this location (creating a
    'new' one if unseen), refresh its last price/date, return (vi_id, cat_id)."""
    name = _s(line.get("name"))
    cat_id = _category_id_by_name(database, _s(line.get("category")) or None)
    price = _f(line.get("unit_cost"))
    if not name:
        return None, cat_id
    row = database.execute(
        # COLLATE NOCASE (not lower()) so the idx_vendoritems_loc_name index is seekable
        "SELECT id, category_id FROM vendor_items "
        "WHERE location_id IS ? AND COALESCE(vendor_name,'') = ? COLLATE NOCASE "
        "  AND vendor_item_name = ? COLLATE NOCASE",
        (loc, vendor or "", name),
    ).fetchone()
    # A credit/return line carries a non-positive unit_cost — it's not a real
    # purchase price, so it must never become the SKU's "last price" (that value
    # is shown verbatim in the UI). Let it still advance the date/category.
    price_for_store = price if (price or 0) > 0 else None
    if row:
        # Only advance the last-purchase price/date when this invoice is at least
        # as recent as what's recorded, so editing (or backfilling) an OLDER
        # invoice can't roll the latest price backwards.
        database.execute(
            "UPDATE vendor_items SET last_purchase_date=?, "
            "last_purchase_price=CASE WHEN ? IS NOT NULL THEN ? ELSE last_purchase_price END, "
            "category_id=COALESCE(category_id, ?) "
            "WHERE id=? AND (last_purchase_date IS NULL OR last_purchase_date <= ?)",
            (inv_date, price_for_store, price_for_store, cat_id, row["id"], inv_date),
        )
        return row["id"], (row["category_id"] or cat_id)
    vrow = database.execute(
        "SELECT id FROM vendors WHERE location_id IS ? AND name = ? COLLATE NOCASE",
        (loc, vendor or ""),
    ).fetchone()
    cur = database.execute(
        "INSERT INTO vendor_items(location_id, vendor_id, vendor_name, vendor_item_name, "
        "category_id, last_purchase_date, last_purchase_price, status) "
        "VALUES(?,?,?,?,?,?,?, 'new')",
        (loc, vrow["id"] if vrow else None, vendor, name, cat_id, inv_date, price_for_store),
    )
    return cur.lastrowid, cat_id


def _f(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    # Reject inf/nan ("Infinity" parses as a float) so they can't poison stored
    # qty/unit_cost and downstream sums.
    return f if math.isfinite(f) else default


def _i(v, default=None):
    # Bound to signed-64-bit: an id outside that range binds fine to int() but
    # raises OverflowError when SQLite binds it, turning the intended 400/NULL-drop
    # into a 500. Clamp it out here so every id-coercion path stays robust.
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n if -(2 ** 63) <= n < 2 ** 63 else default


def _s(v):
    """A trimmed string from request JSON. A non-string (number/list/dict) becomes
    "" rather than crashing on .strip() — name-required checks then reject it."""
    return v.strip() if isinstance(v, str) else ""


def _scalar(v):
    """True if v is safe to bind to sqlite (not a list/dict). Used to drop
    structured values from dynamic UPDATE column lists instead of 500ing."""
    return not isinstance(v, (list, dict))


# Columns whose values must be numeric — coerce on dynamic write so a string
# like "abc" can't land in a REAL/INTEGER column (SQLite won't reject it) and
# later 500 every read that does arithmetic on it.
_NUM_REAL = {"par_level", "last_count", "unit_cost", "size_qty", "menu_price",
             "yield_qty", "pct"}
_NUM_INT = {"sort_order", "on_inventory", "tax_exempt", "archived", "category_id",
            "product_id", "vendor_id", "order_guide"}
# Boolean flag columns: filters use strict equality (archived=0 / archived=1), so a
# NULL would orphan the row from BOTH the active list and every archived view —
# clamp these to 0/1, never NULL.
_BOOL_FLAGS = {"archived", "on_inventory", "tax_exempt", "order_guide"}


def _image_name(v):
    """A stored image filename reduced to its basename — never a path. Prevents a
    traversal value like '../../data/ledger.db' from escaping UPLOAD_DIR on the
    delete (os.remove) path."""
    name = os.path.basename(_s(v))
    return name or None


def _coerce_col(key, v):
    """Bind-safe value for a dynamic column: numeric columns -> _f/_i (NULL on
    junk), FK columns validated to exist/own (NULL if dangling), text columns
    dropped to None if a list/dict."""
    if key in _NUM_REAL:
        return _f(v)
    if key == "category_id":
        return _valid_id("categories", v)            # categories are shared (not loc-scoped)
    if key == "product_id":
        return _own_product_id(db.get_db(), v)
    if key == "vendor_id":
        return _valid_id("vendors", v, loc_scoped=True)
    if key in _BOOL_FLAGS:
        # Clamp to 0/1, NEVER NULL: a malformed value (_i -> None written verbatim)
        # would orphan the row — SQL NULL matches neither `archived=0` nor `archived=1`,
        # so it vanishes from the active list AND every archived view with no recovery.
        return 1 if _i(v) else 0
    if key in _NUM_INT:
        return _i(v)
    return v if _scalar(v) else None


def _recompute_last_price(database, vi_ids, loc):
    """Re-point each vendor_item's last price/date at its newest remaining
    positive-priced line (or NULL if none), so removing/editing-out a delivery
    can't strand a stale 'last price'. Scoped to `loc` so the isolation invariant
    is local to the query, not an assumption about the caller."""
    for vi_id in vi_ids:
        if vi_id is None:
            continue
        database.execute(
            "UPDATE vendor_items SET "
            "  last_purchase_date=(SELECT inv.invoice_date FROM invoice_items ii "
            "    JOIN invoices inv ON inv.id=ii.invoice_id "
            "    WHERE ii.vendor_item_id=vendor_items.id AND ii.unit_cost>0 "
            "    ORDER BY inv.invoice_date DESC, ii.id DESC LIMIT 1), "
            "  last_purchase_price=(SELECT ii.unit_cost FROM invoice_items ii "
            "    JOIN invoices inv ON inv.id=ii.invoice_id "
            "    WHERE ii.vendor_item_id=vendor_items.id AND ii.unit_cost>0 "
            "    ORDER BY inv.invoice_date DESC, ii.id DESC LIMIT 1) "
            "WHERE id=? AND location_id IS ?", (vi_id, loc))


def _own_product_id(database, v):
    """A product_id coerced to int and validated to belong to the active store,
    else None — so a vendor item can't reference (and surface the name of)
    another store's product. Mirrors the recipe write guard."""
    pid = _i(v)
    if pid is None:
        return None
    row = database.execute(
        "SELECT 1 FROM inventory_items WHERE id=? AND location_id IS ?",
        (pid, db.active_location_id()),
    ).fetchone()
    return pid if row else None


def _valid_id(table, v, loc_scoped=False):
    """An id coerced to int and validated to EXIST (and, when loc_scoped, belong
    to the active store), else None — so a dangling/foreign FK is dropped to NULL
    rather than stored or 500ing on a fresh DB's foreign-key constraint."""
    iden = _i(v)
    if iden is None:
        return None
    q = f"SELECT 1 FROM {table} WHERE id=?"
    args = [iden]
    if loc_scoped:
        q += " AND location_id IS ?"
        args.append(db.active_location_id())
    return iden if db.get_db().execute(q, args).fetchone() else None


with app.app_context():
    db.init_db()


def _backup_loop(every_hours=6):
    while True:
        time.sleep(every_hours * 3600)
        try:
            db.backup()
        except Exception as e:   # a failure must never take the server down...
            print(f"  [!] Periodic backup failed: {e}")   # ...but don't fail silently


def _start_backups():
    """Take a startup snapshot and launch the periodic backup thread. Called at
    IMPORT time (not just under `python app.py`) so the documented gunicorn deploy
    — which imports `app:app` and never hits __main__ — still gets its safety-net
    backups. Opt out in tests/tooling via LEDGER_DISABLE_BACKUPS."""
    if os.environ.get("LEDGER_DISABLE_BACKUPS"):
        return
    try:
        snap = db.backup()
        if snap:
            print(f"  Backed up to {os.path.relpath(snap, BASE_DIR)}")
    except Exception as e:
        print(f"  [!] Startup backup failed: {e}")
    threading.Thread(target=_backup_loop, daemon=True).start()


_start_backups()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8088"))
    host = os.environ.get("HOST", "0.0.0.0")
    if not os.environ.get("APP_PASSWORD"):
        if host not in ("127.0.0.1", "localhost", "::1"):
            # Fail closed: don't serve an unauthenticated app on a non-loopback
            # bind. Force loopback so "open" mode can only be reached on LAN/local.
            print("\n  [!] APP_PASSWORD is not set — forcing HOST=127.0.0.1 so the "
                  "open app isn't exposed on the network.\n      Set APP_PASSWORD to "
                  "bind elsewhere.\n")
            host = "127.0.0.1"
        else:
            print("\n  [!] APP_PASSWORD is not set — the ledger is open on localhost.\n")
    # Only enable the Werkzeug debugger (an RCE surface via its console) when
    # bound to loopback — never expose it on a public 0.0.0.0 bind.
    debug = bool(os.environ.get("DEBUG")) and host in ("127.0.0.1", "localhost", "::1")
    if os.environ.get("DEBUG") and not debug:
        print("  [!] DEBUG ignored: refusing the interactive debugger on a non-loopback bind.")
    # Startup snapshot + periodic backup thread already started at import time
    # (see _start_backups), so they run under gunicorn too — nothing to do here.
    print(f"  Barkeep's Ledger running at http://{host}:{port}\n")
    app.run(host=host, port=port, debug=debug)
