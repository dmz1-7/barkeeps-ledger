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
import os
import secrets
import threading
import time
import uuid

# Load .env before importing modules that read the environment at import time.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

from flask import (
    Flask, g, jsonify, request, send_from_directory, abort,
)

import db
import cogs
import reports
import square_client
from invoice_ai import parse_invoice, InvoiceError

BASE_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.teardown_appcontext(db.close_db)
# Cap uploads so a giant (or malicious) file can't exhaust memory/disk. 32 MB
# covers a full-resolution phone photo while still bounding abuse.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024


@app.errorhandler(413)
def _too_large(_e):
    mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify({"error": f"That file is too large (max {mb} MB)."}), 413


# --- auth -------------------------------------------------------------------
# Single shared passcode for a personal tool. Set APP_PASSWORD to enable it;
# leave it unset to run open (fine on a private LAN, noted in the README).

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
    return hmac.compare_digest(sent, expected)


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
    h = request.headers.get("X-Location-Id")
    if not h:
        return
    try:
        lid = int(h)
    except (TypeError, ValueError):
        return
    if db.get_db().execute(
            "SELECT 1 FROM locations WHERE id=? AND archived=0", (lid,)).fetchone():
        g.location_override = lid


# Throttle passcode guessing. In-memory (single-process dev server).
_LOGIN_FAILS = {}          # key -> [recent failure timestamps]
_LOGIN_MAX = 8             # per-key failures per window before lockout
_LOGIN_GLOBAL_MAX = 50     # backstop across ALL keys, immune to header spoofing
_LOGIN_WINDOW = 300        # seconds


def _login_key():
    # Trust the forwarded client IP only from the tunnel's loopback origin; on a
    # direct connection CF-Connecting-IP is attacker-controlled, so key on the
    # real socket address (which can't be spoofed per request) instead.
    addr = request.remote_addr or "?"
    if addr in ("127.0.0.1", "::1"):
        return request.headers.get("CF-Connecting-IP") or addr
    return addr


def _login_blocked(key):
    now = time.monotonic()
    total = 0
    for k in list(_LOGIN_FAILS.keys()):          # sweep: prune stale, count live
        live = [t for t in _LOGIN_FAILS[k] if now - t < _LOGIN_WINDOW]
        if live:
            _LOGIN_FAILS[k] = live
            total += len(live)
        else:
            del _LOGIN_FAILS[k]                   # don't leak keys for one-off IPs
    # Per-key lockout, plus a global cap that a rotated CF-Connecting-IP can't escape.
    return len(_LOGIN_FAILS.get(key, [])) >= _LOGIN_MAX or total >= _LOGIN_GLOBAL_MAX


@app.post("/api/login")
def login():
    expected = _expected_token()
    if expected is None:
        return jsonify({"token": "", "auth_required": False})
    key = _login_key()
    if _login_blocked(key):
        return jsonify({"error": "Too many attempts. Wait a few minutes and try again."}), 429
    pw = (request.json or {}).get("password", "")
    token = _token_for(pw)
    if hmac.compare_digest(token, expected):
        _LOGIN_FAILS.pop(key, None)   # clear on success
        return jsonify({"token": token, "auth_required": True})
    _LOGIN_FAILS.setdefault(key, []).append(time.monotonic())
    return jsonify({"error": "Wrong passcode."}), 401


# --- pages / static ---------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/uploads/<path:name>")
def uploaded(name):
    return send_from_directory(UPLOAD_DIR, name)


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


# --- config / settings ------------------------------------------------------

@app.get("/api/config")
def config():
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
    })


