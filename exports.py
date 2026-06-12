"""CSV exports for bookkeeping.

Each builder returns a CSV string for the ACTIVE store (db.active_location_id())
over a date range, built from the same invoice-line data the reports use. Money
is emitted as plain numbers (not $-formatted) so the file drops straight into a
spreadsheet; category totals are summed exactly via money.sum_dollars.
"""
import csv
import io

import money
import reports
from db import get_db, active_location_id


def _csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def purchases_csv(start, end):
    """One row per invoice line item in the range — the transaction ledger a
    bookkeeper wants: date, vendor, invoice #, category, item, qty, cost, total."""
    db = get_db()
    rows = db.execute(
        "SELECT inv.invoice_date AS d, inv.vendor AS vendor, inv.invoice_number AS num, "
        "       inv.status AS status, c.category_type AS ctype, c.name AS category, "
        "       ii.name AS item, ii.qty AS qty, ii.unit AS unit, "
        "       ii.unit_cost AS unit_cost, ii.total AS total "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN categories c ON c.id = ii.category_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ? "
        "ORDER BY inv.invoice_date DESC, inv.id DESC, ii.id ASC",
        (active_location_id(), start.isoformat(), end.isoformat()),
    ).fetchall()
    out = [
        (r["d"], r["vendor"], r["num"], r["status"], r["ctype"] or "",
         r["category"] or "Uncategorized", r["item"], r["qty"], r["unit"],
         r["unit_cost"], r["total"])
        for r in rows
    ]
    return _csv(
        ["Invoice Date", "Vendor", "Invoice #", "Status", "Category Type",
         "Category", "Item", "Qty", "Unit", "Unit Cost", "Total"],
        out,
    )


def category_summary_csv(start, end):
    """Spend by Category Type -> Category for the range, with a grand-total row."""
    db = get_db()
    rows = db.execute(
        "SELECT c.category_type AS ctype, c.name AS category, "
        "       COALESCE(SUM(ii.total), 0) AS amt "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN categories c ON c.id = ii.category_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ? "
        "GROUP BY c.category_type, c.name "
        "ORDER BY c.category_type, c.name",
        (active_location_id(), start.isoformat(), end.isoformat()),
    ).fetchall()
    out = [(r["ctype"] or "Uncategorized", r["category"] or "Uncategorized",
            money.normalize(r["amt"]) or 0) for r in rows]
    out.append(("", "TOTAL", money.sum_dollars(r["amt"] for r in rows)))
    return _csv(["Category Type", "Category", "Total"], out)


def order_guide_csv():
    """The vendor-grouped order guide flattened to CSV: each item, a SUBTOTAL row
    per vendor, and a final TOTAL — a ready-to-send order sheet per distributor."""
    guide = reports.order_guide()
    out = []
    for v in guide["vendors"]:
        for it in v["items"]:
            out.append((v["vendor"], it["name"], it["unit"], it["par"],
                        it["on_hand"], it["order_qty"], it["unit_cost"], it["line_cost"]))
        out.append((v["vendor"], "SUBTOTAL", "", "", "", "", "", v["subtotal"]))
    out.append(("TOTAL", "", "", "", "", "", "", guide["grand_total"]))
    return _csv(
        ["Vendor", "Item", "Unit", "Par", "On Hand", "Order Qty", "Unit Cost", "Line Cost"],
        out,
    )
