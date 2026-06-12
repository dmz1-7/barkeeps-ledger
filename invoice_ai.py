"""Invoice photo -> structured data, via Claude vision.

Sends a (downscaled) invoice photograph to the Anthropic API and gets back a
strict JSON object: vendor, date, totals, and line items. Uses structured
outputs (`output_config.format`) so the result is guaranteed-parseable JSON,
with a plain-text-JSON fallback for older SDKs.
"""
import base64
import io
import json
import os

from db import get_setting, TAXONOMY

# Pillow is used only to downscale/normalize the image before upload, which
# keeps token cost and latency down. If it's missing we send the raw bytes.
try:
    from PIL import Image
    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False

# Leaf category names from the seeded taxonomy; each line item is tagged with one.
CATEGORY_NAMES = [name for names in TAXONOMY.values() for name in names]
LINE_CATEGORIES = CATEGORY_NAMES + ["Uncategorized"]

# Hint the model toward the right bucket without hard-coding every example.
_TAXONOMY_HINT = "; ".join(f"{t}: {', '.join(cs)}" for t, cs in TAXONOMY.items())

INVOICE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "vendor": {"type": "string", "description": "Supplier / distributor name"},
        "invoice_date": {"type": "string", "description": "ISO date yyyy-mm-dd, or empty string if not visible"},
        "invoice_number": {"type": "string"},
        "subtotal": {"type": ["number", "null"]},
        "tax": {"type": ["number", "null"]},
        "total": {"type": ["number", "null"], "description": "Invoice grand total in dollars"},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"], "description": "e.g. case, bottle, keg, lb, each"},
                    "unit_cost": {"type": ["number", "null"]},
                    "total": {"type": ["number", "null"]},
                    "category": {"type": "string", "enum": LINE_CATEGORIES,
                                 "description": "Best category for THIS line item"},
                },
                "required": ["name", "qty", "unit", "unit_cost", "total", "category"],
            },
        },
    },
    "required": ["vendor", "invoice_date", "invoice_number",
                 "subtotal", "tax", "total", "line_items"],
}

PROMPT = (
    "You are the back-office bookkeeper for a bar. Read this photographed "
    "vendor invoice and extract its contents. Rules:\n"
    "- Money values are plain dollars (e.g. 42.50), no currency symbols.\n"
    "- invoice_date must be ISO yyyy-mm-dd; if you cannot read it, use an empty string.\n"
    "- List every line item you can read. If a field is illegible, use null.\n"
    "- Categorize EACH line item into exactly one category. Available categories "
    f"(grouped by type) — {_TAXONOMY_HINT}. Use 'Uncategorized' only if nothing fits.\n"
    "- A credit memo, return, or refund REDUCES what is owed: enter its amounts as "
    "NEGATIVE dollars (e.g. a $30 keg-deposit refund is total -30.00). Keep a "
    "deposit CHARGED on this invoice positive.\n"
    "- Do not invent values you cannot see."
)


class InvoiceError(RuntimeError):
    pass


def _prep_image(raw_bytes, content_type):
    """Return (base64_str, media_type). Downscale large photos to <=1600px."""
    media_type = content_type or "image/jpeg"
    if not HAVE_PIL:
        return base64.standard_b64encode(raw_bytes).decode(), media_type
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert("RGB")
        max_dim = 1600
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception:
        return base64.standard_b64encode(raw_bytes).decode(), media_type


def parse_invoice(raw_bytes, content_type=None):
    """Parse an invoice image. Returns a dict matching INVOICE_SCHEMA.

    Raises InvoiceError with a friendly message on failure.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise InvoiceError(
            "ANTHROPIC_API_KEY is not set. Add it to your environment so the "
            "ledger can read invoice photos."
        )

    try:
        import anthropic
    except ImportError:
        raise InvoiceError("The 'anthropic' package is not installed.")

    model = (get_setting("ai_model") or "claude-opus-4-8").strip()
    b64, media_type = _prep_image(raw_bytes, content_type)
    client = anthropic.Anthropic(api_key=api_key)

    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        {"type": "text", "text": PROMPT},
    ]

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": INVOICE_SCHEMA}},
        )
    except TypeError:
        # Older SDK without output_config — ask for JSON in the prompt instead.
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            messages=[{"role": "user", "content": content + [
                {"type": "text", "text": "Respond ONLY with a JSON object and nothing else."}
            ]}],
        )
    except anthropic.APIError as e:
        raise InvoiceError(f"Anthropic API error: {getattr(e, 'message', str(e))}")

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    data = _extract_json(text)
    if data is None:
        raise InvoiceError("Could not read a structured result from the image. Try a clearer photo.")
    return _normalize(data)


def _extract_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Pull the first {...} block if the model wrapped it in prose.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _num(v):
    if v is None or v == "":
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _normalize(data):
    items = []
    for it in data.get("line_items") or []:
        if not isinstance(it, dict):
            continue   # the model occasionally returns a non-object line; skip it
        cat = it.get("category")
        if cat not in LINE_CATEGORIES:
            cat = "Uncategorized"
        items.append({
            "name": (it.get("name") or "").strip(),
            "qty": _num(it.get("qty")),
            "unit": (it.get("unit") or None),
            "unit_cost": _num(it.get("unit_cost")),
            "total": _num(it.get("total")),
            "category": cat,
        })
    return {
        "vendor": (data.get("vendor") or "").strip(),
        "invoice_date": (data.get("invoice_date") or "").strip(),
        "invoice_number": (data.get("invoice_number") or "").strip(),
        "subtotal": _num(data.get("subtotal")),
        "tax": _num(data.get("tax")),
        "total": _num(data.get("total")),
        "line_items": items,
    }
