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
import money
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

    # id 0 = an "Uncategorized" column for line items with category_id NULL, so
    # their spend is counted in column_totals/grand_total (otherwise the Category
    # Report's grand_total silently undershoots the invoice totals and disagrees
    # with the Controllable P&L, which DOES include uncategorized cost).
    col_ids = [c["id"] for c in cats] + [0]
    col_totals = {cid: 0.0 for cid in col_ids}
    rows = []
    for inv in invs:
        cells = {}
        for cid in col_ids:
            amt = cell.get((inv["id"], None if cid == 0 else cid), 0.0)
            cells[cid] = amt
            col_totals[cid] += amt
        rows.append({**dict(inv), "cells": cells})

    cats_out = [dict(c) for c in cats] + [
        {"id": 0, "name": "Uncategorized", "category_type": "Uncategorized", "sort_order": 9999}]
    return {
        "categories": cats_out,
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
        "labor_warning": labor_info.get("warning"),
        "unwaged_hours": labor_info.get("unwaged_hours", 0),
        "unwaged_shifts": labor_info.get("unwaged_shifts", 0),
    }


# --- Sales report -----------------------------------------------------------

def _week_start(d):
    return d - dt.timedelta(days=d.weekday())  # Monday


def sales_report(today=None):
    today = today or square_client.business_today()
    configured = square_client.is_configured()
    this_mon = _week_start(today)
    last_mon = this_mon - dt.timedelta(days=7)
    ly_mon = this_mon - dt.timedelta(days=364)  # same weekday a year back
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    # One cached pull covers the weekly grids (incl. last year), PTD and YTD.
    full_start = min(ly_mon, year_start, last_mon)
    sales = square_client.daily_sales_cached(full_start, today)

    def g(d):
        return sales.get(d.isoformat(), 0.0)

    def rng(a, b):
        s, d = 0.0, a
        while d <= b:
            s += sales.get(d.isoformat(), 0.0)
            d += dt.timedelta(days=1)
        return s

    days = []
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    tw_tot = lw_tot = ly_tot = 0.0
    for i, label in enumerate(labels):
        d_tw = this_mon + dt.timedelta(days=i)
        tw = g(d_tw) if d_tw <= today else None
        lw = g(last_mon + dt.timedelta(days=i))
        ly = g(ly_mon + dt.timedelta(days=i))
        if tw:
            tw_tot += tw
        lw_tot += lw
        ly_tot += ly
        days.append({"day": label, "this_week": tw, "last_week": _r(lw), "last_year": _r(ly)})

    ptd = rng(month_start, today)
    ytd = rng(year_start, today)

    return {
        "week_of": this_mon.isoformat(),
        "days": days,
        "totals": {"this_week": _r(tw_tot), "last_week": _r(lw_tot), "last_year": _r(ly_tot)},
        "period_to_date": _r(ptd),
        "year_to_date": _r(ytd),
        "square_configured": configured,
    }


# --- Price Movers -----------------------------------------------------------

#   A line's stable SKU identity = (vendor, vendor-item name). It's derivable
#   whether or not the line is currently linked to a vendor_item, because
#   vendor_items are keyed on exactly those fields — so re-running the importer
#   (which deletes vendor_items and nulls invoice_items.vendor_item_id) doesn't
#   change the identity, and a product can't split across "linked" and "unlinked"
#   groups. It also never merges across vendors. We deliberately key on the vendor
#   item name (not the raw line name + unit), since that IS the model's notion of a
#   distinct pack/SKU. (A vendor_item renamed across the window/prior boundary is
#   intentionally treated as a new identity — that move won't be reported.)
#   NULLIF(...,'') so a blank vendor_item field falls back to the invoice line too,
#   keeping linked and unlinked lines for the same product on the same key.
_PM_VENDOR = "lower(TRIM(COALESCE(NULLIF(TRIM({vi}.vendor_name), ''), {inv}.vendor, '')))"
_PM_NAME = "lower(TRIM(COALESCE(NULLIF(TRIM({vi}.vendor_item_name), ''), {ii}.name, '')))"


