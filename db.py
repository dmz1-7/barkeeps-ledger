"""SQLite layer for Barkeep's Ledger.

Self-contained: the database is a single file under data/ledger.db and the
schema is created on first run. No migrations tool needed for a personal app —
schema changes are additive and guarded by `CREATE TABLE IF NOT EXISTS`.
"""
import datetime as dt
import glob
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
    location_id   INTEGER NOT NULL,   -- no default: a missing store must error, not orphan on 0
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
    unit           TEXT,            -- purchase/costing unit: bottle | case | keg | lb | each ...
    size_qty       REAL,            -- content of one purchase unit (e.g. 750), for recipe costing
    size_unit      TEXT,            -- unit of size_qty (e.g. "ml", "oz", "each")
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

-- Recipes / plate costing. A recipe (menu item) costs the sum of its ingredient
-- lines, each = qty * the linked product's unit_cost. menu_price + yield_qty
-- give cost%, per-serving cost, and margin.
CREATE TABLE IF NOT EXISTS recipes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER REFERENCES locations(id),
    name        TEXT NOT NULL,
    menu_price  REAL DEFAULT 0,     -- selling price of ONE serving
    yield_qty   REAL DEFAULT 1,     -- servings the recipe yields
    notes       TEXT,
    archived    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recipe_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id  INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL,
    qty        REAL DEFAULT 0,      -- amount used, in `unit`
    unit       TEXT,                -- recipe unit (oz, ml, each, ...); blank = product's costing unit
    note       TEXT
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

