"""Cost calculations: COGS %, labor %, and prime cost for a date range.

Two ways to read COGS are offered:
  * purchases-based  — COGS = invoices logged in the period (simple, always on)
  * usage-based      — COGS = beginning inventory + purchases - ending inventory
                       (shown when two inventory counts bracket the period)
"""
import datetime as dt

from flask import abort

from db import get_db, get_setting, active_location_id
import money
import square_client


def pretax_factor(subtotal, total, line_sum, tax=None):
    """Deflation factor to put an invoice's line totals on the PRE-TAX basis.

    Vendors print line totals either tax-exclusive (sum to the subtotal) or
    tax-inclusive (sum to the grand total) — `_reconcile` accepts both. COGS must
    be pre-tax to match the tax-stripped net-sales denominator, so when an
    invoice's lines reconcile to the tax-INCLUSIVE total, deflate them by
    base/total. Returns 1.0 when already pre-tax or undeterminable.

    `base` is the recorded subtotal; when that's missing (a tax+total-only
    invoice, common from AI/manual entry) it's derived as total - tax, so such an
    invoice still deflates instead of silently counting the tax in COGS — matching
    _reconcile's tot - tax target.

    Works by MAGNITUDE, not sign, so a tax-inclusive CREDIT/return (stored
    negative: subtotal=-100, tax=-6, total=-106) deflates the same way as the
    +106 invoice — otherwise the tax credit would over-reduce COGS."""
    base = subtotal
    # Derive base = total - tax when subtotal is missing OR its sign disagrees with
    # total (a credit's base must be negative too).
    if (not base or (total and base * total < 0)) and total and tax:
        base = total - tax
    if not base or not total or base * total <= 0 or abs(base) >= abs(total):
        return 1.0
    return base / total if abs(line_sum - total) < abs(line_sum - base) else 1.0


def _pretax_line_rows(rows):
    """Given rows carrying (iid, sub, tot, tax, amt[, ctype]), yield (row,
    deflated_amt) with each line put on the pre-tax basis per its invoice's
    convention."""
    line_sum = {}
    for r in rows:
        line_sum[r["iid"]] = line_sum.get(r["iid"], 0.0) + (r["amt"] or 0)
    for r in rows:
        f = pretax_factor(r["sub"], r["tot"], line_sum[r["iid"]], r["tax"])
        yield r, (r["amt"] or 0) * f


def purchases(start, end):
    """COGS-basis invoice spend in [start, end], plus a breakdown by category type.

    Both the total AND the breakdown come from LINE ITEMS, deflated to the
    PRE-TAX base (so a vendor whose lines print tax-inclusive doesn't inflate
    COGS vs. the tax-stripped sales denominator). Matches the Controllable P&L
    and Category Report exactly. Summed in integer cents."""
    db = get_db()
    loc = active_location_id()
    rows = db.execute(
        "SELECT inv.id AS iid, inv.subtotal AS sub, inv.total AS tot, inv.tax AS tax, "
        "       COALESCE(c.category_type,'Uncategorized') AS ctype, ii.total AS amt "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "LEFT JOIN categories c ON c.id = ii.category_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ?",
        (loc, start.isoformat(), end.isoformat()),
    ).fetchall()
    by_cat_c = {}
    for r, amt in _pretax_line_rows(rows):
        by_cat_c[r["ctype"]] = by_cat_c.get(r["ctype"], 0) + money.to_cents(amt)
    # Count only invoices that actually contribute a costed line in range, so the
    # surfaced invoice_count can't imply more spend than `total` reflects (a header
    # logged with no line items adds $0 to total and shouldn't inflate the count).
    n = db.execute(
        "SELECT COUNT(DISTINCT inv.id) AS n "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date >= ? AND inv.invoice_date <= ?",
        (loc, start.isoformat(), end.isoformat()),
    ).fetchone()["n"]
    by_cat = {k: round(v / 100.0, 2) for k, v in by_cat_c.items() if v}
    return {"by_category": by_cat,
            "total": round(sum(by_cat_c.values()) / 100.0, 2), "count": n}


def _purchase_total(loc, after_date, through_date):
    """Sum invoice LINE-ITEM totals (pre-tax basis) received after `after_date`
    through `through_date` (both ISO dates) — same basis as purchases(). Used for
    usage-COGS, where purchases span the bracketing-count interval (the opening
    count subsumes its own day's deliveries, so the lower bound is exclusive)."""
    rows = get_db().execute(
        "SELECT inv.id AS iid, inv.subtotal AS sub, inv.total AS tot, inv.tax AS tax, ii.total AS amt "
        "FROM invoice_items ii JOIN invoices inv ON inv.id = ii.invoice_id "
        "WHERE inv.location_id IS ? AND inv.invoice_date > ? AND inv.invoice_date <= ?",
        (loc, after_date, through_date),
    ).fetchall()
    return money.sum_dollars(amt for _, amt in _pretax_line_rows(rows))


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
    # Compare taken_at (text 'YYYY-MM-DD HH:MM:SS') as a plain string rather than
    # wrapping it in date(): a bare column predicate is sargable, so the
    # (location_id, taken_at) index serves the range scan instead of recomputing
    # date() over every one of the store's counts. '<' next-day catches all of
    # the target day's timestamps; '>=' target sorts at/before any same-day time.
    if prefer_before:
        row = db.execute(
            "SELECT id, value, taken_at FROM counts WHERE location_id IS ? AND taken_at < ? "
            "ORDER BY taken_at DESC LIMIT 1",
            (loc, (target + dt.timedelta(days=1)).isoformat()),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT id, value, taken_at FROM counts WHERE location_id IS ? AND taken_at >= ? "
            "ORDER BY taken_at ASC LIMIT 1",
            (loc, target.isoformat()),
        ).fetchone()
    if not row:
        return None
    # Defensive `or 0`: a historical count row with a NULL value (e.g. from a pre-fix
    # overflow) must not crash round(None) and take out the dashboard. A 0 value then
    # fails the positive-bracket guard in summary(), falling back to purchases COGS.
    return {"value": round(row["value"] or 0, 2), "taken_at": row["taken_at"]}


