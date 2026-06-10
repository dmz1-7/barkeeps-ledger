"""Cost calculations: COGS %, labor %, and prime cost for a date range.

Two ways to read COGS are offered:
  * purchases-based  — COGS = invoices logged in the period (simple, always on)
  * usage-based      — COGS = beginning inventory + purchases - ending inventory
                       (shown when two inventory counts bracket the period)
"""
import datetime as dt

from db import get_db, get_setting
import square_client


def purchases(start, end):
    """Sum invoice totals in [start, end], grouped by category."""
    rows = get_db().execute(
        "SELECT category, COALESCE(SUM(total),0) AS amt, COUNT(*) AS n "
        "FROM invoices WHERE invoice_date >= ? AND invoice_date <= ? "
        "GROUP BY category",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    by_cat = {r["category"] or "other": round(r["amt"], 2) for r in rows}
    total = round(sum(by_cat.values()), 2)
    count = sum(r["n"] for r in rows)
    return {"by_category": by_cat, "total": total, "count": count}


def _inventory_value_near(target, prefer_before=True):
    """Find the inventory count nearest `target` date and return its $ value.

    prefer_before=True  -> the latest count on/before target (beginning value)
    prefer_before=False -> the earliest count on/after target (ending value)
    """
    db = get_db()
    if prefer_before:
        row = db.execute(
            "SELECT id, value, taken_at FROM counts WHERE date(taken_at) <= ? "
            "ORDER BY taken_at DESC LIMIT 1",
            (target.isoformat(),),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT id, value, taken_at FROM counts WHERE date(taken_at) >= ? "
            "ORDER BY taken_at ASC LIMIT 1",
            (target.isoformat(),),
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

    def pct(part):
        return round(part / sales * 100, 1) if sales else None

    # Usage-based COGS when counts bracket the period.
    begin = _inventory_value_near(start, prefer_before=True)
    end_inv = _inventory_value_near(end, prefer_before=False)
    usage_cogs = None
    if begin and end_inv and begin["taken_at"] != end_inv["taken_at"]:
        usage_cogs = round(begin["value"] + purch["total"] - end_inv["value"], 2)

    cogs_amount = usage_cogs if usage_cogs is not None else purch["total"]
    cogs_method = "usage" if usage_cogs is not None else "purchases"

    prime = round(cogs_amount + labor, 2)

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "sales": sales,
        "orders": sales_info.get("orders", 0),
        "sales_error": sales_info.get("error"),
        "labor": labor,
        "labor_hours": labor_info.get("hours", 0),
        "labor_pct": pct(labor),
        "labor_error": labor_info.get("error"),
        "purchases": purch["total"],
        "purchases_by_category": purch["by_category"],
        "invoice_count": purch["count"],
        "cogs": cogs_amount,
        "cogs_pct": pct(cogs_amount),
        "cogs_method": cogs_method,
        "prime": prime,
        "prime_pct": pct(prime),
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


def parse_range(start_s, end_s):
    """Parse ISO date strings; default to the current week (Mon-today)."""
    today = dt.date.today()
    if start_s:
        start = dt.date.fromisoformat(start_s)
    else:
        start = today - dt.timedelta(days=today.weekday())
    end = dt.date.fromisoformat(end_s) if end_s else today
    if end < start:
        start, end = end, start
    return start, end
