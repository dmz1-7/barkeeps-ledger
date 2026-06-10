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
    def __init__(self, conn):
        self.c = conn
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
        r = self.c.execute("SELECT id FROM vendors WHERE lower(name)=lower(?)", (name,)).fetchone()
        if r:
            return r["id"]
        cur = self.c.execute("INSERT INTO vendors(name) VALUES(?)", (name,))
        return cur.lastrowid

    # --- clear the throwaway sample rows so the import is clean ---
    def clear_samples(self):
        self.c.execute("DELETE FROM invoices")           # cascades invoice_items
        self.c.execute("DELETE FROM invoice_items")
        self.c.execute("DELETE FROM vendor_items")
        self.c.execute("DELETE FROM inventory_items")
        self.c.execute("DELETE FROM vendors")
        print("  cleared sample/prior data (invoices, items, vendor_items, products, vendors)")

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
            existing = self.c.execute("SELECT id FROM inventory_items WHERE lower(name)=lower(?)", (name,)).fetchone()
            if existing:
                self.c.execute(
                    "UPDATE inventory_items SET category=?, category_id=?, report_by_unit=?, "
                    "accounting_code=?, on_inventory=?, tax_exempt=?, unit_cost=? WHERE id=?",
                    (cat_name, cid, vals[3], vals[4], vals[5], vals[6], vals[7], existing["id"]))
            else:
                self.c.execute(
                    "INSERT INTO inventory_items(name, category, category_id, report_by_unit, "
                    "accounting_code, on_inventory, tax_exempt, unit_cost) VALUES(?,?,?,?,?,?,?,?)",
                    vals)
        print(f"  products: {self.c.execute('SELECT COUNT(*) FROM inventory_items').fetchone()[0]}")

    def import_vendor_items(self, rows):
        self.c.execute("DELETE FROM vendor_items")
        prod = {r["name"].lower(): r["id"]
                for r in self.c.execute("SELECT id, name FROM inventory_items")}
        for r in rows:
            name = (r.get("Vendor Item Name") or "").strip()
            if not name:
                continue
            vname = (r.get("Vendor") or "").strip()
            self.c.execute(
                "INSERT INTO vendor_items(vendor_id, vendor_name, vendor_item_name, product_id, "
                "category_id, item_code, last_purchase_date, last_purchase_price, order_guide, status) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'reviewed')",
                (self.vendor_id(vname), vname, name,
                 prod.get((r.get("Product") or "").strip().lower()),
                 self.category_id(r.get("Category")), (r.get("Item Code") or "").strip(),
                 _iso(r.get("Last Purch Date")), _price(r.get("Last Purch $")),
                 _yn(r.get("Order Guide"))))
        print(f"  vendor_items: {self.c.execute('SELECT COUNT(*) FROM vendor_items').fetchone()[0]}"
              f" | vendors: {self.c.execute('SELECT COUNT(*) FROM vendors').fetchone()[0]}")

    def import_invoices(self, path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            cat_cols = header[5:]               # category columns start after Total
            cat_ids = [self.category_id(c) for c in cat_cols]
            self.c.execute("DELETE FROM invoices")
            self.c.execute("DELETE FROM invoice_items")
            n_inv = n_line = 0
            for row in reader:
                if not any(row):
                    continue
                date, num, vendor, status, total = row[0], row[1], row[2], row[3], row[4]
                cur = self.c.execute(
                    "INSERT INTO invoices(vendor, invoice_date, invoice_number, status, total) "
                    "VALUES(?,?,?,?,?)",
                    (vendor.strip(), _iso(date), num.strip(), (status or "closed").strip().lower(),
                     _price(total)))
                inv_id = cur.lastrowid
                n_inv += 1
                for amt_s, cid in zip(row[5:], cat_ids):
                    amt = _price(amt_s)
                    if amt:
                        self.c.execute(
                            "INSERT INTO invoice_items(invoice_id, name, total, category_id) "
                            "VALUES(?,?,?,?)",
                            (inv_id, next((k for k, v in self.cats.items() if v == cid), "item"),
                             amt, cid))
                        n_line += 1
        print(f"  invoices: {n_inv} | category lines: {n_line}")


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR
    db.init_db()
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    imp = Importer(conn)
    print("Importing MarginEdge data from", d)
    imp.clear_samples()
    imp.import_products(_read(os.path.join(d, "products.csv")))
    imp.import_vendor_items(_read(os.path.join(d, "vendorItems.csv")))
    imp.import_invoices(os.path.join(d, "categoryReport.csv"))
    conn.commit()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