def summary(start, end):
    sales_info = square_client.get_sales(start, end)
    labor_info = square_client.get_labor(start, end)
    purch = purchases(start, end)

    sales = sales_info["sales"]
    labor = labor_info["labor"]
    # A labor-only Square outage returns labor=0 with an error set. Treating that 0
    # as a genuine $0 would drop labor from prime/prime% and inflate the headline,
    # so null the labor-derived figures when labor errored (mirrors the sales guard).
    labor_failed = bool(labor_info.get("error"))

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
    # Require POSITIVE bracket values: a forgotten/empty count is stored as $0, and
    # a $0 closing bracket would make usage COGS = begin + purchases (overstated by
    # the whole opening inventory), a $0 opening would understate or go negative.
    # Fall back to purchases-based COGS rather than trust a degenerate count.
    if begin and end_inv and begin["value"] > 0 and end_inv["value"] > 0:
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
    cogs_sales_basis = "range"
    # Run the interval alignment whenever usage COGS is in play — even when the
    # counts land exactly on the range endpoints. Purchases are summed over the
    # HALF-OPEN (b_date, e_date] (the opening-count day is excluded), so the sales
    # denominator must drop that day's sales too; skipping this in the coincident
    # b_date==start / e_date==end case would leave COGS% understated by one day.
    if usage_cogs is not None and square_client.is_configured():
        daily = square_client.daily_sales_cached(b_date, e_date)
        # Only trust the interval basis when the cache covers EVERY day of the
        # interval — a cold/partial cache would understate the denominator and
        # inflate COGS%. Otherwise fall back to range sales.
        interval_days = (e_date - b_date).days + 1
        if daily and len(daily) == interval_days:
            # Purchases are summed over (b_date, e_date] (exclusive of the opening
            # count day), so match the sales denominator: drop b_date's sales.
            b_iso = b_date.isoformat()
            interval_sales = money.sum_dollars(v for day, v in daily.items() if day > b_iso)
            if interval_sales:
                cogs_sales, cogs_sales_basis = interval_sales, "interval"

    # If usage COGS is active but we couldn't establish the matching interval-sales
    # denominator (cold/partial cache), cogs_amount/range-sales is misaligned: even
    # in the COINCIDENT case (counts on the range endpoints) purchases drop b_date's
    # deliveries while range sales still include b_date's sales, so COGS% drifts by a
    # day. Fall back to the range-consistent purchases basis whenever the interval
    # basis didn't take (a warm cache already set basis 'interval', so this is safe).
    # Only when Square is configured: with no sales, cogs_pct is None anyway and the
    # usage COGS dollar figure is still the useful one.
    if (usage_cogs is not None and square_client.is_configured()
            and cogs_sales_basis == "range"):
        cogs_amount = purch["total"]
        cogs_method = "purchases"
        usage_period = None

    # COGS% and Labor% use different sales bases in usage mode (interval vs
    # range), so prime% is the SUM of the two correctly-based percentages — never
    # prime/cogs_sales, which would divide range labor by interval sales.
    cogs_pct = pct_of(cogs_amount, cogs_sales)
    labor_pct = None if labor_failed else pct_of(labor, sales)
    prime_pct = (round(cogs_pct + labor_pct, 1)
                 if cogs_pct is not None and labor_pct is not None else None)
    # Prime DOLLARS: in interval mode scale COGS to the requested range before
    # adding (range-spanning) labor, so the figure isn't an interval+range mash.
    # Scale by the SALES ratio (range/interval), not a calendar-day ratio, so
    # prime$ / range_sales reconciles with prime_pct (= cogs_pct + labor_pct).
    # cogs_pct divides by interval sales, so prime_cogs = cogs_amount * range
    # sales / interval sales == cogs_pct% * range sales — the same basis.
    prime_cogs = cogs_amount
    if cogs_sales_basis == "interval" and cogs_sales:
        prime_cogs = money.normalize(cogs_amount * sales / cogs_sales) or 0.0
    # Prime needs labor; null it on a labor outage rather than report COGS-only as
    # prime. Also pair it with prime_pct: if prime_pct is None (e.g. range sales is 0
    # so labor_pct is None) don't show a lone Prime$ next to a blank Prime%.
    prime = None if (labor_failed or prime_pct is None) else round((prime_cogs or 0.0) + labor, 2)

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "sales": sales,
        "orders": sales_info.get("orders", 0),
        "sales_error": sales_info.get("error"),
        "labor": labor,
        "labor_hours": labor_info.get("hours", 0),
        "labor_pct": labor_pct,
        "labor_error": labor_info.get("error"),
        "labor_warning": labor_info.get("warning"),
        "unwaged_hours": labor_info.get("unwaged_hours", 0),
        "unwaged_shifts": labor_info.get("unwaged_shifts", 0),
        "purchases": purch["total"],
        "purchases_by_category": purch["by_category"],
        "invoice_count": purch["count"],
        "cogs": cogs_amount,
        "cogs_pct": cogs_pct,
        "cogs_method": cogs_method,
        "cogs_sales": cogs_sales,
        "cogs_sales_basis": cogs_sales_basis,
        "usage_period": usage_period,
        "prime": prime,
        "prime_pct": prime_pct,
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