@app.post("/api/settings")
def save_settings():
    data = request.json or {}
    for key in ("square_env", "square_version", "ai_model",
                "target_cogs_pct", "target_labor_pct", "default_hourly_wage"):
        if key in data:
            db.set_setting(key, data[key])
    # The Square location is per-store now: write it onto the active store's row,
    # not a shared global setting.
    if "square_location_id" in data:
        db.get_db().execute("UPDATE locations SET square_location_id=? WHERE id=?",
                            (data["square_location_id"], db.active_location_id()))
        db.get_db().commit()
    # Only persist a Square token if a non-blank one is supplied (so the UI can
    # show "set" without round-tripping the secret).
    if data.get("square_token"):
        db.set_setting("square_token", data["square_token"].strip())
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
    loc_id = (request.json or {}).get("location_id")
    row = db.get_db().execute(
        "SELECT id FROM locations WHERE id=? AND archived=0", (loc_id,)
    ).fetchone()
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
    d = request.json or {}
    items = d.get("line_items") or []
    vendor = d.get("vendor", "")
    inv_date = d.get("invoice_date", "")
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
            loc, vendor, inv_date, d.get("invoice_number", ""),
            d.get("category"), _f(d.get("subtotal")), _f(d.get("tax")),
            _f(d.get("total")), d.get("image_path"), d.get("notes", ""),
            d.get("raw_json", ""), d.get("status", "closed"), d.get("payment_account"),
        ),
    )
    inv_id = cur.lastrowid
    for it in items:
        vi_id, cat_id = _resolve_vendor_item(database, vendor, inv_date, it, loc)
        database.execute(
            "INSERT INTO invoice_items(invoice_id, name, qty, unit, unit_cost, total, "
            "vendor_item_id, category_id) VALUES(?,?,?,?,?,?,?,?)",
            (inv_id, it.get("name", ""), _f(it.get("qty")), it.get("unit"),
             _f(it.get("unit_cost")), _f(it.get("total")), vi_id, cat_id),
        )
    database.commit()
    return jsonify({"id": inv_id})


@app.get("/api/invoices")
def invoice_list():
    """The Orders view. Optional filters: start, end, vendor, status, q (search)."""
    where, params = ["location_id IS ?"], [db.active_location_id()]
    if request.args.get("start"):
        where.append("invoice_date >= ?"); params.append(request.args["start"])
    if request.args.get("end"):
        where.append("invoice_date <= ?"); params.append(request.args["end"])
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
        abort(404)
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
        abort(404)
    db_.execute("DELETE FROM invoices WHERE id=? AND location_id IS ?", (inv_id, loc))
    db_.commit()
    if row["image_path"]:
        try:
            os.remove(os.path.join(UPLOAD_DIR, row["image_path"]))
        except OSError:
            pass
    return jsonify({"ok": True})


@app.put("/api/invoices/<int:inv_id>")
def invoice_update(inv_id):
    """Edit a saved invoice: update the header and replace its line items. Lets
    the owner fix an AI misparse instead of delete-and-re-enter. The image and
    the original AI audit (raw_json) are preserved."""
    d = request.json or {}
    database = db.get_db()
    loc = db.active_location_id()
    if not database.execute(
        "SELECT 1 FROM invoices WHERE id=? AND location_id IS ?", (inv_id, loc)
    ).fetchone():
        abort(404)
    vendor = d.get("vendor", "")
    inv_date = d.get("invoice_date", "")
    database.execute(
        "UPDATE invoices SET vendor=?, invoice_date=?, invoice_number=?, category=?, "
        "subtotal=?, tax=?, total=?, notes=?, status=?, payment_account=? "
        "WHERE id=? AND location_id IS ?",
        (vendor, inv_date, d.get("invoice_number", ""), d.get("category"),
         _f(d.get("subtotal")), _f(d.get("tax")), _f(d.get("total")),
         d.get("notes", ""), d.get("status", "closed"), d.get("payment_account"),
         inv_id, loc),
    )
    items = d.get("line_items") or []
    database.execute("DELETE FROM invoice_items WHERE invoice_id=?", (inv_id,))
    for it in items:
        vi_id, cat_id = _resolve_vendor_item(database, vendor, inv_date, it, loc)
        database.execute(
            "INSERT INTO invoice_items(invoice_id, name, qty, unit, unit_cost, total, "
            "vendor_item_id, category_id) VALUES(?,?,?,?,?,?,?,?)",
            (inv_id, it.get("name", ""), _f(it.get("qty")), it.get("unit"),
             _f(it.get("unit_cost")), _f(it.get("total")), vi_id, cat_id),
        )
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


@app.post("/api/inventory")
def inventory_create():
    d = request.json or {}
    cur = db.get_db().execute(
        "INSERT INTO inventory_items(location_id, name, category, unit, par_level, last_count, "
        "unit_cost, vendor, sort_order) VALUES(?,?,?,?,?,?,?,?,?)",
        (db.active_location_id(), d.get("name", "").strip(), d.get("category", "other"), d.get("unit", ""),
         _f(d.get("par_level"), 0), _f(d.get("last_count"), 0),
         _f(d.get("unit_cost"), 0), d.get("vendor", ""), _i(d.get("sort_order"), 0)),
    )
    db.get_db().commit()
    return jsonify({"id": cur.lastrowid})


