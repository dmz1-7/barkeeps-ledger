"""Import MarginEdge CSV exports into Barkeep's Ledger.

Usage:
    python import_marginedge.py [DOWNLOADS_DIR]

Reads (from DOWNLOADS_DIR, default ~/Downloads on the Windows mount):
  * products.csv       -> products (inventory_items)
  * vendorItems.csv    -> vendor_items (+ vendors, linked to products)
  * categoryReport.csv -> invoices + per-category invoice lines

Re-runnable: products/vendors/vendor_items are upserted; invoices are cleared
and reloaded from the category report each run (re-export it with a wider date
range to bring in the full history, then run again).
"""
import csv
import os
import re
import sqlite3
import sys

import db
import money

DEFAULT_DIR = "/mnt/c/Users/dmkab/Downloads"


def _price(s):
    if not s:
        return None
    s = s.replace("$", "").replace(",", "").strip()
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def _yn(s):
    return 1 if (s or "").strip().lower() in ("yes", "y", "true", "1") else 0


def _iso(s):
    """Normalize MM/DD/YYYY or yyyy-mm-dd to ISO yyyy-mm-dd; pass through blanks."""
    s = (s or "").strip()
    if not s:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        mo, d, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return s


def _read(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


class Importer:
    def __init__(self, conn, location_id):
        self.c = conn
        self.loc = location_id
        self.cats = {r["name"].lower(): r["id"]
                     for r in conn.execute("SELECT id, name FROM categories")}

    def category_id(self, raw):
        """Resolve a MarginEdge category name to a category_id, handling split
        allocations ('A (80%), B (20%)' -> dominant) and auto-creating unknown
        categories under the 'Other' type."""
        raw = (raw or "").strip()
        if not raw:
            return None
        if "%" in raw:
            parts = re.findall(r"([^,(]+)\((\d+)%\)", raw)
            if parts:
                raw = max(parts, key=lambda p: int(p[1]))[0].strip()
        cid = self.cats.get(raw.lower())
        if cid is None:
            cur = self.c.execute(
                "INSERT INTO categories(name, category_type, sort_order) VALUES(?,?,?)",
                (raw, "Other", 999))
            cid = cur.lastrowid
            self.cats[raw.lower()] = cid
            print(f"    + new category (Other): {raw}")
        return cid

    def vendor_id(self, name):
        name = (name or "").strip()
        if not name:
            return None
        r = self.c.execute(
            "SELECT id FROM vendors WHERE location_id IS ? AND lower(name)=lower(?)",
            (self.loc, name)).fetchone()
        if r:
            return r["id"]
        cur = self.c.execute(
            "INSERT INTO vendors(location_id, name) VALUES(?,?)", (self.loc, name))
        return cur.lastrowid

    # --- clear only THIS location's data so other stores are untouched ---
    def clear_location(self):
        self.c.execute("DELETE FROM invoices WHERE location_id IS ?", (self.loc,))  # cascades items
        self.c.execute("DELETE FROM vendor_items WHERE location_id IS ?", (self.loc,))
        self.c.execute("DELETE FROM inventory_items WHERE location_id IS ?", (self.loc,))
        self.c.execute("DELETE FROM vendors WHERE location_id IS ?", (self.loc,))
        print(f"  cleared prior data for location {self.loc} (invoices, items, vendor_items, products, vendors)")

    def import_products(self, rows):
        for r in rows:
            name = (r.get("Name") or "").strip()
            if not name:
                continue
            cid = self.category_id(r.get("Category"))
            cat_name = next((k for k, v in self.cats.items() if v == cid), None)
            vals = (name, cat_name, cid, r.get("Report By Unit", "").strip(),
                    (r.get("Accounting Code") or "").strip(), _yn(r.get("On Inventory")),
                    _yn(r.get("Tax Exempt")), _price(r.get("Latest Price")) or 0)
            existing = self.c.execute(
                "SELECT id FROM inventory_items WHERE location_id IS ? AND lower(name)=lower(?)",
                (self.loc, name)).fetchone()
            if existing:
                self.c.execute(
                    "UPDATE inventory_items SET category=?, category_id=?, report_by_unit=?, "
                    "accounting_code=?, on_inventory=?, tax_exempt=?, unit_cost=? WHERE id=?",
                    (cat_name, cid, vals[3], vals[4], vals[5], vals[6], vals[7], existing["id"]))
            else:
                self.c.execute(
                    "INSERT INTO inventory_items(location_id, name, category, category_id, report_by_unit, "
                    "accounting_code, on_inventory, tax_exempt, unit_cost) VALUES(?,?,?,?,?,?,?,?,?)",
                    (self.loc,) + vals)
        print(f"  products: {self.c.execute('SELECT COUNT(*) FROM inventory_items WHERE location_id IS ?', (self.loc,)).fetchone()[0]}")

    def import_vendor_items(self, rows):
        self.c.execute("DELETE FROM vendor_items WHERE location_id IS ?", (self.loc,))
        prod = {r["name"].lower(): r["id"] for r in self.c.execute(
            "SELECT id, name FROM inventory_items WHERE location_id IS ?", (self.loc,))}
        for r in rows:
            name = (r.get("Vendor Item Name") or "").strip()
            if not name:
                continue
            vname = (r.get("Vendor") or "").strip()
            self.c.execute(
                "INSERT INTO vendor_items(location_id, vendor_id, vendor_name, vendor_item_name, product_id, "
                "category_id, item_code, last_purchase_date, last_purchase_price, order_guide, status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?, 'reviewed')",
                (self.loc, self.vendor_id(vname), vname, name,
                 prod.get((r.get("Product") or "").strip().lower()),
                 self.category_id(r.get("Category")), (r.get("Item Code") or "").strip(),
                 _iso(r.get("Last Purch Date")), _price(r.get("Last Purch $")),
                 _yn(r.get("Order Guide"))))
        c = lambda t: self.c.execute(f"SELECT COUNT(*) FROM {t} WHERE location_id IS ?", (self.loc,)).fetchone()[0]
        print(f"  vendor_items: {c('vendor_items')} | vendors: {c('vendors')}")

    def import_invoices(self, path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            header = [h.strip() for h in next(reader)]
            # Locate columns by NAME (exports vary, e.g. an extra "Sync Status").
            idx = {name: i for i, name in enumerate(header)}
            total_i = idx["Total"]
            date_i, num_i, vendor_i = idx["Invoice Date"], idx["Invoice #"], idx["Vendor"]
            status_i = idx.get("Status")
            cat_start = total_i + 1            # category columns follow Total
            cat_cols = header[cat_start:]
            cat_ids = [self.category_id(c) for c in cat_cols]
            self.c.execute("DELETE FROM invoices WHERE location_id IS ?", (self.loc,))
            n_inv = n_line = 0
            for row in reader:
                if not any(row):
                    continue
                status = row[status_i] if status_i is not None else "closed"
                cur = self.c.execute(
                    "INSERT INTO invoices(location_id, vendor, invoice_date, invoice_number, status, total) "
                    "VALUES(?,?,?,?,?,?)",
                    (self.loc, row[vendor_i].strip(), _iso(row[date_i]), row[num_i].strip(),
                     (status or "closed").strip().lower(), money.normalize(_price(row[total_i]))))
                inv_id = cur.lastrowid
                n_inv += 1
                for amt_s, cid in zip(row[cat_start:], cat_ids):
                    amt = money.normalize(_price(amt_s))
                    if amt:
                        self.c.execute(
                            "INSERT INTO invoice_items(invoice_id, name, total, category_id) "
                            "VALUES(?,?,?,?)",
                            (inv_id, next((k for k, v in self.cats.items() if v == cid), "item"),
                             amt, cid))
                        n_line += 1
        print(f"  invoices: {n_inv} | category lines: {n_line}")


def _resolve(dirpath, override, *patterns):
    """Find a CSV by override path or case-insensitive glob (newest match),
    so browser-renamed variants like 'vendorItems (1).csv' still resolve."""
    import glob
    if override:
        return override
    listing = os.listdir(dirpath)
    for pat in patterns:
        rx = re.compile(pat, re.IGNORECASE)
        matches = [os.path.join(dirpath, n) for n in listing if rx.match(n)]
        if matches:
            return max(matches, key=os.path.getmtime)
    sys.exit(f"Could not find a file matching {patterns} in {dirpath}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Import MarginEdge CSVs into a store.")
    ap.add_argument("downloads_dir", nargs="?", default=DEFAULT_DIR)
    ap.add_argument("--location", default="Pubkey DC",
                    help="Store name to import into (must exist in the locations table).")
    ap.add_argument("--products", help="Path to the products CSV (else auto-detect).")
    ap.add_argument("--vendor-items", dest="vendor_items", help="Path to the vendor items CSV.")
    ap.add_argument("--category-report", dest="category_report", help="Path to the category report CSV.")
    args = ap.parse_args()

    d = args.downloads_dir
    products = _resolve(d, args.products, r"products.*\.csv$")
    vendor_items = _resolve(d, args.vendor_items, r"vendor.?items.*\.csv$")
    category_report = _resolve(d, args.category_report, r"category.?report.*\.csv$")

    db.init_db()
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    loc = conn.execute("SELECT id FROM locations WHERE name=?", (args.location,)).fetchone()
    if not loc:
        names = [r["name"] for r in conn.execute("SELECT name FROM locations")]
        sys.exit(f"Unknown location {args.location!r}. Known: {names}")

    imp = Importer(conn, loc["id"])
    print(f"Importing into '{args.location}' (id {loc['id']})")
    print(f"  products:        {os.path.basename(products)}")
    print(f"  vendor items:    {os.path.basename(vendor_items)}")
    print(f"  category report: {os.path.basename(category_report)}")
    imp.clear_location()
    imp.import_products(_read(products))
    imp.import_vendor_items(_read(vendor_items))
    imp.import_invoices(category_report)
    conn.commit()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
