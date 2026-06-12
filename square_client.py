"""Square API client — pulls sales (Orders) and labor (Shifts).

Kept dependency-light: just `requests`. All calls fail soft — if Square isn't
configured or a call errors, callers get zeros plus an `error` string so the
dashboard can show "Connect Square" instead of crashing.
"""
import datetime as dt
import requests

from db import get_setting, get_db, active_location_id

PROD_BASE = "https://connect.squareup.com"
SANDBOX_BASE = "https://connect.squareupsandbox.com"


def _active_square_location_id():
    """The Square location id for the store this request is acting on, read from
    the locations table (not a mutable global setting) so sales/labor always follow
    the active store, even with concurrent devices."""
    row = get_db().execute(
        "SELECT square_location_id FROM locations WHERE id=?", (active_location_id(),)
    ).fetchone()
    return ((row["square_location_id"] if row else "") or "").strip()


def _cfg():
    return {
        "token": (get_setting("square_token") or "").strip(),
        "location_id": _active_square_location_id(),
        "env": (get_setting("square_env") or "production").strip(),
        "version": (get_setting("square_version") or "2025-01-23").strip(),
    }


def is_configured():
    c = _cfg()
    return bool(c["token"] and c["location_id"])


def _base(env):
    return SANDBOX_BASE if env == "sandbox" else PROD_BASE


def _headers(c):
    return {
        "Square-Version": c["version"],
        "Authorization": f"Bearer {c['token']}",
        "Content-Type": "application/json",
    }


def _iso(d):
    """Accept a date or datetime; return RFC3339 UTC string Square expects."""
    if isinstance(d, dt.date) and not isinstance(d, dt.datetime):
        d = dt.datetime.combine(d, dt.time.min)
    return d.replace(microsecond=0).isoformat() + "Z"


# --- business day boundaries ------------------------------------------------
# A "business day" runs from `day_start_hour` (local) to the same hour next day,
# so late-night/after-midnight sales count toward the night they happened. Both
# the Square query window and the per-order day bucketing use this.

def _tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo((get_setting("tz") or "America/New_York").strip())


def _day_start_hour():
    try:
        return int(get_setting("day_start_hour") or 5)
    except (TypeError, ValueError):
        return 5


def _window(start, end):
    """UTC RFC3339 (start_at, end_at) for business days [start .. end] inclusive:
    from `start` at day_start (local) to `end`+1 at day_start (local)."""
    tz, h = _tz(), _day_start_hour()
    s = dt.datetime.combine(start, dt.time(h), tzinfo=tz)
    e = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(h), tzinfo=tz)
    fmt = lambda x: x.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fmt(s), fmt(e)


def _business_date(ts):
    """The business-day date for a tz-aware datetime, honoring day_start_hour.
    Compares the local wall-clock hour rather than doing timedelta arithmetic so
    the boundary can't slip across a DST gap (e.g. a pre-dawn day_start_hour)."""
    local = ts.astimezone(_tz())
    d = local.date()
    if local.hour < _day_start_hour():
        d -= dt.timedelta(days=1)
    return d


def _business_day(closed_at):
    """The business-day date (ISO) an order's closed_at falls into."""
    ts = _parse_ts(closed_at)
    if not ts:
        return None
    return _business_date(ts).isoformat()


def business_today(now=None):
    """Today's business day as a date, in the configured tz and honoring the
    day_start_hour boundary: between midnight and day_start it is still the
    PRIOR calendar day. Default date ranges use this so "today"/"this week"
    line up with how sales and labor are bucketed, regardless of server tz."""
    return _business_date(now or dt.datetime.now(dt.timezone.utc))


def list_locations():
    c = _cfg()
    if not c["token"]:
        return {"error": "No Square access token set.", "locations": []}
    try:
        r = requests.get(
            f"{_base(c['env'])}/v2/locations",
            headers=_headers(c),
            timeout=20,
        )
        r.raise_for_status()
        locs = r.json().get("locations", [])
        return {
            "locations": [
                {"id": l.get("id"), "name": l.get("name"), "currency": l.get("currency")}
                for l in locs
            ]
        }
    except requests.RequestException as e:
        return {"error": _err(e), "locations": []}


def _amt(m):
    return (m or {}).get("amount", 0)


def _net_sales_cents(o):
    """Net sales for one order, in integer cents.

    'Net sales' here is the restaurant cost-ratio denominator: item revenue after
    discounts and refunds, but EXCLUDING tax, tips and service charges. Square's
    `net_amounts.total_money` is net-of-refunds but still INCLUDES tax + tip +
    service charge, so dividing cost by it inflates the denominator and deflates
    every COGS%/Labor%/prime%/P&L figure. We strip those three back out.

    Stay within ONE frame so refunded orders stay consistent: prefer the
    net_amounts.* sub-fields (net of returns); only when an order has no
    net_amounts at all (e.g. Square Invoice-sourced orders) fall back to the
    order-level total_* fields — never mix the two.
    """
    na = o.get("net_amounts")
    if na:
        return (_amt(na.get("total_money")) - _amt(na.get("tax_money"))
                - _amt(na.get("tip_money")) - _amt(na.get("service_charge_money")))
    return (_amt(o.get("total_money")) - _amt(o.get("total_tax_money"))
            - _amt(o.get("total_tip_money")) - _amt(o.get("total_service_charge_money")))