@app.put("/api/inventory/<int:item_id>")
def inventory_update(item_id):
    d = request.json or {}
    fields = ["name", "category", "unit", "par_level", "last_count", "unit_cost",
              "vendor", "sort_order", "archived"]
    sets, vals = [], []
    for key in fields:
        if key in d:
            sets.append(f"{key}=?")
            vals.append(d[key])
    if not sets:
        return jsonify({"ok": True})
    vals += [item_id, db.active_location_id()]
    cur = db.get_db().execute(
        f"UPDATE inventory_items SET {','.join(sets)} WHERE id=? AND location_id IS ?", vals)
    if cur.rowcount == 0:
        abort(404)
    db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/inventory/<int:item_id>")
def inventory_delete(item_id):
    cur = db.get_db().execute(
        "UPDATE inventory_items SET archived=1 WHERE id=? AND location_id IS ?",
        (item_id, db.active_location_id()))
    if cur.rowcount == 0:
        abort(404)
    db.get_db().commit()
    return jsonify({"ok": True})


@app.get("/api/inventory/order-list")
def order_list():
    """Items at or below par, with how many units to bring back up to par."""
    rows = db.get_db().execute(
        "SELECT * FROM inventory_items WHERE archived=0 AND location_id IS ? "
        "AND last_count <= par_level ORDER BY category, name", (db.active_location_id(),)
    ).fetchall()
    out = []
    for r in rows:
        need = max((r["par_level"] or 0) - (r["last_count"] or 0), 0)
        out.append({**dict(r), "order_qty": round(need, 2),
                    "order_cost": round(need * (r["unit_cost"] or 0), 2)})
    return jsonify(out)


@app.post("/api/counts")
def count_save():
    """Record a walk-around count. lines: [{item_id, qty}]. Updates last_count
    and snapshots the total inventory $ value for usage-based COGS."""
    d = request.json or {}
    lines = d.get("lines") or []
    database = db.get_db()
    loc = db.active_location_id()
    cur = database.execute(
        "INSERT INTO counts(location_id, note, value) VALUES(?, ?, 0)",
        (loc, d.get("note", "")),
    )
    count_id = cur.lastrowid
    total_value = 0.0
    for ln in lines:
        item = database.execute(
            "SELECT unit_cost FROM inventory_items WHERE id=? AND location_id IS ?",
            (ln.get("item_id"), loc),
        ).fetchone()
        if not item:
            continue  # ignore items that don't belong to the active store
        unit_cost = item["unit_cost"] or 0
        qty = _f(ln.get("qty"), 0) or 0
        total_value += qty * unit_cost
        database.execute(
            "INSERT INTO count_lines(count_id, item_id, qty, unit_cost) VALUES(?,?,?,?)",
            (count_id, ln.get("item_id"), qty, unit_cost),
        )
        database.execute(
            "UPDATE inventory_items SET last_count=? WHERE id=? AND location_id IS ?",
            (qty, ln.get("item_id"), loc),
        )
    database.execute("UPDATE counts SET value=? WHERE id=?", (round(total_value, 2), count_id))
    database.commit()
    return jsonify({"id": count_id, "value": round(total_value, 2)})


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
    d = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Vendor needs a name."}), 400
    cur = db.get_db().execute(
        "INSERT INTO vendors(location_id, name, contact_name, phone, email, account_number, "
        "order_days, notes) VALUES(?,?,?,?,?,?,?,?)",
        (db.active_location_id(), name, d.get("contact_name", ""), d.get("phone", ""),
         d.get("email", ""), d.get("account_number", ""), d.get("order_days", ""), d.get("notes", "")),
    )
    db.get_db().commit()
    return jsonify({"id": cur.lastrowid})


@app.get("/api/vendors/<int:vid>")
def vendor_get(vid):
    db_ = db.get_db()
    v = db_.execute("SELECT * FROM vendors WHERE id=? AND location_id IS ?",
                    (vid, db.active_location_id())).fetchone()
    if not v:
        abort(404)
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
    out["spend"] = round(sum((r["total"] or 0) for r in invoices), 2)
    return jsonify(out)