def price_movers(start, end):
    """Items whose unit cost moved between the prior price and the latest price in
    the window, ranked by dollar impact (delta x qty bought in window)."""
    db = get_db()
    loc = active_location_id()
    vkey = _PM_VENDOR.format(vi="vi", inv="inv")
    nkey = _PM_NAME.format(vi="vi", ii="ii")
    rows = db.execute(
        f"SELECT {vkey} AS vkey, {nkey} AS nkey, "
        "       COALESCE(vi.vendor_name, inv.vendor) AS vendor, "
        "       COALESCE(vi.vendor_item_name, ii.name) AS name, "
        "       ii.unit_cost AS price, ii.qty AS qty, ii.category_id AS cat "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN vendor_items vi ON vi.id = ii.vendor_item_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ? "
        "  AND ii.unit_cost > 0 "   # only positive unit costs are real prices; credit/return lines aren't
        "  AND TRIM(COALESCE(vi.vendor_item_name, ii.name, '')) <> '' "
        "ORDER BY inv.invoice_date DESC, ii.id DESC",   # newest first -> first price seen is latest
        (loc, start.isoformat(), end.isoformat()),
    ).fetchall()

    groups = {}
    for r in rows:
        g = groups.get((r["vkey"], r["nkey"]))
        if g is None:
            groups[(r["vkey"], r["nkey"])] = g = {
                "vendor": r["vendor"], "name": r["name"], "cat": r["cat"],
                "new_price": None, "qty": 0.0,
            }
        if g["new_price"] is None and r["price"] is not None:
            g["new_price"] = r["price"]      # newest-first, so this is the latest price
        # Impact = price delta x qty bought AT the new price; only count units at
        # the latest price so units bought earlier in the window at the old price
        # don't get charged the full delta (which overstated impact).
        if r["price"] == g["new_price"]:
            g["qty"] += (r["qty"] or 0)

    pvkey = _PM_VENDOR.format(vi="v", inv="xi")
    pnkey = _PM_NAME.format(vi="v", ii="x")
    cat_names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM categories")}
    movers = []
    for (gv, gn), g in groups.items():
        new_price, qty = g["new_price"], (g["qty"] or 0)
        if new_price is None or not qty:     # no price or nothing bought -> no signal
            continue
        prior = db.execute(
            "SELECT x.unit_cost AS p FROM invoice_items x JOIN invoices xi ON xi.id=x.invoice_id "
            "LEFT JOIN vendor_items v ON v.id = x.vendor_item_id "
            "WHERE xi.location_id IS ? AND xi.invoice_date < ? AND x.unit_cost > 0 "
            f"  AND {pvkey} = ? AND {pnkey} = ? "
            "ORDER BY xi.invoice_date DESC, x.id DESC LIMIT 1",
            (loc, start.isoformat(), gv, gn),
        ).fetchone()
        old_price = prior["p"] if prior else None
        if old_price is None or old_price == new_price:
            continue
        movers.append({
            "name": g["name"],
            "category": cat_names.get(g["cat"], "Uncategorized"),
            "old_price": _r(old_price),
            "new_price": _r(new_price),
            "change_pct": _r((new_price - old_price) / old_price * 100) if old_price else None,
            "qty": _r(qty),
            "impact": _r((new_price - old_price) * qty),
        })

    movers.sort(key=lambda m: -abs(m["impact"]))
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "movers": movers,
        "total_impact": money.sum_dollars(m["impact"] for m in movers),
    }


