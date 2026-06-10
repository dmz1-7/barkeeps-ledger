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
import reports
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
    loc_id = (request.json or {}).get("location_id")
    database = db.get_db()
    row = database.execute(
        "SELECT id, square_location_id FROM locations WHERE id=?", (loc_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "Unknown location."}), 400
    db.set_setting("active_location_id", row["id"])
    # Mirror the store's Square id so sales/labor follow the active location.
    db.set_setting("square_location_id", row["square_location_id"] or "")
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
    cur = database.execute(
        "INSERT INTO counts(location_id, note, value) VALUES(?, ?, 0)",
        (db.active_location_id(), d.get("note", "")),
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
        "SELECT id, taken_at, note, value FROM counts WHERE location_id IS ? "
        "ORDER BY taken_at DESC LIMIT 100", (db.active_location_id(),)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# --- vendors ----------------------------------------------------------------
# Invoices and inventory items reference a vendor by name (free text), so spend
# and links are matched case-insensitively on the vendor's name.

def _period_bounds(today=None):
    import datetime as _dt
    today = today or _dt.date.today()
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
    v = db_.execute("SELECT * FROM vendors WHERE id=?", (vid,)).fetchone()
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
    vals.append(vid)
    db.get_db().execute(f"UPDATE vendors SET {','.join(sets)} WHERE id=?", vals)
    db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/vendors/<int:vid>")
def vendor_delete(vid):
    db.get_db().execute("UPDATE vendors SET archived=1 WHERE id=?", (vid,))
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
    vals.append(vid)
    db.get_db().execute(f"UPDATE vendor_items SET {','.join(sets)} WHERE id=?", vals)
    db.get_db().commit()
    return jsonify({"ok": True})


@app.get("/api/products/<int:pid>")
def product_get(pid):
    d = db.get_db()
    row = d.execute(
        "SELECT p.*, c.name AS category_name, c.category_type "
        "FROM inventory_items p LEFT JOIN categories c ON c.id = p.category_id "
        "WHERE p.id=?", (pid,),
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
        vals.append(pid)
        db.get_db().execute(f"UPDATE inventory_items SET {','.join(sets)} WHERE id=?", vals)
        db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/products/<int:pid>")
def product_delete(pid):
    db.get_db().execute("UPDATE inventory_items SET archived=1 WHERE id=?", (pid,))
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
        f"WHERE {' AND '.join(where)} ORDER BY vi.vendor_item_name COLLATE NOCASE LIMIT 1000",
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
        vals.append(vid)
        db.get_db().execute(f"UPDATE vendor_items SET {','.join(sets)} WHERE id=?", vals)
        db.get_db().commit()
    return jsonify({"ok": True})


@app.delete("/api/vendor-items/<int:vid>")
def vendor_item_delete(vid):
    db.get_db().execute("UPDATE vendor_items SET archived=1 WHERE id=?", (vid,))
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
        database.execute(
            "UPDATE vendor_items SET last_purchase_date=?, last_purchase_price=?, "
            "category_id=COALESCE(category_id, ?) WHERE id=?",
            (inv_date, price, cat_id, row["id"]),
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8088"))
    host = os.environ.get("HOST", "0.0.0.0")
    if not os.environ.get("APP_PASSWORD"):
        print("\n  [!] APP_PASSWORD is not set — the ledger is open to anyone on "
              "your network.\n      Set it before exposing this beyond localhost.\n")
    print(f"  Barkeep's Ledger running at http://{host}:{port}\n")
    app.run(host=host, port=port, debug=bool(os.environ.get("DEBUG")))