@app.put("/api/vendors/<int:vid>")
def vendor_update(vid):
    d = request.json or {}
    fields = ["name", "contact_name", "phone", "email", "account_number",
              "order_days", "notes", "archived"]
    sets, vals = [], []
    for key in fields:
        if key in d:
            sets.append(f"{key}=?")
            vals.append(d[key])
    if not sets:
        return jsonify({"ok": True})
    vals += [vid, db.active_location_id()]
    cur = db.get_db().execute(
        f"UPDATE vendors SET {','.join(sets)} WHERE id=? AND location_id IS ?", vals)
    if cur.rowcount == 0:
        abort(404)
    db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/vendors/<int:vid>")
def vendor_delete(vid):
    cur = db.get_db().execute(
        "UPDATE vendors SET archived=1 WHERE id=? AND location_id IS ?",
        (vid, db.active_location_id()))
    if cur.rowcount == 0:
        abort(404)
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
    d = request.json or {}
    name = (d.get("name") or "").strip()
    ctype = (d.get("category_type") or "").strip()
    if not name or not ctype:
        return jsonify({"error": "Category needs a name and a type."}), 400
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
    d = request.json or {}
    sets, vals = [], []
    for key in ("name", "category_type", "sort_order", "archived"):
        if key in d:
            sets.append(f"{key}=?"); vals.append(d[key])
    if sets:
        vals.append(cid)
        db.get_db().execute(f"UPDATE categories SET {','.join(sets)} WHERE id=?", vals)
        db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/categories/<int:cid>")
def category_delete(cid):
    db.get_db().execute("UPDATE categories SET archived=1 WHERE id=?", (cid,))
    db.get_db().commit()
    return jsonify({"ok": True})


# --- products ---------------------------------------------------------------
# "Products" are stored in inventory_items (shared with the Stock/Count screen).

_PRODUCT_COLS = ("name", "category", "category_id", "unit", "report_by_unit",
                 "accounting_code", "on_inventory", "tax_exempt", "par_level",
                 "last_count", "unit_cost", "vendor", "sort_order", "archived")


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
    d = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Product needs a name."}), 400
    cols = [k for k in _PRODUCT_COLS if k in d]
    if "name" not in cols:
        cols.append("name")
    cols.append("location_id")
    vals = [name if k == "name" else (db.active_location_id() if k == "location_id" else d.get(k))
            for k in cols]
    placeholders = ",".join("?" * len(cols))
    cur = db.get_db().execute(
        f"INSERT INTO inventory_items({','.join(cols)}) VALUES({placeholders})", vals
    )
    db.get_db().commit()
    return jsonify({"id": cur.lastrowid})


@app.get("/api/products/purchase-report")
def product_purchase_report():
    start, end = cogs.parse_range(request.args.get("start"), request.args.get("end"))
    rows = db.get_db().execute(
        "SELECT ii.name AS product, c.category_type, c.name AS category, "
        "       ii.unit AS report_by, COALESCE(SUM(ii.qty),0) AS units, "
        "       COALESCE(SUM(ii.total),0) AS spend "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN categories c ON c.id = ii.category_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ? "
        "  AND ii.name IS NOT NULL AND TRIM(ii.name) <> '' "
        "GROUP BY ii.name, ii.category_id ORDER BY spend DESC",
        (db.active_location_id(), start.isoformat(), end.isoformat()),
    ).fetchall()
    out = [{**dict(r), "units": round(r["units"], 2), "spend": round(r["spend"], 2)}
           for r in rows]
    return jsonify({"rows": out,
                    "period": {"start": start.isoformat(), "end": end.isoformat()}})


