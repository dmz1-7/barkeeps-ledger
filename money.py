"""Money hardening for Barkeep's Ledger.

Money is stored as SQLite REAL (dollars), which is fine for a single value but
drifts below the penny once you sum or compare many of them in float (the
classic 0.1 + 0.2 != 0.3). These helpers route SETTLED amounts — invoice
subtotal/tax/total, line totals, the count's snapshot value, daily net sales —
through integer cents so values are exact to the penny on write, sums don't
accumulate error, and equality is an integer comparison instead of a fuzzy
float tolerance.

Unit RATES (unit_cost, last_purchase_price) are deliberately NOT forced through
here: a per-unit price can be legitimately sub-cent (e.g. $0.33/can in a
24-pack, or a per-pound price), so those columns stay plain REAL.
"""
import math


def to_cents(x, default=0):
    """A dollar amount (number/str/None) -> integer cents, rounded to the penny.
    None/blank/unparseable/non-finite (inf, nan) -> `default` (0 by default — use
    cents_or_none when a missing value must stay distinguishable from $0.00)."""
    if x is None or x == "":
        return default
    try:
        c = float(x) * 100   # multiply first: a huge-but-finite value (1e308) overflows here
    except (TypeError, ValueError):
        return default
    return int(round(c)) if math.isfinite(c) else default


def cents_or_none(x):
    """Like to_cents, but a missing/unparseable/non-finite value returns None
    instead of 0, so a genuinely absent amount stays distinct from $0.00."""
    if x is None or x == "":
        return None
    try:
        c = float(x) * 100
    except (TypeError, ValueError):
        return None
    return int(round(c)) if math.isfinite(c) else None


def to_dollars(cents):
    """Integer cents -> float dollars, exact to the penny."""
    return cents / 100.0


def normalize(x):
    """Round a dollar amount to the penny for storage, preserving None for a
    missing/unparseable value (the settled-amount columns are nullable). This is
    the write-path replacement for a bare float(): it guarantees what lands in
    the DB is an exact to-the-penny value, not float noise from the client."""
    c = cents_or_none(x)
    return None if c is None else c / 100.0


def sum_dollars(values):
    """Exact sum of dollar amounts: accumulate in integer cents, return dollars.
    Avoids the float-summation drift of sum(floats)."""
    return sum(to_cents(v) for v in values) / 100.0


def same_money(a, b, tol_cents=0):
    """True when two dollar amounts are equal to the penny (or within tol_cents)."""
    return abs(to_cents(a) - to_cents(b)) <= tol_cents
