"""Square API client — pulls sales (Orders) and labor (Shifts).

Kept dependency-light: just `requests`. All calls fail soft — if Square isn't
configured or a call errors, callers get zeros plus an `error` string so the
dashboard can show "Connect Square" instead of crashing.
"""
import datetime as dt
import requests

from db import get_setting

PROD_BASE = "https://connect.squareup.com"
SANDBOX_BASE = "https://connect.squareupsandbox.com"


def _cfg():
    return {
        "token": (get_setting("square_token") or "").strip(),
        "location_id": (get_setting("square_location_id") or "").strip(),
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
                    "closed_at": {
                        "start_at": _iso(start),
                        "end_at": _iso(end + dt.timedelta(days=1)),
                    }
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
                # Prefer net (pre-tax, post-discount) sales for cost ratios.
                net = o.get("net_amounts", {}).get("total_money")
                money = net or o.get("total_money") or {}
                total_cents += money.get("amount", 0)
            cursor = data.get("cursor")
            if not cursor:
                break
        return {"sales": round(total_cents / 100.0, 2), "orders": order_count, "error": None}
    except requests.RequestException as e:
        return {"sales": 0.0, "orders": 0, "error": _err(e)}


def get_daily_sales(start, end):
    """Net sales bucketed by calendar date (local-ish, by closed_at date).

    Returns {iso_date: dollars}. Empty dict if Square isn't configured or errors
    — callers treat missing days as zero.
    """
    c = _cfg()
    if not is_configured():
        return {}
    body = {
        "location_ids": [c["location_id"]],
        "query": {
            "filter": {
                "date_time_filter": {
                    "closed_at": {
                        "start_at": _iso(start),
                        "end_at": _iso(end + dt.timedelta(days=1)),
                    }
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
                ts = _parse_ts(o.get("closed_at"))
                if not ts:
                    continue
                day = ts.date().isoformat()
                net = o.get("net_amounts", {}).get("total_money")
                money = net or o.get("total_money") or {}
                by_day[day] = by_day.get(day, 0.0) + money.get("amount", 0) / 100.0
            cursor = data.get("cursor")
            if not cursor:
                break
        return {d: round(v, 2) for d, v in by_day.items()}
    except requests.RequestException:
        return {}


def get_labor(start, end):
    """Sum labor cost from Square shifts between start and end (inclusive dates).

    Cost = paid hours * hourly wage, summed across shifts. Unpaid breaks are
    subtracted. Returns dollars plus hours.
    """
    c = _cfg()
    if not is_configured():
        return {"labor": 0.0, "hours": 0.0, "shifts": 0, "error": "Square not configured."}
    body = {
        "query": {
            "filter": {
                "location_ids": [c["location_id"]],
                "start": {
                    "start_at": _iso(start),
                    "end_at": _iso(end + dt.timedelta(days=1)),
                },
            }
        },
        "limit": 200,
    }
    total_cost = 0.0
    total_hours = 0.0
    shift_count = 0
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
                cost, hours = _shift_cost(s)
                total_cost += cost
                total_hours += hours
                shift_count += 1
            cursor = data.get("cursor")
            if not cursor:
                break
        return {
            "labor": round(total_cost, 2),
            "hours": round(total_hours, 2),
            "shifts": shift_count,
            "error": None,
        }
    except requests.RequestException as e:
        return {"labor": 0.0, "hours": 0.0, "shifts": 0, "error": _err(e)}


def _parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _shift_cost(shift):
    start = _parse_ts(shift.get("start_at"))
    end = _parse_ts(shift.get("end_at"))
    if not start or not end:
        return 0.0, 0.0
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
    rate = wage.get("amount", 0) / 100.0  # cents -> dollars
    return hours * rate, hours


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
