"""SQLite layer for Barkeep's Ledger.

Self-contained: the database is a single file under data/ledger.db and the
schema is created on first run. No migrations tool needed for a personal app —
schema changes are additive and guarded by `CREATE TABLE IF NOT EXISTS`.
"""
import os
import sqlite3
from flask import g

DB_PATH = os.environ.get(
    "LEDGER_DB",
    os.path.join(os.path.dirname(__file__), "data", "ledger.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Stores. Each location's data (products, vendors, vendor items, invoices,
-- counts, sales mix) is kept separate; categories are shared. Each maps to a
-- Square location so sales/labor follow the active store. Seeded on first run.
CREATE TABLE IF NOT EXISTS locations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,
    square_location_id TEXT,
    archived           INTEGER DEFAULT 0,
    created_at         TEXT DEFAULT (datetime('now'))
);

-- The two-level spend taxonomy: every Category belongs to a Category Type.
-- Seeded on first run (see TAXONOMY); the user can add/rename/archive later.
CREATE TABLE IF NOT EXISTS categories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    category_type TEXT NOT NULL,      -- Food | Beer | Wine | Liquor | N/A Bev | Other
    sort_order    INTEGER DEFAULT 0,
    archived      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invoices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id    INTEGER REFERENCES locations(id),
    vendor         TEXT,
    invoice_date   TEXT,             -- ISO yyyy-mm-dd
    invoice_number TEXT,
    category       TEXT,             -- legacy whole-invoice tag, now optional
    subtotal       REAL,
    tax            REAL,
    total          REAL,
    image_path     TEXT,             -- relative filename under uploads/
    notes          TEXT,
    raw_json       TEXT,             -- the AI parse result, for audit
    status         TEXT DEFAULT 'closed',   -- processing | action_required | closed
    payment_account TEXT,
    upload_date    TEXT DEFAULT (datetime('now')),
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id        INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    name              TEXT,
    qty               REAL,
    unit              TEXT,
    unit_cost         REAL,
    total             REAL,
    inventory_item_id INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL,
    vendor_item_id    INTEGER REFERENCES vendor_items(id) ON DELETE SET NULL,
    category_id       INTEGER REFERENCES categories(id) ON DELETE SET NULL
);

-- Vendor-specific SKUs. Each maps (eventually) to a canonical Product and a
-- Category. New ones land in the "New Item Review" queue (status = 'new').
CREATE TABLE IF NOT EXISTS vendor_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id        INTEGER REFERENCES locations(id),
    vendor_id          INTEGER REFERENCES vendors(id) ON DELETE SET NULL,
    vendor_name        TEXT,
    vendor_item_name   TEXT,
    product_id         INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL,
    category_id        INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    item_code          TEXT,
    last_purchase_date TEXT,
    last_purchase_price REAL,
    order_guide        INTEGER DEFAULT 0,
    status             TEXT DEFAULT 'new',   -- new | reviewed
    archived           INTEGER DEFAULT 0,
    created_at         TEXT DEFAULT (datetime('now'))
);

-- Actual sales mix the user enters per reporting period; powers P&L income.
CREATE TABLE IF NOT EXISTS sales_mix (
    location_id   INTEGER NOT NULL DEFAULT 0,
    period_start  TEXT NOT NULL,
    period_end    TEXT NOT NULL,
    category_type TEXT NOT NULL,
    pct           REAL DEFAULT 0,
    PRIMARY KEY (location_id, period_start, period_end, category_type)
);

CREATE TABLE IF NOT EXISTS vendors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id    INTEGER REFERENCES locations(id),
    name           TEXT NOT NULL,
    contact_name   TEXT,
    phone          TEXT,
    email          TEXT,
    account_number TEXT,
    order_days     TEXT,             -- e.g. "Tue / Fri"
    notes          TEXT,
    archived       INTEGER DEFAULT 0,
    created_at     TEXT DEFAULT (datetime('now'))
);

-- "Products": the canonical item list. (Table keeps its original name so the
-- existing Stock/Count features and their foreign keys keep working; the new
-- Products UI/API drive off category_id and the extra columns below.)
CREATE TABLE IF NOT EXISTS inventory_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id    INTEGER REFERENCES locations(id),
    name           TEXT NOT NULL,
    category       TEXT,            -- legacy flat tag; superseded by category_id
    category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    unit           TEXT,            -- bottle | case | keg | lb | each ...
    report_by_unit TEXT,            -- reporting unit, e.g. "Each", "Bottle", "Keg (1/2BBL)"
    accounting_code TEXT,
    on_inventory   INTEGER DEFAULT 1,
    tax_exempt     INTEGER DEFAULT 0,
    par_level      REAL DEFAULT 0,
    last_count     REAL DEFAULT 0,
    unit_cost      REAL DEFAULT 0,
    vendor         TEXT,
    sort_order     INTEGER DEFAULT 0,
    archived       INTEGER DEFAULT 0,
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS counts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER REFERENCES locations(id),
    taken_at  TEXT DEFAULT (datetime('now')),
    note      TEXT,
    value     REAL DEFAULT 0        -- snapshot of total $ value at count time
);