@app.get("/api/products/new-items")
def product_new_items():
    rows = db.get_db().execute(
        "SELECT vi.*, c.name AS category_name, c.category_type, p.name AS product_name "
        "FROM vendor_items vi LEFT JOIN categories c ON c.id = vi.category_id "
        "LEFT JOIN inventory_items p ON p.id = vi.product_id "
        "WHERE vi.archived=0 AND vi.status='new' AND vi.location_id IS ? "
        "ORDER BY vi.created_at DESC, vi.id DESC", (db.active_location_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/products/new-items/<int:vid>/accept")
def product_new_item_accept(vid):
    """Mark a new vendor item reviewed; optionally set its category/product."""
    d = request.json or {}
    sets, vals = ["status='reviewed'"], []
    if "category_id" in d:
        sets.append("category_id=?"); vals.append(d["category_id"])
    if "product_id" in d:
        sets.append("product_id=?"); vals.append(d["product_id"])
    vals += [vid, db.active_location_id()]
    cur = db.get_db().execute(
        f"UPDATE vendor_items SET {','.join(sets)} WHERE id=? AND location_id IS ?", vals)
    if cur.rowcount == 0:
        abort(404)
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
        abort(404)
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
    d = request.json or {}
    sets, vals = [], []
    for key in _PRODUCT_COLS:
        if key in d:
            sets.append(f"{key}=?"); vals.append(d[key])
    if sets:
        vals += [pid, db.active_location_id()]
        cur = db.get_db().execute(
            f"UPDATE inventory_items SET {','.join(sets)} WHERE id=? AND location_id IS ?", vals)
        if cur.rowcount == 0:
            abort(404)
        db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/products/<int:pid>")
def product_delete(pid):
    cur = db.get_db().execute(
        "UPDATE inventory_items SET archived=1 WHERE id=? AND location_id IS ?",
        (pid, db.active_location_id()))
    if cur.rowcount == 0:
        abort(404)
    db.get_db().commit()
    return jsonify({"ok": True})


# --- vendor items -----------------------------------------------------------

@app.get("/api/vendor-items")
def vendor_item_list():
    where = ["vi.archived = 0", "vi.location_id IS ?"]
    params = [db.active_location_id()]
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
        "LEFT JOIN inventory_items p ON p.id = vi.product_id "
        f"WHERE {' AND '.join(where)} ORDER BY vi.vendor_item_name COLLATE NOCASE LIMIT 5000",
        params,
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/vendor-items")
def vendor_item_create():
    d = request.json or {}
    name = (d.get("vendor_item_name") or "").strip()
    if not name:
        return jsonify({"error": "Vendor item needs a name."}), 400
    cur = db.get_db().execute(
        "INSERT INTO vendor_items(location_id, vendor_id, vendor_name, vendor_item_name, product_id, "
        "category_id, item_code, order_guide, status) VALUES(?,?,?,?,?,?,?,?, 'reviewed')",
        (db.active_location_id(), _i(d.get("vendor_id")), d.get("vendor_name", ""), name,
         _i(d.get("product_id")), _i(d.get("category_id")), d.get("item_code", ""), _i(d.get("order_guide"), 0)),
    )
    db.get_db().commit()
    return jsonify({"id": cur.lastrowid})


@app.put("/api/vendor-items/<int:vid>")
def vendor_item_update(vid):
    d = request.json or {}
    sets, vals = [], []
    for key in ("vendor_name", "vendor_item_name", "product_id", "category_id",
                "item_code", "order_guide", "status", "archived"):
        if key in d:
            sets.append(f"{key}=?"); vals.append(d[key])
    if sets:
        vals += [vid, db.active_location_id()]
        cur = db.get_db().execute(
            f"UPDATE vendor_items SET {','.join(sets)} WHERE id=? AND location_id IS ?", vals)
        if cur.rowcount == 0:
            abort(404)
        db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/vendor-items/<int:vid>")
def vendor_item_delete(vid):
    cur = db.get_db().execute(
        "UPDATE vendor_items SET archived=1 WHERE id=? AND location_id IS ?",
        (vid, db.active_location_id()))
    if cur.rowcount == 0:
        abort(404)
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
    mix = (request.json or {}).get("mix") or {}
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
    items = line_items or []
    line_sum = round(sum((_f(li.get("total"), 0) or 0) for li in items), 2)
    sub, tot, tx = _f(subtotal), _f(total), (_f(tax) or 0)
    targets = []
    if sub is not None:
        targets.append(round(sub, 2))
    if tot is not None:
        targets.append(round(tot, 2))
        if tx:
            targets.append(round(tot - tx, 2))   # pre-tax base when lines exclude tax
    if not targets or not items:
        return {"line_sum": line_sum, "expected": (targets[0] if targets else None),
                "delta": None, "ok": None}
    expected = min(targets, key=lambda t: abs(line_sum - t))   # whichever convention fits
    delta = round(line_sum - expected, 2)
    tol = max(0.02, abs(expected) * 0.005)   # tolerate penny rounding; flag a real gap
    return {"line_sum": line_sum, "expected": expected, "delta": delta, "ok": abs(delta) <= tol}


def _find_duplicate_invoice(database, loc, vendor, invoice_number, inv_date, total):
    """Return a brief {id, vendor, invoice_date, invoice_number, total} for an
    existing invoice in this location that looks like the same one, else None.

    Strong signal: same vendor + a non-empty invoice number. Fallback when there's
    no number: same vendor + date + total (within a cent)."""
    vendor = (vendor or "").strip()
    num = (invoice_number or "").strip()
    cols = "id, vendor, invoice_date, invoice_number, total"
    if vendor and num:
        row = database.execute(
            f"SELECT {cols} FROM invoices WHERE location_id IS ? AND lower(vendor)=lower(?) "
            "AND invoice_number <> '' AND lower(invoice_number)=lower(?) ORDER BY id DESC LIMIT 1",
            (loc, vendor, num),
        ).fetchone()
        if row:
            return dict(row)
    if vendor and inv_date and total is not None:
        row = database.execute(
            f"SELECT {cols} FROM invoices WHERE location_id IS ? AND lower(vendor)=lower(?) "
            "AND invoice_date=? AND total IS NOT NULL AND ABS(total - ?) < 0.01 "
            "ORDER BY id DESC LIMIT 1",
            (loc, vendor, inv_date, total),
        ).fetchone()
        if row:
            return dict(row)
    return None


def _resolve_vendor_item(database, vendor, inv_date, line, loc):
    """Match an invoice line to a vendor_item within this location (creating a
    'new' one if unseen), refresh its last price/date, return (vi_id, cat_id)."""
    name = (line.get("name") or "").strip()
    cat_id = _category_id_by_name(database, line.get("category"))
    price = _f(line.get("unit_cost"))
    if not name:
        return None, cat_id
    row = database.execute(
        "SELECT id, category_id FROM vendor_items "
        "WHERE location_id IS ? AND lower(COALESCE(vendor_name,'')) = lower(?) "
        "  AND lower(vendor_item_name) = lower(?)",
        (loc, vendor or "", name),
    ).fetchone()
    if row:
        # Only advance the last-purchase price/date when this invoice is at least
        # as recent as what's recorded, so editing (or backfilling) an OLDER
        # invoice can't roll the latest price backwards.
        database.execute(
            "UPDATE vendor_items SET last_purchase_date=?, last_purchase_price=?, "
            "category_id=COALESCE(category_id, ?) "
            "WHERE id=? AND (last_purchase_date IS NULL OR last_purchase_date <= ?)",
            (inv_date, price, cat_id, row["id"], inv_date),
        )
        return row["id"], (row["category_id"] or cat_id)
    vrow = database.execute(
        "SELECT id FROM vendors WHERE location_id IS ? AND lower(name)=lower(?)",
        (loc, vendor or ""),
    ).fetchone()
    cur = database.execute(
        "INSERT INTO vendor_items(location_id, vendor_id, vendor_name, vendor_item_name, "
        "category_id, last_purchase_date, last_purchase_price, status) "
        "VALUES(?,?,?,?,?,?,?, 'new')",
        (loc, vrow["id"] if vrow else None, vendor, name, cat_id, inv_date, price),
    )
    return cur.lastrowid, cat_id


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


with app.app_context():
    db.init_db()


def _backup_loop(every_hours=6):
    while True:
        time.sleep(every_hours * 3600)
        try:
            db.backup()
        except Exception as e:   # a failure must never take the server down...
            print(f"  [!] Periodic backup failed: {e}")   # ...but don't fail silently


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8088"))
    host = os.environ.get("HOST", "0.0.0.0")
    if not os.environ.get("APP_PASSWORD"):
        print("\n  [!] APP_PASSWORD is not set — the ledger is open to anyone on "
              "your network.\n      Set it before exposing this beyond localhost.\n")
    debug = bool(os.environ.get("DEBUG"))
    # In debug Werkzeug's reloader runs this block in both parent and child; do
    # the backup work only in the worker (or always, when the reloader is off).
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        try:
            snap = db.backup()
            if snap:
                print(f"  Backed up to {os.path.relpath(snap, BASE_DIR)}")
        except Exception as e:
            print(f"  [!] Startup backup failed: {e}")
        threading.Thread(target=_backup_loop, daemon=True).start()
    print(f"  Barkeep's Ledger running at http://{host}:{port}\n")
    app.run(host=host, port=port, debug=debug)
