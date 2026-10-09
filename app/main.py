import json
import logging
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.parse import parse_lead
from app.rules import apply_rules
from app.sinks.discord import send_to_discord

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Speed-to-Lead")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/facebook-lead")
async def facebook_lead(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    received_at = datetime.now(timezone.utc)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    # Ack immediately; Facebook expects a fast response
    background_tasks.add_task(process_lead, payload, received_at)
    return {"status": "received"}


async def process_lead(payload: dict[str, Any], received_at: datetime) -> None:
    try:
        lead = parse_lead(payload, received_at=received_at)
        result = apply_rules(lead)
        lead = result.lead

        # Placeholder message until the LLM step; latency is measured just before sending
        latency = (datetime.now(timezone.utc) - received_at).total_seconds()
        content = "\n".join(
            [
                f"**New lead** `{lead.leadgen_id}`",
                f"Name: {lead.name or '-'}",
                f"Company: {lead.company or '-'}",
                f"Doors: {lead.doors_raw or '-'}",
                f"Rule cap: {result.rule_cap if result.rule_cap is not None else 'none'}",
                f"Flags: {', '.join(lead.flags) or 'none'}",
                f"Latency: {latency:.2f}s",
            ]
        )
        sent = await send_to_discord(content)
        log.info("Lead %s processed, discord=%s, latency=%.2fs", lead.leadgen_id, sent, latency)
    except Exception:
        log.exception("Lead processing failed")
