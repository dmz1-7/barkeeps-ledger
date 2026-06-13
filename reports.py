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
from cogs import pretax_factor, _pretax_line_rows
import money
import square_client

CATEGORY_TYPES = list(TAXONOMY.keys())  # Food, Beer, Wine, Liquor, N/A Bev, Other


def _r(n):
    return round(n or 0, 2)


def _same_rate(a, b):
    """Compare two unit_cost rates by rounded value. unit_cost is a plain REAL
    (deliberately NOT penny-normalized for sub-cent rates), so two economically
    identical prices entered via different derivations (8.00/24 vs a stored
    0.333333) carry different float tails and `==` would call them distinct. Round
    to 4dp so a price 'epoch' boundary is robust to that float noise."""
    return round(a or 0, 4) == round(b or 0, 4)


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
        "SELECT id, invoice_date, invoice_number, vendor, status, subtotal, total, tax "
        f"FROM invoices WHERE {' AND '.join(where)} "
        "ORDER BY invoice_date DESC, id DESC",
        params,
    ).fetchall()

    # Deflate and round each LINE to cents (via _pretax_line_rows), exactly as
    # cogs.purchases / controllable_pl do — NOT a per-(invoice,category) SUM rounded
    # once. Rounding at the same per-line granularity keeps the Category Report's
    # column_totals/grand_total reconciled to the P&L to the penny even for
    # tax-inclusive multi-line invoices (where the two granularities diverge ~1c/group).
    cell_cents = {}   # (invoice_id, category_id) -> integer cents
    inv_ids = [r["id"] for r in invs]
    if inv_ids:
        marks = ",".join("?" * len(inv_ids))
        rows_q = db.execute(
            f"SELECT inv.id AS iid, inv.subtotal AS sub, inv.total AS tot, inv.tax AS tax, "
            f"ii.category_id AS cat, ii.total AS amt "
            f"FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
            f"WHERE ii.invoice_id IN ({marks})",
            inv_ids,
        ).fetchall()
        for r, amt in _pretax_line_rows(rows_q):
            key = (r["iid"], r["cat"])
            cell_cents[key] = cell_cents.get(key, 0) + money.to_cents(amt)

    # id 0 = an "Uncategorized" column. It catches line items with category_id
    # NULL **and** any line booked to a category that is no longer active (archived
    # or otherwise not a displayed column). Without that, spend on an archived
    # category would be silently dropped from column_totals AND grand_total, making
    # the Category Report under-report purchases and disagree with the Controllable
    # P&L (which counts that spend under its type regardless of archive state).
    active_ids = {c["id"] for c in cats}
    col_ids = [c["id"] for c in cats] + [0]
    col_cents = {cid: 0 for cid in col_ids}   # accumulate in integer cents (no float drift)
    row_cents = {}                            # iid -> {col_id: cents}
    for (iid, cid), c in cell_cents.items():
        col = cid if cid in active_ids else 0   # NULL / archived / unknown -> Uncategorized
        col_cents[col] += c
        rc = row_cents.setdefault(iid, {})
        rc[col] = rc.get(col, 0) + c
    rows = []
    for inv in invs:
        rc = row_cents.get(inv["id"], {})
        cells = {cid: round(rc.get(cid, 0) / 100.0, 2) for cid in col_ids}
        rows.append({**dict(inv), "cells": cells})

    cats_out = [dict(c) for c in cats] + [
        {"id": 0, "name": "Uncategorized", "category_type": "Uncategorized", "sort_order": 9999}]
    return {
        "categories": cats_out,
        "rows": rows,
        "column_totals": {k: round(v / 100.0, 2) for k, v in col_cents.items()},
        "grand_total": round(sum(col_cents.values()) / 100.0, 2),
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
    last_t = None
    for t in CATEGORY_TYPES:
        amt = _r(sales * mix.get(t, 0) / 100.0)
        income_by_type[t] = amt
        income.append({
            "category_type": t,
            "amt": amt,
            "pct_of_sales": _r(amt / sales * 100) if sales else None,
        })
        if mix.get(t, 0):
            last_t = t
    total_income = _r(sales)
    # Per-type incomes are rounded independently, so on a full 100% split they can
    # sum a penny off total_income. Allocate that residual to the last non-zero type
    # so the parts reconcile to total_income exactly (the "income sums to sales"
    # invariant) — only when the mix is actually a ~100% split.
    if last_t is not None and abs(sum(mix.get(t, 0) for t in CATEGORY_TYPES) - 100) < 0.5:
        residual = _r(total_income - sum(income_by_type.values()))
        if residual:
            income_by_type[last_t] = _r(income_by_type[last_t] + residual)
            for row in income:
                if row["category_type"] == last_t:
                    row["amt"] = income_by_type[last_t]
                    row["pct_of_sales"] = _r(row["amt"] / sales * 100) if sales else None

    # COGS by category type -> category, from invoice lines in the period, on the
    # PRE-TAX basis (deflate tax-inclusive invoices) and summed in integer cents.
    cogs_rows = db.execute(
        "SELECT inv.id AS iid, inv.subtotal AS sub, inv.total AS tot, inv.tax AS tax, "
        "       c.category_type AS ctype, c.name AS category, ii.total AS amt "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN categories c ON c.id = ii.category_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ?",
        (loc, start.isoformat(), end.isoformat()),
    ).fetchall()

    by_type = {}
    for r, amt in _pretax_line_rows(cogs_rows):
        ctype = r["ctype"] or "Uncategorized"
        cat = r["category"] or "Uncategorized"
        bt = by_type.setdefault(ctype, {"total_c": 0, "cats_c": {}})
        cents = money.to_cents(amt)
        bt["total_c"] += cents
        bt["cats_c"][cat] = bt["cats_c"].get(cat, 0) + cents

    ordered_types = CATEGORY_TYPES + [t for t in by_type if t not in CATEGORY_TYPES]
    cogs = []
    total_cogs_c = 0
    for t in ordered_types:
        if t not in by_type:
            continue
        bt = by_type[t]
        type_income = income_by_type.get(t, 0)
        type_total = round(bt["total_c"] / 100.0, 2)
        total_cogs_c += bt["total_c"]
        cats = [{"category": cat, "amt": round(c / 100.0, 2),
                 "pct": _r(round(c / 100.0, 2) / type_income * 100) if type_income else None}
                for cat, c in bt["cats_c"].items()]
        cogs.append({
            "category_type": t,
            "type_total": type_total,
            "type_pct": _r(type_total / type_income * 100) if type_income else None,
            "categories": sorted(cats, key=lambda x: -x["amt"]),
        })

    total_cogs = round(total_cogs_c / 100.0, 2)
    # When Square sales failed soft (sales=0, error set) total_income is a phantom
    # 0, so gross/controllable would read as a real multi-hundred-dollar LOSS.
    # Null the profit headlines instead (mirrors cogs.summary nulling cogs_pct on
    # a zero denominator) so a degraded P&L can't masquerade as a real loss.
    sales_failed = bool(sales_info.get("error"))
    # A labor-only outage returns labor=0 with an error: controllable_profit =
    # gross - labor would then read as an inflated real profit. Null it (and the
    # Labor expense amount) on a labor error too, not just a sales error.
    labor_failed = bool(labor_info.get("error"))
    gross = None if sales_failed else _r(total_income - total_cogs)
    controllable = None if (sales_failed or labor_failed) else _r((gross or 0) - labor)
    labor_amt = None if labor_failed else labor

    def pct(part):
        return _r(part / total_income * 100) if total_income and part is not None else None

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
        "expenses": [{"name": "Labor", "amt": labor_amt, "pct": pct(labor_amt)}],
        "total_expenses": labor_amt,
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
    errors = []
    sales = square_client.daily_sales_cached(full_start, today, errors)

    def g(d):
        return sales.get(d.isoformat(), 0.0)

    def rng(a, b):
        # Sum the penny-exact daily floats via money.sum_dollars (integer cents),
        # matching the codebase's money discipline instead of float += drift.
        vals, d = [], a
        while d <= b:
            vals.append(sales.get(d.isoformat(), 0.0))
            d += dt.timedelta(days=1)
        return money.sum_dollars(vals)

    days = []
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    tw_vals, lw_vals, ly_vals = [], [], []
    for i, label in enumerate(labels):
        d_tw = this_mon + dt.timedelta(days=i)
        tw = g(d_tw) if d_tw <= today else None
        lw = g(last_mon + dt.timedelta(days=i))
        ly = g(ly_mon + dt.timedelta(days=i))
        if tw:
            tw_vals.append(tw)
        lw_vals.append(lw)
        ly_vals.append(ly)
        days.append({"day": label, "this_week": tw, "last_week": _r(lw), "last_year": _r(ly)})
    tw_tot, lw_tot, ly_tot = (money.sum_dollars(tw_vals), money.sum_dollars(lw_vals),
                              money.sum_dollars(ly_vals))

    ptd = rng(month_start, today)
    ytd = rng(year_start, today)

    return {
        "week_of": this_mon.isoformat(),
        "days": days,
        "totals": {"this_week": _r(tw_tot), "last_week": _r(lw_tot), "last_year": _r(ly_tot)},
        "period_to_date": _r(ptd),
        "year_to_date": _r(ytd),
        "square_configured": configured,
        # Surface a Square outage so an all-$0 degraded report is distinguishable
        # from a genuine slow sales week (mirrors summary/controllable_pl).
        "sales_error": errors[0] if errors else None,
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
                "new_price": None, "earliest_price": None, "qty": 0.0,
                "epoch_ended": False,
            }
        if g["new_price"] is None and r["price"] is not None:
            g["new_price"] = r["price"]      # newest-first, so this is the latest price
        if r["price"] is not None:
            g["earliest_price"] = r["price"]  # newest-first: last write = oldest in-window price
        # Impact = price delta x qty bought AT the new price. Count only the
        # CONTIGUOUS newest run at new_price (the latest price epoch). Match on the
        # epoch, not value equality: once the price changes, the epoch is over, so a
        # later dip-and-return to the SAME value can't fold those earlier units in
        # (which charged the full delta on pre-dip units and overstated impact).
        if not g["epoch_ended"]:
            if _same_rate(r["price"], g["new_price"]):
                g["qty"] += (r["qty"] or 0)
            elif r["price"] is not None:
                g["epoch_ended"] = True

    pvkey = _PM_VENDOR.format(vi="v", inv="xi")
    pnkey = _PM_NAME.format(vi="v", ii="x")
    cat_names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM categories")}
    # Prior price (latest BEFORE the window) for every SKU in ONE pass, keyed on the
    # same (vendor, item) identity — instead of a correlated subquery per moving SKU
    # over a non-sargable lower(TRIM(...)) key (an N+1 that grows with history).
    prior_price = {}
    for r in db.execute(
            f"SELECT {pvkey} AS vk, {pnkey} AS nk, x.unit_cost AS p FROM invoice_items x "
            "JOIN invoices xi ON xi.id=x.invoice_id "
            "LEFT JOIN vendor_items v ON v.id = x.vendor_item_id "
            "WHERE xi.location_id IS ? AND xi.invoice_date < ? AND x.unit_cost > 0 "
            "ORDER BY xi.invoice_date ASC, x.id ASC",   # ascending so the LAST seen per key wins (=latest)
            (loc, start.isoformat())):
        prior_price[(r["vk"], r["nk"])] = r["p"]
    movers = []
    for (gv, gn), g in groups.items():
        new_price, qty = g["new_price"], (g["qty"] or 0)
        if new_price is None or not qty:     # no price or nothing bought -> no signal
            continue
        old_price = prior_price.get((gv, gn))
        if old_price is None:
            # No price before the window — fall back to the earliest price seen
            # WITHIN it, so a change that happens entirely in-window still shows.
            ep = g.get("earliest_price")
            if ep is not None and not _same_rate(ep, new_price):
                old_price = ep
        if old_price is None or _same_rate(old_price, new_price):
            continue
        q = _r(qty)   # use the DISPLAYED qty for impact so Δprice x shown-qty reconciles
        movers.append({
            "name": g["name"],
            "category": cat_names.get(g["cat"], "Uncategorized"),
            "old_price": _r(old_price),
            "new_price": _r(new_price),
            "change_pct": _r((new_price - old_price) / old_price * 100) if old_price else None,
            "qty": q,
            "impact": _r((new_price - old_price) * q),
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
            if _same_rate(r["price"], g["new_price"]):
                g["new_qty"] += (r["qty"] or 0)
            else:
                g["ambiguous"] = True
        elif not _same_rate(r["price"], g["new_price"]):
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
            "qty": (q := _r(g["new_qty"] or 0)),
            "impact": _r((new - old) * q),   # impact from the displayed qty, so they reconcile
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