def price_alerts(lookback_days=30, min_pct=10.0):
    """PROACTIVE price-increase alerts: vendor items whose MOST RECENT purchase
    price jumped at least `min_pct` above the prior (different) price, where that
    latest purchase happened within `lookback_days`. This is the push counterpart
    to price_movers (which is window/pull driven): it answers "what am I quietly
    paying more for right now?" without the user picking a date range.

    Keyed on the same stable (vendor, vendor-item name) SKU identity as
    price_movers, so it survives the importer's vendor_item wipe and never merges
    across vendors or packs. Increases only — drops aren't an alert."""
    db = get_db()
    loc = active_location_id()
    cutoff = (square_client.business_today() - dt.timedelta(days=lookback_days)).isoformat()
    vkey = _PM_VENDOR.format(vi="vi", inv="inv")
    nkey = _PM_NAME.format(vi="vi", ii="ii")
    rows = db.execute(
        f"SELECT {vkey} AS vkey, {nkey} AS nkey, "
        "       COALESCE(vi.vendor_name, inv.vendor) AS vendor, "
        "       COALESCE(vi.vendor_item_name, ii.name) AS name, "
        "       ii.unit_cost AS price, ii.qty AS qty, ii.category_id AS cat, "
        "       inv.invoice_date AS d "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN vendor_items vi ON vi.id = ii.vendor_item_id "
        "WHERE inv.location_id IS ? AND ii.unit_cost > 0 "   # credit/return lines aren't price signals
        "  AND inv.invoice_date IS NOT NULL AND TRIM(inv.invoice_date) <> '' "
        "  AND TRIM(COALESCE(vi.vendor_item_name, ii.name, '')) <> '' "
        "ORDER BY inv.invoice_date DESC, ii.id DESC",   # newest first
        (loc,),
    ).fetchall()

    # Per SKU, walk newest-first: the first row is the latest price; accumulate
    # the qty bought at that latest price/date; the first OLDER row at a different
    # price is the prior price (the most recent actual change). Once we've found
    # that prior price the group is resolved and older rows are ignored.
    groups = {}
    for r in rows:
        key = (r["vkey"], r["nkey"])
        g = groups.get(key)
        if g is None:
            groups[key] = g = {
                "vendor": r["vendor"], "name": r["name"], "cat": r["cat"],
                "new_price": r["price"], "new_date": r["d"], "new_qty": 0.0,
                "old_price": None, "ambiguous": False, "done": False,
            }
        if g["done"]:
            continue
        if r["d"] == g["new_date"]:
            # Still on the latest purchase date (rows are date-desc, so the
            # latest-date rows are contiguous and come first). A second line on
            # that same date at a DIFFERENT price is an intra-day correction or a
            # second SKU collapsing onto this key, so the current price is
            # ambiguous — suppress rather than risk a phantom hike.
            if r["price"] == g["new_price"]:
                g["new_qty"] += (r["qty"] or 0)
            else:
                g["ambiguous"] = True
        elif r["price"] != g["new_price"]:
            g["old_price"] = r["price"]   # most recent EARLIER-dated price that differs
            g["done"] = True
        # else: earlier date at the same price -> no change yet, keep scanning.

    cat_names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM categories")}
    alerts = []
    for g in groups.values():
        if g["ambiguous"]:                    # mixed prices on the latest date
            continue
        new, old = g["new_price"], g["old_price"]
        if old is None or new is None or old <= 0:
            continue
        if g["new_date"] < cutoff:            # latest purchase isn't recent -> stale
            continue
        if new <= old:                        # increases only
            continue
        pct = (new - old) / old * 100
        if pct < min_pct:                     # below the alert threshold
            continue
        alerts.append({
            "name": g["name"],
            "vendor": g["vendor"],
            "category": cat_names.get(g["cat"], "Uncategorized"),
            "old_price": _r(old),
            "new_price": _r(new),
            "change_pct": _r(pct),
            "last_date": g["new_date"],
            "qty": _r(g["new_qty"]),
            "impact": _r((new - old) * (g["new_qty"] or 0)),
        })

    alerts.sort(key=lambda a: -a["change_pct"])
    return {
        "lookback_days": lookback_days,
        "min_pct": min_pct,
        "count": len(alerts),
        "alerts": alerts,
    }


# --- Order guide ------------------------------------------------------------

def order_guide():
    """Products strictly below par for the active store, GROUPED BY VENDOR with a
    suggested order quantity (par - on hand) and line cost — so the owner places
    one order per distributor instead of reading a flat list. Vendors keep their
    first-seen order (the query sorts by vendor, then name); a blank vendor is
    bucketed as 'Unassigned'. Subtotals/total are summed exactly via money."""
    db = get_db()
    rows = db.execute(
        "SELECT name, COALESCE(NULLIF(TRIM(vendor), ''), 'Unassigned') AS vendor, "
        "       unit, par_level, last_count, unit_cost "
        "FROM inventory_items "
        "WHERE archived = 0 AND location_id IS ? AND par_level > 0 "
        "  AND COALESCE(last_count, 0) < par_level "   # par set but never counted -> still order
        "ORDER BY vendor COLLATE NOCASE, name COLLATE NOCASE",
        (active_location_id(),),
    ).fetchall()

    groups = {}
    ordered = []
    for r in rows:
        need = _r((r["par_level"] or 0) - (r["last_count"] or 0))
        if need <= 0:
            continue
        item = {
            "name": r["name"], "unit": r["unit"],
            "par": _r(r["par_level"]), "on_hand": _r(r["last_count"]),
            "order_qty": need, "unit_cost": _r(r["unit_cost"]),
            "line_cost": money.normalize(need * (r["unit_cost"] or 0)) or 0.0,
        }
        # Group case-insensitively (the rest of the app keys vendors on
        # lower(name)), keeping the first-seen casing for display, so one
        # distributor never splits into two order sheets.
        key = (r["vendor"] or "").casefold()
        g = groups.get(key)
        if g is None:
            groups[key] = g = {"vendor": r["vendor"], "items": [], "subtotal": 0.0}
            ordered.append(g)
        g["items"].append(item)

    for g in ordered:
        g["subtotal"] = money.sum_dollars(i["line_cost"] for i in g["items"])
    return {
        "vendors": ordered,
        "item_count": sum(len(g["items"]) for g in ordered),
        "grand_total": money.sum_dollars(
            i["line_cost"] for g in ordered for i in g["items"]),
    }