CREATE INDEX IF NOT EXISTS idx_items_invoice ON invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_countlines_count ON count_lines(count_id);
CREATE INDEX IF NOT EXISTS idx_recipeitems_recipe ON recipe_items(recipe_id);
CREATE INDEX IF NOT EXISTS idx_vendoritems_vendor ON vendor_items(vendor_id);
CREATE INDEX IF NOT EXISTS idx_vendoritems_name ON vendor_items(vendor_name, vendor_item_name);
"""

# Indexes on columns that may be added by _ensure_columns to pre-existing
# tables — created only after those columns are guaranteed to exist.
POST_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_items_category ON invoice_items(category_id);
-- Composite (location, ...) indexes: every report scopes by location_id, so a
-- date-only / single-column index forced a scan to filter the store.
CREATE INDEX IF NOT EXISTS idx_invoices_loc_date ON invoices(location_id, invoice_date);
CREATE INDEX IF NOT EXISTS idx_inv_loc_arch ON inventory_items(location_id, archived);
CREATE INDEX IF NOT EXISTS idx_counts_loc_taken ON counts(location_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_items_invitem ON invoice_items(inventory_item_id);
-- Index the ON DELETE SET NULL child FK columns so deleting a parent inventory
-- item (e.g. a MarginEdge re-import wiping a store's products) seeks the
-- referencing rows instead of full-scanning count_lines / recipe_items.
CREATE INDEX IF NOT EXISTS idx_countlines_item ON count_lines(item_id);
CREATE INDEX IF NOT EXISTS idx_recipeitems_product ON recipe_items(product_id);
-- Case-insensitive product-name seek: the MarginEdge importer resolves each row
-- with lower(name)=lower(?) per store; without this, every row is a per-store scan.
CREATE INDEX IF NOT EXISTS idx_inv_loc_name ON inventory_items(location_id, name COLLATE NOCASE);
-- One Square location id per store: the daily_sales cache + sales/labor pulls key
-- on it, so a duplicate would cross-pollute two stores' Square numbers. Partial,
-- so blank/NULL ids (stores not yet wired to Square) don't collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_locations_sqid ON locations(square_location_id)
    WHERE COALESCE(square_location_id, '') <> '';
-- Drop the redundant date-only invoice index: every invoice query also scopes by
-- location_id, so idx_invoices_loc_date (location_id, invoice_date) already covers
-- it and the single-column index only added write cost.
DROP INDEX IF EXISTS idx_invoices_date;
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
    "default_hourly_wage": "0",   # $/hr applied to Square shifts with no wage (0 = off)
    "price_alert_pct": "10",      # flag a vendor item when its price jumps >= this %
    "square_env": "production",   # production | sandbox
    "square_version": "2025-01-23",
    "ai_model": os.environ.get("LEDGER_AI_MODEL", "claude-opus-4-8"),
    "tz": "America/New_York",     # timezone for business-day boundaries
    "day_start_hour": "5",        # business day runs 5am -> 5am (next day)
}


_SCHEMA_READY = set()   # DB_PATHs whose columns this process has ensured


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Self-heal once per process: re-run ONLY _ensure_columns (column adds) so
        # a server started before a column migration doesn't 500 on reads. Full
        # migration — POST_INDEXES, the sales_mix rebuild, data backfills — is
        # handled by init_db() at import (see the bottom of app.py), not here.
        if DB_PATH not in _SCHEMA_READY:
            try:
                _ensure_columns(conn)
                conn.executescript(POST_INDEXES)   # composite indexes on the added columns
                conn.commit()
                _SCHEMA_READY.add(DB_PATH)   # only mark ready if it actually succeeded
            except sqlite3.OperationalError:
                pass   # table not created yet (first boot) — init_db handles it; retry next request
        g.db = conn
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added to tables that predate this schema. ALTER TABLE ADD COLUMN
# disallows non-constant defaults, so upload_date is added bare and backfilled.
# NOTE: ALTER ADD COLUMN also can't carry a REFERENCES/ON DELETE constraint, so
# on a MIGRATED db these columns (location_id, vendor_item_id, category_id) lack
# the FKs that a FRESH db has. We deliberately DON'T rebuild the tables to add
# them: a full CREATE+copy+swap on the live two-store DB is a real corruption
# risk, and the app soft-deletes (archived=1) rather than hard-deleting, so the
# missing ON DELETE SET NULL paths effectively never fire. Tenant scoping is
# enforced in the queries, not by these FKs.
# Also: get_db()'s self-heal only re-runs _ensure_columns + POST_INDEXES. Any new
# column here that needs non-NULL values on EXISTING rows must get its own
# idempotent backfill wired into init_db (see _backfill_location_ids) — the
# self-heal won't populate it.
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
        "size_qty": "REAL",
        "size_unit": "TEXT",
    },
    "vendor_items": {"location_id": "INTEGER"},
    "vendors": {"location_id": "INTEGER"},
    "counts": {"location_id": "INTEGER"},
    "recipe_items": {"unit": "TEXT"},
}


def _ensure_columns(conn):
    """Add any missing columns to pre-existing tables (idempotent)."""
    for table, cols in _ADDED_COLUMNS.items():
        # These identifiers come only from the _ADDED_COLUMNS constant, never from
        # request data, so there's no injection today. Assert they're plain
        # identifiers anyway, so a future edit that sourced a name from user input
        # can't silently become injectable through this f-string interpolation.
        assert table.isidentifier(), table
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            assert col.isidentifier(), col
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
    so set the old (pre-location) table ASIDE and let SCHEMA recreate it. The old
    rows are user-entered P&L income mix — NOT transient — so they're preserved
    and re-inserted by _restore_legacy_sales_mix (tagged location_id=0, which
    _migrate_locations then maps to the default store)."""
    # If a prior init_db crashed between the rename and the restore, the temp
    # table still holds the user's rows — DON'T drop it; leave it for
    # _restore_legacy_sales_mix to recover this run.
    if {r["name"] for r in conn.execute("PRAGMA table_info(_sales_mix_legacy)")}:
        return
    info = list(conn.execute("PRAGMA table_info(sales_mix)"))
    cols = {r["name"] for r in info}
    # Rebuild if the table predates location_id OR still carries the legacy
    # DEFAULT 0 on location_id (CREATE IF NOT EXISTS can't drop a column default).
    legacy_default = any(r["name"] == "location_id" and r["dflt_value"] is not None for r in info)
    if cols and ("location_id" not in cols or legacy_default):
        conn.execute("ALTER TABLE sales_mix RENAME TO _sales_mix_legacy")


def _restore_legacy_sales_mix(conn):
    """Re-insert rows set aside by _predrop_legacy_sales_mix into the rebuilt
    sales_mix with location_id=0 (mapped to the default store by migration)."""
    legacy = {r["name"] for r in conn.execute("PRAGMA table_info(_sales_mix_legacy)")}
    if not legacy:
        return
    # Preserve real location_ids (the default-rebuild case); fall back to 0 (the
    # pre-location case) which _migrate_locations/_backfill maps to the default store.
    loc_expr = "location_id" if "location_id" in legacy else "0"
    conn.execute(
        f"INSERT OR IGNORE INTO sales_mix(location_id, period_start, period_end, category_type, pct) "
        f"SELECT {loc_expr}, period_start, period_end, category_type, pct FROM _sales_mix_legacy")
    conn.execute("DROP TABLE _sales_mix_legacy")


