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
import square_client
from invoice_ai import parse_invoice, InvoiceError

BASE_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.teardown_appcontext(db.close_db)


# --- auth -------------------------------------------------------------------
# Single shared passcode for a personal tool. Set APP_PASSWORD to enable it;
# leave it unset to run open (fine on a private LAN, noted in the README).

def _expected_token():
    pw = os.environ.get("APP_PASSWORD", "")
    if not pw:
        return None
    secret = os.environ.get("APP_SECRET", "barkeep-secret")
    return hmac.new(secret.encode(), pw.encode(), hashlib.sha256).hexdigest()


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
    if p == "/" or p.startswith("/static/") or p == "/api/login" or p == "/api/health":
        return
    if p.startswith("/api/") or p.startswith("/uploads/"):
        if not _authed():
            return jsonify({"error": "unauthorized"}), 401


@app.post("/api/login")
def login():
    expected = _expected_token()
    if expected is None:
        return jsonify({"token": "", "auth_required": False})
    pw = (request.json or {}).get("password", "")
    secret = os.environ.get("APP_SECRET", "barkeep-secret")
    token = hmac.new(secret.encode(), pw.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(token, expected):
        return jsonify({"token": token, "auth_required": True})
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
    return jsonify({
        "auth_required": _expected_token() is not None,
        "square_configured": square_client.is_configured(),
        "square_env": s.get("square_env", "production"),
        "square_location_id": s.get("square_location_id", ""),
        "square_version": s.get("square_version", ""),
        "has_square_token": bool((s.get("square_token") or "").strip()),
        "ai_model": s.get("ai_model", "claude-opus-4-8"),
        "ai_key_present": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "target_cogs_pct": s.get("target_cogs_pct", "30"),
        "target_labor_pct": s.get("target_labor_pct", "25"),
    })


@app.post("/api/settings")
def save_settings():
    data = request.json or {}
    # Only persist a Square token if a non-blank one is supplied (so the UI can
    # show "set" without round-tripping the secret).
    for key in ("square_location_id", "square_env", "square_version", "ai_model",
                "target_cogs_pct", "target_labor_pct"):
        if key in data:
            db.set_setting(key, data[key])
    if data.get("square_token"):
        db.set_setting("square_token", data["square_token"].strip())
    return jsonify({"ok": True})


@app.get("/api/locations")
def locations():
    return jsonify(square_client.list_locations())


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
    database = db.get_db()
    cur = database.execute(
        "INSERT INTO invoices(vendor, invoice_date, invoice_number, category, "
        "subtotal, tax, total, image_path, notes, raw_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            d.get("vendor", ""), d.get("invoice_date", ""), d.get("invoice_number", ""),
            d.get("category", "other"), _f(d.get("subtotal")), _f(d.get("tax")),
            _f(d.get("total")), d.get("image_path"), d.get("notes", ""),
            d.get("raw_json", ""),
        ),
    )
    inv_id = cur.lastrowid
    for it in items:
        database.execute(
            "INSERT INTO invoice_items(invoice_id, name, qty, unit, unit_cost, total) "
            "VALUES(?,?,?,?,?,?)",
            (inv_id, it.get("name", ""), _f(it.get("qty")), it.get("unit"),
             _f(it.get("unit_cost")), _f(it.get("total"))),
        )
    database.commit()
    return jsonify({"id": inv_id})


@app.get("/api/invoices")
def invoice_list():
    rows = db.get_db().execute(
        "SELECT id, vendor, invoice_date, invoice_number, category, total, image_path "
        "FROM invoices ORDER BY invoice_date DESC, id DESC LIMIT 300"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/invoices/<int:inv_id>")
def invoice_get(inv_id):
    db_ = db.get_db()
    row = db_.execute("SELECT * FROM invoices WHERE id=?", (inv_id,)).fetchone()
    if not row:
        abort(404)
    items = db_.execute(
        "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (inv_id,)
    ).fetchall()
    out = dict(row)
    out["line_items"] = [dict(i) for i in items]
    return jsonify(out)


@app.delete("/api/invoices/<int:inv_id>")
def invoice_delete(inv_id):
    db_ = db.get_db()
    row = db_.execute("SELECT image_path FROM invoices WHERE id=?", (inv_id,)).fetchone()
    db_.execute("DELETE FROM invoices WHERE id=?", (inv_id,))
    db_.commit()
    if row and row["image_path"]:
        try:
            os.remove(os.path.join(UPLOAD_DIR, row["image_path"]))
        except OSError:
            pass
    return jsonify({"ok": True})


# --- inventory --------------------------------------------------------------

@app.get("/api/inventory")
def inventory_list():
    rows = db.get_db().execute(
        "SELECT * FROM inventory_items WHERE archived=0 "
        "ORDER BY category, sort_order, name"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/inventory")
def inventory_create():
    d = request.json or {}
    cur = db.get_db().execute(
        "INSERT INTO inventory_items(name, category, unit, par_level, last_count, "
        "unit_cost, vendor, sort_order) VALUES(?,?,?,?,?,?,?,?)",
        (d.get("name", "").strip(), d.get("category", "other"), d.get("unit", ""),
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
    vals.append(item_id)
    db.get_db().execute(f"UPDATE inventory_items SET {','.join(sets)} WHERE id=?", vals)
    db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/inventory/<int:item_id>")
def inventory_delete(item_id):
    db.get_db().execute("UPDATE inventory_items SET archived=1 WHERE id=?", (item_id,))
    db.get_db().commit()
    return jsonify({"ok": True})


@app.get("/api/inventory/order-list")
def order_list():
    """Items at or below par, with how many units to bring back up to par."""
    rows = db.get_db().execute(
        "SELECT * FROM inventory_items WHERE archived=0 AND last_count <= par_level "
        "ORDER BY category, name"
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
    cur = database.execute(
        "INSERT INTO counts(note, value) VALUES(?, 0)", (d.get("note", ""),)
    )
    count_id = cur.lastrowid
    total_value = 0.0
    for ln in lines:
        item = database.execute(
            "SELECT unit_cost FROM inventory_items WHERE id=?", (ln.get("item_id"),)
        ).fetchone()
        unit_cost = (item["unit_cost"] if item else 0) or 0
        qty = _f(ln.get("qty"), 0) or 0
        total_value += qty * unit_cost
        database.execute(
            "INSERT INTO count_lines(count_id, item_id, qty, unit_cost) VALUES(?,?,?,?)",
            (count_id, ln.get("item_id"), qty, unit_cost),
        )
        database.execute(
            "UPDATE inventory_items SET last_count=? WHERE id=?", (qty, ln.get("item_id"))
        )
    database.execute("UPDATE counts SET value=? WHERE id=?", (round(total_value, 2), count_id))
    database.commit()
    return jsonify({"id": count_id, "value": round(total_value, 2)})


@app.get("/api/counts")
def count_list():
    rows = db.get_db().execute(
        "SELECT id, taken_at, note, value FROM counts ORDER BY taken_at DESC LIMIT 100"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# --- helpers ----------------------------------------------------------------

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8088"))
    host = os.environ.get("HOST", "0.0.0.0")
    if not os.environ.get("APP_PASSWORD"):
        print("\n  [!] APP_PASSWORD is not set — the ledger is open to anyone on "
              "your network.\n      Set it before exposing this beyond localhost.\n")
    print(f"  Barkeep's Ledger running at http://{host}:{port}\n")
    app.run(host=host, port=port, debug=bool(os.environ.get("DEBUG")))
