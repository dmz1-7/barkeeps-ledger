"""Performance reports for Barkeep's Ledger.

Four reports, all built on the two-level category taxonomy (see db.TAXONOMY):

  * category_report   — invoices as rows, spend-by-category as columns (a pivot)
  * controllable_pl   — Income (Square sales x per-period mix), COGS by
                        category, Labor; Gross & Controllable Profit
  * sales_report      — weekly grid (this/last week, last year) + PTD/YTD
  * price_movers      — products whose unit cost moved, ranked by $ impact

COGS comes from logged invoice lines (always available). Income, labor, and the
Sales report depend on Square; without it they fail soft to zeros + an error.
"""
import datetime as dt

from db import get_db, TAXONOMY, active_location_id
import square_client

CATEGORY_TYPES = list(TAXONOMY.keys())  # Food, Beer, Wine, Liquor, N/A Bev, Other


def _r(n):
    return round(n or 0, 2)


# --- Category Report --------------------------------------------------------

def category_report(start, end, vendor=None, status=None, search=None):
    """Pivot: each invoice is a row; each category is a column of spend."""
    db = get_db()
    cats = db.execute(
        "SELECT id, name, category_type, sort_order FROM categories "
        "WHERE archived=0 ORDER BY sort_order"
    ).fetchall()

    where = ["location_id IS ?", "invoice_date >= ?", "invoice_date <= ?"]
    params = [active_location_id(), start.isoformat(), end.isoformat()]
    if vendor:
        where.append("lower(vendor) = lower(?)")
        params.append(vendor)
    if status:
        where.append("status = ?")
        params.append(status)
    if search:
        where.append("(vendor LIKE ? OR invoice_number LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    invs = db.execute(
        "SELECT id, invoice_date, invoice_number, vendor, status, total "
        f"FROM invoices WHERE {' AND '.join(where)} "
        "ORDER BY invoice_date DESC, id DESC",
        params,
    ).fetchall()

    inv_ids = [r["id"] for r in invs]
    cell = {}
    if inv_ids:
        marks = ",".join("?" * len(inv_ids))
        for r in db.execute(
            f"SELECT invoice_id, category_id, COALESCE(SUM(total),0) AS amt "
            f"FROM invoice_items WHERE invoice_id IN ({marks}) "
            f"GROUP BY invoice_id, category_id",
            inv_ids,
        ):
            cell[(r["invoice_id"], r["category_id"])] = _r(r["amt"])

    col_totals = {c["id"]: 0.0 for c in cats}
    rows = []
    for inv in invs:
        cells = {}
        for c in cats:
            amt = cell.get((inv["id"], c["id"]), 0.0)
            cells[c["id"]] = amt
            col_totals[c["id"]] += amt
        rows.append({**dict(inv), "cells": cells})

    return {
        "categories": [dict(c) for c in cats],
        "rows": rows,
        "column_totals": {k: _r(v) for k, v in col_totals.items()},
        "grand_total": _r(sum(col_totals.values())),
    }


# --- Controllable P&L -------------------------------------------------------

def controllable_pl(start, end):
    db = get_db()
    sales_info = square_client.get_sales(start, end)
    labor_info = square_client.get_labor(start, end)
    sales = sales_info["sales"]
    labor = labor_info["labor"]

    loc = active_location_id()
    mix = {
        r["category_type"]: r["pct"]
        for r in db.execute(
            "SELECT category_type, pct FROM sales_mix "
            "WHERE location_id=? AND period_start=? AND period_end=?",
            (loc, start.isoformat(), end.isoformat()),
        )
    }

    # Income by category type = total sales x that type's mix %.
    income = []
    income_by_type = {}
    for t in CATEGORY_TYPES:
        amt = _r(sales * mix.get(t, 0) / 100.0)
        income_by_type[t] = amt
        income.append({
            "category_type": t,
            "amt": amt,
            "pct_of_sales": _r(amt / sales * 100) if sales else None,
        })
    total_income = _r(sales)

    # COGS by category type -> category, from invoice lines in the period.
    cogs_rows = db.execute(
        "SELECT c.category_type AS ctype, c.name AS category, "
        "       COALESCE(SUM(ii.total),0) AS amt "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN categories c ON c.id = ii.category_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ? "
        "GROUP BY c.category_type, c.name",
        (loc, start.isoformat(), end.isoformat()),
    ).fetchall()

    by_type = {}
    for r in cogs_rows:
        ctype = r["ctype"] or "Uncategorized"
        by_type.setdefault(ctype, {"total": 0.0, "categories": []})
        by_type[ctype]["total"] += r["amt"] or 0
        by_type[ctype]["categories"].append({
            "category": r["category"] or "Uncategorized",
            "amt": _r(r["amt"]),
        })

    ordered_types = CATEGORY_TYPES + [t for t in by_type if t not in CATEGORY_TYPES]
    cogs = []
    total_cogs = 0.0
    for t in ordered_types:
        if t not in by_type:
            continue
        type_income = income_by_type.get(t, 0)
        type_total = _r(by_type[t]["total"])
        total_cogs += type_total
        for c in by_type[t]["categories"]:
            c["pct"] = _r(c["amt"] / type_income * 100) if type_income else None
        cogs.append({
            "category_type": t,
            "type_total": type_total,
            "type_pct": _r(type_total / type_income * 100) if type_income else None,
            "categories": sorted(by_type[t]["categories"], key=lambda x: -x["amt"]),
        })

    total_cogs = _r(total_cogs)
    gross = _r(total_income - total_cogs)
    controllable = _r(gross - labor)

    def pct(part):
        return _r(part / total_income * 100) if total_income else None

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "sales": sales,
        "income": income,
        "total_income": total_income,
        "cogs": cogs,
        "total_cogs": total_cogs,
        "total_cogs_pct": pct(total_cogs),
        "gross_profit": gross,
        "gross_pct": pct(gross),
        "expenses": [{"name": "Labor", "amt": labor, "pct": pct(labor)}],
        "total_expenses": labor,
        "controllable_profit": controllable,
        "controllable_pct": pct(controllable),
        "labor_hours": labor_info.get("hours", 0),
        "mix_set": bool(mix),
        "square_configured": square_client.is_configured(),
        "sales_error": sales_info.get("error"),
        "labor_error": labor_info.get("error"),
    }