def _migrate_locations(conn):
    """One-time: tag all pre-location rows with the default store and set the
    active location. The Square id lives per-store on the locations row now, so
    nothing is mirrored into a global setting."""
    if conn.execute("SELECT 1 FROM settings WHERE key='migrated_locations'").fetchone():
        return
    row = conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()
    if not row:
        return
    loc_id = row["id"]
    for tbl in ("invoices", "inventory_items", "vendor_items", "vendors", "counts"):
        conn.execute(f"UPDATE {tbl} SET location_id=? WHERE location_id IS NULL", (loc_id,))
    conn.execute("UPDATE sales_mix SET location_id=? WHERE location_id=0", (loc_id,))
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('active_location_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(loc_id),))
    conn.execute("INSERT INTO settings(key, value) VALUES('migrated_locations','1')")


def _backfill_location_ids(conn):
    """Idempotent: tag any row still missing a location_id with the default store.
    Schema-driven (every _ADDED_COLUMNS table that has a location_id), so a table
    that becomes location-scoped later is handled without a new one-shot flag."""
    row = conn.execute("SELECT MIN(id) AS id FROM locations").fetchone()
    if not row or row["id"] is None:
        return
    for tbl, cols in _ADDED_COLUMNS.items():
        if "location_id" in cols:
            conn.execute(f"UPDATE {tbl} SET location_id=? WHERE location_id IS NULL", (row["id"],))


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
    conn.execute("PRAGMA foreign_keys = ON")   # match get_db so any cascading migration behaves
    _predrop_legacy_sales_mix(conn)
    conn.executescript(SCHEMA)
    _restore_legacy_sales_mix(conn)
    _ensure_columns(conn)
    conn.executescript(POST_INDEXES)
    # Seed any default settings not already present, and pull the (global) token
    # from env. The Square *location* is per-store now, so it's seeded onto the
    # store row below rather than into a global setting.
    env_seed = {"square_token": os.environ.get("SQUARE_ACCESS_TOKEN", "")}
    for key, val in {**DEFAULT_SETTINGS, **env_seed}.items():
        row = conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO settings(key, value) VALUES(?,?)", (key, val))
    _seed_taxonomy(conn)
    _seed_locations(conn)
    # Env bootstrap of the default store's Square location id (only fills an empty
    # slot — existing/seeded ids win).
    env_sq = os.environ.get("SQUARE_LOCATION_ID", "").strip()
    if env_sq:
        conn.execute(
            "UPDATE locations SET square_location_id=? "
            "WHERE id=(SELECT MIN(id) FROM locations) AND COALESCE(square_location_id,'')=''",
            (env_sq,))
    _migrate_legacy_data(conn)
    _migrate_locations(conn)
    _backfill_location_ids(conn)   # idempotent, schema-driven safety net
    # Idempotent: re-home any sales_mix row stranded on the legacy location_id=0
    # default (e.g. from an old DB) onto the default store so it's not invisible.
    conn.execute(
        "UPDATE sales_mix SET location_id=(SELECT MIN(id) FROM locations) WHERE location_id=0")
    conn.commit()
    conn.close()


# --- settings helpers -------------------------------------------------------

def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def active_location_id():
    """The store THIS request is acting on. A per-request override (set from the
    X-Location-Id header in app._resolve_location) wins, so concurrent devices and
    tabs no longer share one mutable 'current store'. Falls back to the persisted
    setting, then the lowest location id, for non-SPA callers."""
    override = g.get("location_override")
    if override is not None:
        return override
    v = get_setting("active_location_id")
    try:
        lid = int(v)
    except (TypeError, ValueError):
        lid = None
    # Honor the persisted default only if it still points at an ACTIVE store. If
    # that store was later archived (or the setting is missing/garbage), fall
    # through to the lowest active store so we never act on an archived tenant —
    # matching the header-override and MIN() branches, both of which filter
    # archived=0. A real id is always present (the seed creates DC/NYC).
    if lid is not None and get_db().execute(
            "SELECT 1 FROM locations WHERE id=? AND archived=0", (lid,)).fetchone():
        return lid
    row = get_db().execute("SELECT MIN(id) AS id FROM locations WHERE archived=0").fetchone()
    return row["id"] if row and row["id"] is not None else 1


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, "" if value is None else str(value)),
    )
    db.commit()


def set_setting_default(key, value):
    """Insert a setting only if the key is absent (atomic; concurrent callers
    converge on the first value written)."""
    db = get_db()
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
        (key, "" if value is None else str(value)),
    )
    db.commit()


def all_settings():
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# --- backups ----------------------------------------------------------------

def backup(keep=14):
    """Snapshot the live DB to data/backups/ledger-<stamp>.db and keep the most
    recent `keep`. Uses SQLite's online backup API, so it's safe to run against a
    live database. Returns the backup path, or None if there's no DB yet.

    The single ledger.db file is the only copy of everything, so this is the
    safety net against an accidental wipe."""
    if not os.path.exists(DB_PATH):
        return None
    bdir = os.path.join(os.path.dirname(DB_PATH), "backups")
    os.makedirs(bdir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = os.path.join(bdir, f"ledger-{stamp}.db")
    src = dst = None
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
    finally:
        if src:
            src.close()
        if dst:
            dst.close()
    for old in sorted(glob.glob(os.path.join(bdir, "ledger-*.db")))[:-keep]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dest
