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

CREATE TABLE IF NOT EXISTS invoices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor         TEXT,
    invoice_date   TEXT,             -- ISO yyyy-mm-dd
    invoice_number TEXT,
    category       TEXT,             -- food | liquor | beer | wine | na_beverage | supplies | other
    subtotal       REAL,
    tax            REAL,
    total          REAL,
    image_path     TEXT,             -- relative filename under uploads/
    notes          TEXT,
    raw_json       TEXT,             -- the AI parse result, for audit
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
    inventory_item_id INTEGER REFERENCES inventory_items(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS vendors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
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

CREATE TABLE IF NOT EXISTS inventory_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    category    TEXT,               -- liquor | beer | wine | food | na_beverage | supplies | other
    unit        TEXT,               -- bottle | case | keg | lb | each ...
    par_level   REAL DEFAULT 0,
    last_count  REAL DEFAULT 0,
    unit_cost   REAL DEFAULT 0,
    vendor      TEXT,
    sort_order  INTEGER DEFAULT 0,
    archived    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS counts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
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

CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_items_invoice ON invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_countlines_count ON count_lines(count_id);
"""

DEFAULT_SETTINGS = {
    "target_cogs_pct": "30",      # % of sales
    "target_labor_pct": "25",     # % of sales
    "square_env": "production",   # production | sandbox
    "square_version": "2025-01-23",
    "ai_model": os.environ.get("LEDGER_AI_MODEL", "claude-opus-4-8"),
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


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # Seed any default settings not already present, and pull secrets from env.
    env_seed = {
        "square_token": os.environ.get("SQUARE_ACCESS_TOKEN", ""),
        "square_location_id": os.environ.get("SQUARE_LOCATION_ID", ""),
    }
    for key, val in {**DEFAULT_SETTINGS, **env_seed}.items():
        row = conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO settings(key, value) VALUES(?,?)", (key, val))
    conn.commit()
    conn.close()


# --- settings helpers -------------------------------------------------------

def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


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