# --- Sales report -----------------------------------------------------------

def _week_start(d):
    return d - dt.timedelta(days=d.weekday())  # Monday


def sales_report(today=None):
    today = today or dt.date.today()
    configured = square_client.is_configured()
    this_mon = _week_start(today)
    last_mon = this_mon - dt.timedelta(days=7)
    ly_mon = this_mon - dt.timedelta(days=364)  # same weekday a year back

    def daily(week_start):
        wk_end = week_start + dt.timedelta(days=6)
        return square_client.get_daily_sales(week_start, wk_end)

    this_week = daily(this_mon)
    last_week = daily(last_mon)
    last_year = daily(ly_mon)

    days = []
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    tw_tot = lw_tot = ly_tot = 0.0
    for i, label in enumerate(labels):
        d_tw = this_mon + dt.timedelta(days=i)
        tw = this_week.get(d_tw.isoformat(), 0.0) if d_tw <= today else None
        lw = last_week.get((last_mon + dt.timedelta(days=i)).isoformat(), 0.0)
        ly = last_year.get((ly_mon + dt.timedelta(days=i)).isoformat(), 0.0)
        if tw:
            tw_tot += tw
        lw_tot += lw
        ly_tot += ly
        days.append({"day": label, "this_week": tw, "last_week": _r(lw), "last_year": _r(ly)})

    # Period-to-date (this month) and Year-to-date.
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    ptd = square_client.get_sales(month_start, today)["sales"]
    ytd = square_client.get_sales(year_start, today)["sales"]

    return {
        "week_of": this_mon.isoformat(),
        "days": days,
        "totals": {"this_week": _r(tw_tot), "last_week": _r(lw_tot), "last_year": _r(ly_tot)},
        "period_to_date": _r(ptd),
        "year_to_date": _r(ytd),
        "square_configured": configured,
    }


# --- Price Movers -----------------------------------------------------------

def price_movers(start, end):
    """Items whose unit cost moved between the prior price and the latest price
    in the window, ranked by dollar impact (delta x qty bought in window)."""
    db = get_db()
    loc = active_location_id()
    # Latest price + qty within the window, per item name.
    in_window = db.execute(
        "SELECT ii.name AS name, ii.category_id AS category_id, "
        "       SUM(ii.qty) AS qty, "
        "       (SELECT unit_cost FROM invoice_items x JOIN invoices xi ON xi.id=x.invoice_id "
        "        WHERE x.name = ii.name AND xi.location_id IS ? "
        "          AND xi.invoice_date >= ? AND xi.invoice_date <= ? "
        "        ORDER BY xi.invoice_date DESC, x.id DESC LIMIT 1) AS new_price "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ? "
        "  AND ii.name IS NOT NULL AND TRIM(ii.name) <> '' "
        "GROUP BY ii.name",
        (loc, start.isoformat(), end.isoformat(), loc, start.isoformat(), end.isoformat()),
    ).fetchall()

    cat_names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM categories")}
    movers = []
    for r in in_window:
        prior = db.execute(
            "SELECT unit_cost FROM invoice_items x JOIN invoices xi ON xi.id=x.invoice_id "
            "WHERE x.name=? AND xi.location_id IS ? AND xi.invoice_date < ? AND x.unit_cost IS NOT NULL "
            "ORDER BY xi.invoice_date DESC, x.id DESC LIMIT 1",
            (r["name"], loc, start.isoformat()),
        ).fetchone()
        old_price = prior["unit_cost"] if prior else None
        new_price = r["new_price"]
        if old_price is None or new_price is None or old_price == new_price:
            continue
        qty = r["qty"] or 0
        impact = _r((new_price - old_price) * qty)
        movers.append({
            "name": r["name"],
            "category": cat_names.get(r["category_id"], "Uncategorized"),
            "old_price": _r(old_price),
            "new_price": _r(new_price),
            "change_pct": _r((new_price - old_price) / old_price * 100) if old_price else None,
            "qty": _r(qty),
            "impact": impact,
        })

    movers.sort(key=lambda m: -abs(m["impact"]))
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "movers": movers,
        "total_impact": _r(sum(m["impact"] for m in movers)),
    }