CREATE TABLE IF NOT EXISTS count_lines (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    count_id  INTEGER NOT NULL REFERENCES counts(id) ON DELETE CASCADE,
    item_id   INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL,
    qty       REAL,
    unit_cost REAL                  -- unit cost captured at count time
);

-- Per-day net sales cache (keyed by Square location) so the Sales report only
-- calls Square for days it hasn't seen or that are still changing (today/yesterday).
CREATE TABLE IF NOT EXISTS daily_sales (
    square_location_id TEXT NOT NULL,
    date               TEXT NOT NULL,
    net_sales          REAL DEFAULT 0,
    fetched_at         TEXT,
    PRIMARY KEY (square_location_id, date)
);

CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_items_invoice ON invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_countlines_count ON count_lines(count_id);
CREATE INDEX IF NOT EXISTS idx_vendoritems_vendor ON vendor_items(vendor_id);
CREATE INDEX IF NOT EXISTS idx_vendoritems_name ON vendor_items(vendor_name, vendor_item_name);
"""

# Indexes on columns that may be added by _ensure_columns to pre-existing
# tables — created only after those columns are guaranteed to exist.
POST_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_items_category ON invoice_items(category_id);
"""

# The seeded two-level taxonomy: {Category Type: [Category, ...]}. Order within
# each list is the display sort order. Drives the Category Report, Purchase
# Report, and Controllable P&L grouping.
TAXONOMY = {
    "Food":    ["Meat", "Produce", "Bread", "Dairy", "Grocery and Dry Goods", "Seafood"],
    "Beer":    ["Beer Bottle / Can", "Beer Keg"],
    "Wine":    ["Wine"],
    "Liquor":  ["Liquor", "Bar Consumables"],
    "N/A Bev": ["NA Beverages"],
    "Other":   ["Paper Supplies", "Smallwares", "Linen / Laundry", "Retail"],
}

# Maps the old flat invoice/item category tags onto a seeded category name so
# existing rows get a sensible category_id during the one-time migration.
LEGACY_CATEGORY_MAP = {
    "liquor": "Liquor",
    "beer": "Beer Bottle / Can",
    "wine": "Wine",
    "na_beverage": "NA Beverages",
    "food": "Grocery and Dry Goods",
    "supplies": "Paper Supplies",
    "other": None,
}

# Seeded stores: (name, Square location id). The first is the default; all
# pre-location data is migrated onto it.
LOCATIONS = [
    ("Pubkey DC", "LNKNR2A7MBB4K"),
    ("Pubkey NYC", "LS1WRASW8V02R"),
]

DEFAULT_SETTINGS = {
    "target_cogs_pct": "30",      # % of sales
    "target_labor_pct": "25",     # % of sales
    "square_env": "production",   # production | sandbox
    "square_version": "2025-01-23",
    "ai_model": os.environ.get("LEDGER_AI_MODEL", "claude-opus-4-8"),
    "tz": "America/New_York",     # timezone for business-day boundaries
    "day_start_hour": "5",        # business day runs 5am -> 5am (next day)
}


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added to tables that predate this schema. ALTER TABLE ADD COLUMN
# disallows non-constant defaults, so upload_date is added bare and backfilled.
_ADDED_COLUMNS = {
    "invoices": {
        "status": "TEXT DEFAULT 'closed'",
        "payment_account": "TEXT",
        "upload_date": "TEXT",
        "location_id": "INTEGER",
    },
    "invoice_items": {
        "vendor_item_id": "INTEGER",
        "category_id": "INTEGER",
    },
    "inventory_items": {
        "category_id": "INTEGER",
        "report_by_unit": "TEXT",
        "accounting_code": "TEXT",
        "on_inventory": "INTEGER DEFAULT 1",
        "tax_exempt": "INTEGER DEFAULT 0",
        "location_id": "INTEGER",
    },
    "vendor_items": {"location_id": "INTEGER"},
    "vendors": {"location_id": "INTEGER"},
    "counts": {"location_id": "INTEGER"},
}


def _ensure_columns(conn):
    """Add any missing columns to pre-existing tables (idempotent)."""
    for table, cols in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _seed_taxonomy(conn):
    """Insert any seeded categories that aren't present yet (idempotent)."""
    order = 0
    for ctype, names in TAXONOMY.items():
        for name in names:
            conn.execute(
                "INSERT OR IGNORE INTO categories(name, category_type, sort_order) "
                "VALUES(?,?,?)",
                (name, ctype, order),
            )
            order += 1


def _seed_locations(conn):
    """Insert seeded stores that aren't present yet (idempotent)."""
    for name, sq in LOCATIONS:
        conn.execute(
            "INSERT OR IGNORE INTO locations(name, square_location_id) VALUES(?,?)",
            (name, sq),
        )