def get_sales(start, end):
    """Sum completed-order net sales between start and end (inclusive dates).

    Returns {sales: float, orders: int, error: str|None}. `sales` is in dollars.
    """
    c = _cfg()
    if not is_configured():
        return {"sales": 0.0, "orders": 0, "error": "Square not configured."}
    body = {
        "location_ids": [c["location_id"]],
        "query": {
            "filter": {
                "date_time_filter": {
                    "closed_at": dict(zip(("start_at", "end_at"), _window(start, end)))
                },
                "state_filter": {"states": ["COMPLETED"]},
            },
            "sort": {"sort_field": "CLOSED_AT", "sort_order": "DESC"},
        },
        "limit": 500,
    }
    total_cents = 0
    order_count = 0
    cursor = None
    try:
        while True:
            if cursor:
                body["cursor"] = cursor
            r = requests.post(
                f"{_base(c['env'])}/v2/orders/search",
                headers=_headers(c),
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            for o in data.get("orders", []):
                order_count += 1
                total_cents += _net_sales_cents(o)
            cursor = data.get("cursor")
            if not cursor:
                break
        return {"sales": round(total_cents / 100.0, 2), "orders": order_count, "error": None}
    except requests.RequestException as e:
        return {"sales": 0.0, "orders": 0, "error": _err(e)}


def get_daily_sales(start, end):
    """Net sales bucketed by business day (see _business_day / day_start_hour).

    Returns {iso_date: dollars}, or None if the Square call ERRORED (distinct
    from a successful fetch that found no sales, which is {}). Callers must not
    treat an errored fetch as "zero sales" — that would corrupt the cache.
    """
    c = _cfg()
    if not is_configured():
        return {}
    body = {
        "location_ids": [c["location_id"]],
        "query": {
            "filter": {
                "date_time_filter": {
                    "closed_at": dict(zip(("start_at", "end_at"), _window(start, end)))
                },
                "state_filter": {"states": ["COMPLETED"]},
            },
            "sort": {"sort_field": "CLOSED_AT", "sort_order": "DESC"},
        },
        "limit": 500,
    }
    by_day = {}
    cursor = None
    try:
        while True:
            if cursor:
                body["cursor"] = cursor
            r = requests.post(
                f"{_base(c['env'])}/v2/orders/search",
                headers=_headers(c),
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            for o in data.get("orders", []):
                day = _business_day(o.get("closed_at"))
                if not day:
                    continue
                # accumulate integer cents (matches get_sales), convert once below
                by_day[day] = by_day.get(day, 0) + _net_sales_cents(o)
            cursor = data.get("cursor")
            if not cursor:
                break
        return {d: round(c / 100.0, 2) for d, c in by_day.items()}
    except requests.RequestException:
        return None


def daily_sales_cached(start, end):
    """Per-day net sales for [start, end] for the active Square location, backed
    by the daily_sales table. Only days that are missing from the cache or still
    changing (today and yesterday) are fetched from Square; everything else is
    served from the DB. Returns {iso_date: dollars} (0 for days with no sales)."""
    c = _cfg()
    if not is_configured():
        return {}
    sqid = c["location_id"]
    dbc = get_db()
    cached = {
        r["date"]: r["net_sales"]
        for r in dbc.execute(
            "SELECT date, net_sales FROM daily_sales "
            "WHERE square_location_id=? AND date>=? AND date<=?",
            (sqid, start.isoformat(), end.isoformat()),
        )
    }
    today = business_today()
    stale_from = (today - dt.timedelta(days=1)).isoformat()  # refresh today + yesterday
    need = []
    d = start
    while d <= end:
        ds = d.isoformat()
        if ds not in cached or ds >= stale_from:
            need.append(ds)
        d += dt.timedelta(days=1)
    if need:
        fetch_start = dt.date.fromisoformat(min(need))
        fresh = get_daily_sales(fetch_start, end)  # one paginated Square call
        if fresh is None:
            # The Square call errored. Serve whatever we already have and DO NOT
            # write zeros — overwriting good cached days with 0 would silently
            # and permanently corrupt the Sales history.
            return cached
        d = fetch_start
        while d <= end:
            ds = d.isoformat()
            val = round(fresh.get(ds, 0.0), 2)
            dbc.execute(
                "INSERT INTO daily_sales(square_location_id, date, net_sales, fetched_at) "
                "VALUES(?,?,?, datetime('now')) "
                "ON CONFLICT(square_location_id, date) DO UPDATE SET "
                "net_sales=excluded.net_sales, fetched_at=excluded.fetched_at",
                (sqid, ds, val),
            )
            cached[ds] = val
            d += dt.timedelta(days=1)
        dbc.commit()
    return cached


def _default_wage():
    """Fallback hourly wage (dollars) applied to shifts Square records with no
    rate — typically tipped staff whose base wage isn't set on the shift. 0
    means "no fallback" (and those hours are reported as unwaged)."""
    try:
        return max(float(get_setting("default_hourly_wage") or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def get_labor(start, end):
    """Sum labor cost from Square shifts between start and end (inclusive dates).

    Cost = paid hours * hourly wage, summed across shifts. Unpaid breaks are
    subtracted. Returns dollars plus hours.

    Shifts Square records with no wage (common for tipped staff) would otherwise
    contribute 0 cost and silently understate Labor%. Such hours are billed at
    the `default_hourly_wage` setting when set, and always reported back as
    `unwaged_hours`/`unwaged_shifts` (+ a `warning` when they aren't priced) so
    the figure is transparent rather than quietly low.
    """
    c = _cfg()
    if not is_configured():
        return {"labor": 0.0, "hours": 0.0, "shifts": 0, "unwaged_hours": 0.0,
                "unwaged_shifts": 0, "warning": None, "error": "Square not configured."}
    fallback = _default_wage()
    body = {
        "query": {
            "filter": {
                "location_ids": [c["location_id"]],
                "start": dict(zip(("start_at", "end_at"), _window(start, end))),
            }
        },
        "limit": 200,
    }
    total_cost = 0.0
    total_hours = 0.0
    shift_count = 0
    unwaged_hours = 0.0
    unwaged_shifts = 0
    cursor = None
    try:
        while True:
            if cursor:
                body["cursor"] = cursor
            r = requests.post(
                f"{_base(c['env'])}/v2/labor/shifts/search",
                headers=_headers(c),
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            for s in data.get("shifts", []):
                cost, hours, unwaged = _shift_cost(s, fallback)
                total_cost += cost
                total_hours += hours
                shift_count += 1
                if unwaged > 0:
                    unwaged_hours += unwaged
                    unwaged_shifts += 1
            cursor = data.get("cursor")
            if not cursor:
                break
        # Always disclose unwaged hours: counted as $0 (actionable) when there's
        # no fallback, or estimated at the default wage (informational) when set —
        # either way the labor figure leans on data Square didn't record.
        warning = None
        if unwaged_shifts:
            hrs = round(unwaged_hours, 1)
            if fallback > 0:
                warning = (f"{unwaged_shifts} shift(s) ({hrs} hrs) had no wage in "
                           f"Square and were estimated at the Default Hourly Wage "
                           f"(${fallback:.2f}/hr).")
            else:
                warning = (f"{unwaged_shifts} shift(s) ({hrs} hrs) had no wage in "
                           "Square and counted as $0 labor — set a Default Hourly "
                           "Wage in Settings so Labor% isn't understated.")
        return {
            "labor": round(total_cost, 2),
            "hours": round(total_hours, 2),
            "shifts": shift_count,
            "unwaged_hours": round(unwaged_hours, 2),
            "unwaged_shifts": unwaged_shifts,
            "warning": warning,
            "error": None,
        }
    except requests.RequestException as e:
        return {"labor": 0.0, "hours": 0.0, "shifts": 0, "unwaged_hours": 0.0,
                "unwaged_shifts": 0, "warning": None, "error": _err(e)}


def _parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _shift_cost(shift, fallback_rate=0.0):
    """Return (cost, hours, unwaged_hours) for a shift. A shift with no recorded
    wage is billed at fallback_rate and its hours reported as unwaged so the
    caller can flag the gap (see get_labor)."""
    start = _parse_ts(shift.get("start_at"))
    end = _parse_ts(shift.get("end_at"))
    if not start or not end:
        return 0.0, 0.0, 0.0
    seconds = (end - start).total_seconds()
    # Subtract unpaid breaks.
    for br in shift.get("breaks", []) or []:
        if br.get("is_paid"):
            continue
        b_start = _parse_ts(br.get("start_at"))
        b_end = _parse_ts(br.get("end_at"))
        if b_start and b_end:
            seconds -= (b_end - b_start).total_seconds()
    hours = max(seconds, 0) / 3600.0
    wage = (shift.get("wage") or {}).get("hourly_rate") or {}
    amount = wage.get("amount") or 0  # cents; missing/None/0 => no recorded wage
    if amount > 0:
        return hours * amount / 100.0, hours, 0.0
    return hours * fallback_rate, hours, hours


def _err(e):
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            j = resp.json()
            errs = j.get("errors")
            if errs:
                return "; ".join(x.get("detail", x.get("code", "error")) for x in errs)
        except ValueError:
            pass
        return f"Square HTTP {resp.status_code}"
    return str(e)
