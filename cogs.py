"""Cost calculations: COGS %, labor %, and prime cost for a date range.

Two ways to read COGS are offered:
  * purchases-based  — COGS = invoices logged in the period (simple, always on)
  * usage-based      — COGS = beginning inventory + purchases - ending inventory
                       (shown when two inventory counts bracket the period)
"""
import datetime as dt

from flask import abort

from db import get_db, get_setting, active_location_id
import square_client


def purchases(start, end):
    """Total invoice spend in [start, end], plus a breakdown by category type.

    The total comes from invoice grand totals; the breakdown comes from
    categorized line items (so it reflects the two-level taxonomy)."""
    db = get_db()
    loc = active_location_id()
    total_row = db.execute(
        "SELECT COALESCE(SUM(total),0) AS amt, COUNT(*) AS n FROM invoices "
        "WHERE location_id IS ? AND invoice_date >= ? AND invoice_date <= ?",
        (loc, start.isoformat(), end.isoformat()),
    ).fetchone()
    rows = db.execute(
        "SELECT COALESCE(c.category_type,'Uncategorized') AS ctype, "
        "       COALESCE(SUM(ii.total),0) AS amt "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN categories c ON c.id = ii.category_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ? "
        "GROUP BY c.category_type",
        (loc, start.isoformat(), end.isoformat()),
    ).fetchall()
    by_cat = {r["ctype"]: round(r["amt"], 2) for r in rows if r["amt"]}
    return {"by_category": by_cat, "total": round(total_row["amt"], 2), "count": total_row["n"]}


def _purchase_total(loc, after_date, through_date):
    """Sum invoice grand totals received after `after_date` through `through_date`
    (both ISO dates). Used for usage-COGS, where purchases must span the SAME
    interval as the bracketing counts (the opening count subsumes its own day's
    deliveries, so the lower bound is exclusive)."""
    row = get_db().execute(
        "SELECT COALESCE(SUM(total),0) AS amt FROM invoices "
        "WHERE location_id IS ? AND invoice_date > ? AND invoice_date <= ?",
        (loc, after_date, through_date),
    ).fetchone()
    return round(row["amt"], 2)


# How far a bracketing count may sit outside the requested period and still be
# trusted for usage-based COGS (else the inventory swing covers a different span).
USAGE_GRACE_DAYS = 14


def _inventory_value_near(target, prefer_before=True):
    """Find the inventory count nearest `target` date and return its $ value.

    prefer_before=True  -> the latest count on/before target (beginning value)
    prefer_before=False -> the earliest count on/after target (ending value)
    """
    db = get_db()
    loc = active_location_id()
    if prefer_before:
        row = db.execute(
            "SELECT id, value, taken_at FROM counts WHERE location_id IS ? AND date(taken_at) <= ? "
            "ORDER BY taken_at DESC LIMIT 1",
            (loc, target.isoformat()),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT id, value, taken_at FROM counts WHERE location_id IS ? AND date(taken_at) >= ? "
            "ORDER BY taken_at ASC LIMIT 1",
            (loc, target.isoformat()),
        ).fetchone()
    if not row:
        return None
    return {"value": round(row["value"], 2), "taken_at": row["taken_at"]}


def summary(start, end):
    sales_info = square_client.get_sales(start, end)
    labor_info = square_client.get_labor(start, end)
    purch = purchases(start, end)

    sales = sales_info["sales"]
    labor = labor_info["labor"]

    def pct_of(part, den):
        return round(part / den * 100, 1) if den else None

    # Usage-based COGS when two counts genuinely bracket the period. The swing is
    # measured between the counts, so purchases must be summed over the SAME
    # interval (not the requested range) or the figure is internally inconsistent.
    begin = _inventory_value_near(start, prefer_before=True)
    end_inv = _inventory_value_near(end, prefer_before=False)
    usage_cogs = None
    usage_period = None
    b_date = e_date = None
    if begin and end_inv:
        b_date = dt.date.fromisoformat(begin["taken_at"][:10])
        e_date = dt.date.fromisoformat(end_inv["taken_at"][:10])
        # The usage cost spans [b_date, e_date] but cogs_pct divides by sales over
        # the requested range, so keep the count interval close to that range
        # (each end within grace AND total overshoot <= the range length). This
        # also rejects usage for very short ranges, where it's meaningless.
        slop = (start - b_date).days + (e_date - end).days
        brackets = (b_date < e_date
                    and (start - b_date).days <= USAGE_GRACE_DAYS
                    and (e_date - end).days <= USAGE_GRACE_DAYS
                    and slop <= (end - start).days)
        if brackets:
            interval_purch = _purchase_total(
                active_location_id(), b_date.isoformat(), e_date.isoformat())
            usage_cogs = round(begin["value"] + interval_purch - end_inv["value"], 2)
            usage_period = {"start": b_date.isoformat(), "end": e_date.isoformat(),
                            "purchases": interval_purch}

    cogs_amount = usage_cogs if usage_cogs is not None else purch["total"]
    cogs_method = "usage" if usage_cogs is not None else "purchases"

    # COGS% must divide by sales over the SAME span as the cost. Purchases-based
    # COGS spans the requested range (use `sales`). Usage-based COGS spans the
    # count interval [b_date, e_date], which can sit well outside the range, so
    # divide it by THAT interval's sales (cheap via the daily cache) rather than
    # range sales — otherwise COGS%/prime% are inflated by up to ~2x. Labor%
    # always stays on the requested-range sales.
    cogs_sales = sales
    if (usage_cogs is not None and square_client.is_configured()
            and (b_date != start or e_date != end)):
        daily = square_client.daily_sales_cached(b_date, e_date)
        interval_sales = round(sum(daily.values()), 2) if daily else 0.0
        if interval_sales:
            cogs_sales = interval_sales

    prime = round(cogs_amount + labor, 2)

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "sales": sales,
        "orders": sales_info.get("orders", 0),
        "sales_error": sales_info.get("error"),
        "labor": labor,
        "labor_hours": labor_info.get("hours", 0),
        "labor_pct": pct_of(labor, sales),
        "labor_error": labor_info.get("error"),
        "labor_warning": labor_info.get("warning"),
        "unwaged_hours": labor_info.get("unwaged_hours", 0),
        "unwaged_shifts": labor_info.get("unwaged_shifts", 0),
        "purchases": purch["total"],
        "purchases_by_category": purch["by_category"],
        "invoice_count": purch["count"],
        "cogs": cogs_amount,
        "cogs_pct": pct_of(cogs_amount, cogs_sales),
        "cogs_method": cogs_method,
        "cogs_sales": cogs_sales,
        "usage_period": usage_period,
        "prime": prime,
        "prime_pct": pct_of(prime, cogs_sales),
        "begin_inventory": begin,
        "end_inventory": end_inv,
        "targets": {
            "cogs": _num(get_setting("target_cogs_pct"), 30.0),
            "labor": _num(get_setting("target_labor_pct"), 25.0),
        },
        "square_configured": square_client.is_configured(),
    }


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _iso_or_400(s):
    """Parse an ISO date or abort 400 — query-string dates are user input and a
    bad value must be a clean 400, not an unhandled 500."""
    try:
        return dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        abort(400, description=f"Invalid date: {s!r} (expected YYYY-MM-DD).")


def parse_range(start_s, end_s):
    """Parse ISO date strings; default to the current week (Mon-today)."""
    today = square_client.business_today()
    start = _iso_or_400(start_s) if start_s else today - dt.timedelta(days=today.weekday())
    end = _iso_or_400(end_s) if end_s else today
    if end < start:
        start, end = end, start
    return start, end
