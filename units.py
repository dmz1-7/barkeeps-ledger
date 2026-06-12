"""Unit conversion for recipe costing.

Three incompatible dimensions — volume, weight, count — each maps to a base unit
(ml, g, each) via a factor (how many base units in one of this unit). convert()
returns None across dimensions (you can't turn fluid ounces of gin into pounds)
or for an unrecognised unit, so the caller can fall back and flag the line.

'oz' means FLUID ounce (bar context); weight ounces aren't modelled — use g/lb.
"""

# unit alias -> base-units-per-unit, grouped by dimension.
_VOLUME = {  # base: ml
    "ml": 1.0, "milliliter": 1.0, "millilitre": 1.0, "cc": 1.0,
    "l": 1000.0, "liter": 1000.0, "litre": 1000.0,
    "oz": 29.5735, "floz": 29.5735, "fl oz": 29.5735, "ounce": 29.5735,
    "tsp": 4.92892, "tbsp": 14.7868, "dash": 0.92,
    "cup": 236.588, "pt": 473.176, "pint": 473.176,
    "qt": 946.353, "quart": 946.353, "gal": 3785.41, "gallon": 3785.41,
    "shot": 44.3603, "jigger": 44.3603,           # 1.5 fl oz
}
_WEIGHT = {  # base: g
    "g": 1.0, "gram": 1.0, "gm": 1.0,
    "kg": 1000.0, "kilo": 1000.0, "kilogram": 1000.0,
    "lb": 453.592, "lbs": 453.592, "pound": 453.592,
}
_COUNT = {  # base: each
    "each": 1.0, "ea": 1.0, "unit": 1.0, "ct": 1.0, "count": 1.0, "piece": 1.0,
}

_DIMS = (("volume", _VOLUME), ("weight", _WEIGHT), ("count", _COUNT))


def _resolve(u):
    """(dimension, factor) for a unit alias, or (None, None) if unrecognised."""
    key = (u or "").strip().lower()
    for dim, table in _DIMS:
        if key in table:
            return dim, table[key]
    return None, None


def known(u):
    return _resolve(u)[0] is not None


def convert(qty, from_unit, to_unit):
    """`qty` from_unit expressed in to_unit, within one dimension. Returns None
    if either unit is unknown or they're in different dimensions."""
    fd, ff = _resolve(from_unit)
    td, tf = _resolve(to_unit)
    if fd is None or td is None or fd != td:
        return None
    return qty * ff / tf
