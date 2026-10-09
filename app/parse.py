"""Turn a Facebook Lead Ads payload (field_data shape) into a Lead."""

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.models import Lead

# field_data name -> Lead attribute
FIELD_MAP = {
    "full_name": "name",
    "email": "email",
    "phone_number": "phone",
    "company_name": "company",
    "number_of_doors": "doors_raw",
    "message": "message",
}

DOORS_AMBIGUOUS = "doors_ambiguous"

# A number with optional thousands separators, decimals and a "k" suffix:
# "200", "1,200", "1.5k"
_NUMBER_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(k\b)?", re.IGNORECASE)

# What may sit between the two numbers of a range: "50-100", "50 – 100", "50 to 100"
_RANGE_SEP_RE = re.compile(r"\s*(?:-|–|—|to)\s*", re.IGNORECASE)


def _to_number(match: re.Match, force_k: bool = False) -> float:
    whole, decimals, k_suffix = match.groups()
    value = float(whole.replace(",", "") + (decimals or ""))
    return value * 1000 if k_suffix or force_k else value


def parse_doors(raw: str | None) -> tuple[int | None, bool]:
    """Pull a door count out of free text. Returns (doors, ambiguous).

    - One number: "about 200", "200+", "~200 doors", "1,200", "1.5k" -> that number.
    - A range of two numbers: "50-100", "50 to 100" -> the midpoint (rounded down).
    - Any other text with two or more numbers ("2 buildings, 40 units")
      -> (None, True), since we can't tell which number is the door count.
    - No number at all ("lots", "two hundred") -> (None, False).
    """
    if not raw:
        return None, False
    matches = list(_NUMBER_RE.finditer(raw))
    if not matches:
        return None, False
    if len(matches) == 1:
        return int(_to_number(matches[0])), False

    if len(matches) == 2:
        first, second = matches
        between = raw[first.end():second.start()]
        if _RANGE_SEP_RE.fullmatch(between):
            # "2-3k" means 2k-3k
            low = _to_number(first, force_k=bool(second.group(3)))
            high = _to_number(second)
            return int((low + high) // 2), False

    return None, True


def _first_value(entry: dict[str, Any]) -> str | None:
    values = entry.get("values")
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return None
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def parse_lead(payload: dict[str, Any], received_at: datetime | None = None) -> Lead:
    """Build a Lead from a payload. Never fails on missing or malformed fields:
    a lead with only a name still needs a reply."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    fields: dict[str, str | None] = {}
    extra: dict[str, str] = {}

    field_data = payload.get("field_data")
    if not isinstance(field_data, list):
        field_data = []

    for entry in field_data:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"].strip().lower()
        value = _first_value(entry)
        if value is None:
            continue
        attr = FIELD_MAP.get(name)
        if attr is None:
            extra.setdefault(name, value)
        else:
            fields.setdefault(attr, value)  # first occurrence wins

    # Some forms split the name instead of using full_name
    if "name" not in fields:
        parts = [extra.get("first_name"), extra.get("last_name")]
        joined = " ".join(p for p in parts if p)
        if joined:
            fields["name"] = joined

    leadgen_id = str(payload.get("leadgen_id") or "").strip()
    if not leadgen_id:
        leadgen_id = f"missing-{uuid.uuid4().hex[:8]}"
    created_time = payload.get("created_time")

    doors, doors_ambiguous = parse_doors(fields.get("doors_raw"))
    flags = [DOORS_AMBIGUOUS] if doors_ambiguous else []

    return Lead(
        leadgen_id=leadgen_id,
        created_time=str(created_time) if created_time is not None else None,
        received_at=received_at or datetime.now(timezone.utc),
        doors=doors,
        extra=extra,
        flags=flags,
        **fields,
    )
