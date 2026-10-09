"""Write one Airtable record per lead, falling back to a local CSV."""

import csv
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyairtable import Api

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = (5, 10)  # (connect, read)
FALLBACK_CSV = Path(__file__).resolve().parents[2] / "leads_fallback.csv"
NOTIFY_SENT = "sent"
NOTIFY_FAILED = "failed"

# Airtable field names from PLAN.md, also the CSV column order
FIELDS = [
    "leadgen_id",
    "received_at",
    "name",
    "company",
    "email",
    "phone",
    "doors",
    "message",
    "reason",
    "reply",
    "llm_score",
    "rule_cap",
    "final_score",
    "notify_status",
    "notified_at",
    "latency_seconds",
    "flags",
]

_csv_lock = threading.Lock()  # background tasks can finish at the same time


def to_utc(dt: datetime) -> datetime:
    """Aware UTC datetime. A naive datetime is assumed to already be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_iso(dt: datetime) -> str:
    """ISO 8601 in UTC with a Z suffix, e.g. 2026-10-09T21:38:05.123Z.
    Airtable stores it as an instant and displays it in the field's time zone."""
    return to_utc(dt).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_record(
    *,
    lead,
    llm,
    rule_cap: int | None,
    final_score: int | None,
    notify_status: str,
    notified_at: datetime | None,
) -> dict[str, Any]:
    """Map a processed lead onto the Airtable fields. None means leave blank."""
    received_at = to_utc(lead.received_at)
    notified_at = to_utc(notified_at) if notified_at else None
    latency = round((notified_at - received_at).total_seconds(), 2) if notified_at else None
    return {
        "leadgen_id": lead.leadgen_id,
        "received_at": utc_iso(received_at),
        "name": lead.name,
        "company": lead.company,
        "email": lead.email,
        "phone": lead.phone,
        "doors": lead.doors,
        "message": lead.message,
        "reason": llm.reason,
        "reply": llm.reply,
        "llm_score": llm.fit_score,
        "rule_cap": rule_cap,
        "final_score": final_score,
        "notify_status": notify_status,
        "notified_at": utc_iso(notified_at) if notified_at else None,
        "latency_seconds": latency,
        "flags": ", ".join(lead.flags),
    }


def _airtable_table():
    token = os.getenv("AIRTABLE_TOKEN")
    base_id = os.getenv("AIRTABLE_BASE_ID")
    table_name = os.getenv("AIRTABLE_TABLE_NAME") or "Leads"
    if not token or not base_id:
        return None
    return Api(token, timeout=TIMEOUT_SECONDS).table(base_id, table_name)


def write_csv(record: dict[str, Any], path: Path = FALLBACK_CSV) -> bool:
    try:
        with _csv_lock:
            new_file = not path.exists() or path.stat().st_size == 0
            with path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                if new_file:
                    writer.writeheader()
                writer.writerow({k: "" if v is None else v for k, v in record.items()})
        return True
    except Exception as exc:
        log.error("CSV fallback failed for %s: %s", record.get("leadgen_id"), type(exc).__name__)
        return False


def save_lead(record: dict[str, Any], csv_path: Path = FALLBACK_CSV) -> str:
    """Create the Airtable record, or append it to the CSV if that fails.
    Returns "airtable", "csv", or "failed". Never raises.

    Blocking (pyairtable is sync): call it with asyncio.to_thread."""
    leadgen_id = record.get("leadgen_id")
    try:
        table = _airtable_table()
        if table is None:
            log.warning("Airtable not configured, writing %s to CSV", leadgen_id)
        else:
            # Airtable rejects null for some field types; omit blanks instead
            fields = {k: v for k, v in record.items() if v is not None and v != ""}
            table.create(fields, typecast=True)
            return "airtable"
    except Exception as exc:
        # Only the type and status: never the exception text, which can carry
        # request details. The token itself is only ever in a header.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        log.error(
            "Airtable write failed for %s: %s%s",
            leadgen_id, type(exc).__name__, f" (HTTP {status})" if status else "",
        )
    return "csv" if write_csv(record, csv_path) else "failed"
