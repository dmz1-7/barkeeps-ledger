"""CSV exports for bookkeeping.

Each builder returns a CSV string for the ACTIVE store (db.active_location_id())
over a date range, built from the same invoice-line data the reports use. Money
is emitted as plain numbers (not $-formatted) so the file drops straight into a
spreadsheet; category totals are summed exactly via money.sum_dollars.
"""
import csv
import io

import cogs
import money
import recipes
import reports
from db import get_db, active_location_id


def _safe(cell):
    """Neutralize spreadsheet formula injection: a text cell whose first
    non-whitespace/quote character is =, +, -, or @ is evaluated as a formula by
    Excel/Sheets, and vendor/item/recipe names are free text (some AI-parsed).
    Prefix such cells with an apostrophe so they import as literal text."""
    # Test the value with leading whitespace/quotes stripped — Excel ignores
    # those before deciding a cell is a formula.
    if isinstance(cell, str) and cell.lstrip(" \t\r\n\"'")[:1] in ("=", "+", "-", "@"):
        return "'" + cell
    return cell


def _csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows([_safe(c) for c in row] for row in rows)
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
    """Spend by Category Type -> Category for the range, with a grand-total row.

    Deflated to the PRE-TAX line basis (via cogs._pretax_line_rows) so this export
    ties out exactly to the on-screen Category Report and the Controllable P&L for
    tax-inclusive vendors, instead of carrying the tax in the category spend."""
    rows = get_db().execute(
        "SELECT inv.id AS iid, inv.subtotal AS sub, inv.total AS tot, inv.tax AS tax, "
        "       c.category_type AS ctype, c.name AS category, ii.total AS amt "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN categories c ON c.id = ii.category_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ?",
        (active_location_id(), start.isoformat(), end.isoformat()),
    ).fetchall()
    cents = {}   # (ctype, category) -> integer cents
    for r, amt in cogs._pretax_line_rows(rows):
        key = (r["ctype"] or "Uncategorized", r["category"] or "Uncategorized")
        cents[key] = cents.get(key, 0) + money.to_cents(amt)
    out = [(ctype, cat, round(c / 100.0, 2)) for (ctype, cat), c in sorted(cents.items())]
    out.append(("", "TOTAL", round(sum(cents.values()) / 100.0, 2)))
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


def recipes_csv():
    """Menu costing sheet: one row per recipe with cost, cost%, and margin."""
    out = [
        (r["name"], r["menu_price"], r["yield_qty"], r["batch_cost"],
         r["cost_per_serving"], r["cost_pct"], r["margin"])
        for r in recipes.list_costed()
    ]
    return _csv(
        ["Recipe", "Menu Price", "Yield", "Batch Cost", "Cost/Serving",
         "Cost %", "Margin"],
        out,
    )
