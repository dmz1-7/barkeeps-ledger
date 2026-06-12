"""Recipe / plate costing.

A recipe's batch cost is the sum of its ingredient lines, each line costed as
`qty * the linked product's unit_cost`. Per-serving cost = batch cost / yield;
cost% and margin compare that to the menu price. All money is summed exactly via
money (integer cents).

LIMITATION (documented on purpose): there is no unit-conversion engine yet, so a
line's `qty` is expressed in the PRODUCT'S costing unit — the unit its unit_cost
is priced in. A cocktail using 1.5 oz of a bottle-priced spirit records the
fraction of a bottle. Garnishes/food priced "each" are natural (1 lime = qty 1).
A future pass can add purchase-unit -> recipe-unit conversion.
"""
import money
from db import get_db, active_location_id


def _costed_items(db, recipe_id, loc):
    # Scope the product join to the recipe's store too, so a stray cross-store
    # product_id can never cost against another store's price (defense in depth;
    # the write path already refuses foreign products).
    rows = db.execute(
        "SELECT ri.id, ri.product_id, ri.qty, ri.note, "
        "       p.name AS product, p.unit AS unit, p.unit_cost AS unit_cost "
        "FROM recipe_items ri "
        "LEFT JOIN inventory_items p ON p.id = ri.product_id AND p.location_id IS ? "
        "WHERE ri.recipe_id = ? ORDER BY ri.id",
        (loc, recipe_id),
    ).fetchall()
    items = []
    for r in rows:
        qty = r["qty"] or 0
        uc = r["unit_cost"] or 0
        items.append({
            "id": r["id"], "product_id": r["product_id"],
            "product": r["product"], "unit": r["unit"],
            "qty": round(qty, 4), "unit_cost": round(uc, 2),
            "line_cost": money.normalize(qty * uc) or 0.0,
            "note": r["note"],
            # A line whose product was deleted (or never linked) still shows, but
            # contributes $0 and is flagged so the cost isn't silently understated.
            "missing_product": r["product"] is None,
        })
    return items


def _summary(rec, items):
    batch = money.sum_dollars(i["line_cost"] for i in items)
    yld = rec["yield_qty"] if (rec["yield_qty"] or 0) > 0 else 1
    per = money.normalize(batch / yld) or 0.0
    price = rec["menu_price"] or 0
    return {
        "id": rec["id"], "name": rec["name"],
        "menu_price": round(price, 2), "yield_qty": rec["yield_qty"] or 1,
        "notes": rec["notes"],
        "batch_cost": batch,
        "cost_per_serving": per,
        "cost_pct": round(per / price * 100, 1) if price else None,
        "margin": money.normalize(price - per) if price else None,
        "item_count": len(items),
        "missing_products": sum(1 for i in items if i["missing_product"]),
    }


def cost(recipe_id):
    """Full costed view of one recipe (summary + costed ingredient lines), scoped
    to the active store. Returns None if the recipe doesn't exist there."""
    db = get_db()
    loc = active_location_id()
    rec = db.execute(
        "SELECT * FROM recipes WHERE id = ? AND location_id IS ?",
        (recipe_id, loc),
    ).fetchone()
    if not rec:
        return None
    items = _costed_items(db, recipe_id, loc)
    return {**_summary(rec, items), "items": items}


def list_costed():
    """Cost summary for every active recipe in the active store."""
    db = get_db()
    loc = active_location_id()
    recs = db.execute(
        "SELECT * FROM recipes WHERE location_id IS ? AND archived = 0 "
        "ORDER BY name COLLATE NOCASE",
        (loc,),
    ).fetchall()
    return [_summary(r, _costed_items(db, r["id"], loc)) for r in recs]