def _predrop_legacy_sales_mix(conn):
    """sales_mix gained location_id in its primary key; ALTER can't change a PK,
    so drop the old (pre-location, transient) table and let SCHEMA recreate it."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sales_mix)")}
    if cols and "location_id" not in cols:
        conn.execute("DROP TABLE sales_mix")


def _migrate_locations(conn):
    """One-time: tag all pre-location rows with the default store (DC), set the
    active location, and mirror its Square id into the square_location_id setting."""
    if conn.execute("SELECT 1 FROM settings WHERE key='migrated_locations'").fetchone():
        return
    row = conn.execute(
        "SELECT id, square_location_id FROM locations ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        return
    loc_id, sq = row["id"], row["square_location_id"]
    for tbl in ("invoices", "inventory_items", "vendor_items", "vendors", "counts"):
        conn.execute(f"UPDATE {tbl} SET location_id=? WHERE location_id IS NULL", (loc_id,))
    conn.execute("UPDATE sales_mix SET location_id=? WHERE location_id=0", (loc_id,))
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('active_location_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(loc_id),))
    if sq:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('square_location_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sq,))
    conn.execute("INSERT INTO settings(key, value) VALUES('migrated_locations','1')")


def _category_ids(conn):
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}


def _migrate_legacy_data(conn):
    """One-time backfill: map old flat categories -> category_id, denormalize
    category onto invoice_items, and seed vendor_items from invoice lines."""
    flag = conn.execute(
        "SELECT 1 FROM settings WHERE key='migrated_v2'"
    ).fetchone()
    if flag:
        return
    cat_ids = _category_ids(conn)
    legacy_to_id = {
        old: (cat_ids.get(name) if name else None)
        for old, name in LEGACY_CATEGORY_MAP.items()
    }

    # Products (inventory_items): legacy text category -> category_id.
    for r in conn.execute("SELECT id, category FROM inventory_items").fetchall():
        cid = legacy_to_id.get((r["category"] or "").lower())
        if cid:
            conn.execute("UPDATE inventory_items SET category_id=? WHERE id=?", (cid, r["id"]))

    # Invoice lines: inherit the invoice's legacy category for now.
    for r in conn.execute(
        "SELECT ii.id AS iid, inv.category AS cat FROM invoice_items ii "
        "JOIN invoices inv ON inv.id = ii.invoice_id"
    ).fetchall():
        cid = legacy_to_id.get((r["cat"] or "").lower())
        if cid:
            conn.execute("UPDATE invoice_items SET category_id=? WHERE id=?", (cid, r["iid"]))

    # Backfill upload_date from created_at where missing.
    conn.execute(
        "UPDATE invoices SET upload_date = COALESCE(upload_date, created_at) "
        "WHERE upload_date IS NULL"
    )

    # Seed vendor_items from the distinct (vendor, line name) pairs already
    # logged, so the Vendor Items screen isn't empty on first load.
    rows = conn.execute(
        "SELECT inv.vendor AS vendor, ii.name AS name, ii.category_id AS category_id, "
        "       MAX(inv.invoice_date) AS last_date, "
        "       (SELECT unit_cost FROM invoice_items x JOIN invoices xi ON xi.id=x.invoice_id "
        "        WHERE x.name=ii.name AND xi.vendor IS inv.vendor "
        "        ORDER BY xi.invoice_date DESC LIMIT 1) AS last_price "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "WHERE ii.name IS NOT NULL AND TRIM(ii.name) <> '' "
        "GROUP BY inv.vendor, ii.name"
    ).fetchall()
    vendor_ids = {
        (r["name"] or "").lower(): r["id"]
        for r in conn.execute("SELECT id, name FROM vendors").fetchall()
    }
    for r in rows:
        conn.execute(
            "INSERT INTO vendor_items(vendor_id, vendor_name, vendor_item_name, "
            "category_id, last_purchase_date, last_purchase_price, status) "
            "VALUES(?,?,?,?,?,?, 'reviewed')",
            (vendor_ids.get((r["vendor"] or "").lower()), r["vendor"], r["name"],
             r["category_id"], r["last_date"], r["last_price"]),
        )

    conn.execute("INSERT INTO settings(key, value) VALUES('migrated_v2','1')")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _predrop_legacy_sales_mix(conn)
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.executescript(POST_INDEXES)
    # Seed any default settings not already present, and pull secrets from env.
    env_seed = {
        "square_token": os.environ.get("SQUARE_ACCESS_TOKEN", ""),
        "square_location_id": os.environ.get("SQUARE_LOCATION_ID", ""),
    }
    for key, val in {**DEFAULT_SETTINGS, **env_seed}.items():
        row = conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO settings(key, value) VALUES(?,?)", (key, val))
    _seed_taxonomy(conn)
    _seed_locations(conn)
    _migrate_legacy_data(conn)
    _migrate_locations(conn)
    conn.commit()
    conn.close()


# --- settings helpers -------------------------------------------------------

def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def active_location_id():
    """The store currently being viewed. Falls back to the lowest location id."""
    v = get_setting("active_location_id")
    try:
        return int(v)
    except (TypeError, ValueError):
        row = get_db().execute("SELECT MIN(id) AS id FROM locations").fetchone()
        return row["id"] if row else None


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, "" if value is None else str(value)),
    )
    db.commit()


def all_settings():
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}
