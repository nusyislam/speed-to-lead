import json
import logging
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.llm import qualify
from app.parse import parse_lead
from app.rules import apply_rules, final_score
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


def _show(value: Any) -> str:
    return "none" if value is None else str(value)


async def process_lead(payload: dict[str, Any], received_at: datetime) -> None:
    try:
        lead = parse_lead(payload, received_at=received_at)
        rules = apply_rules(lead)
        lead = rules.lead
        llm = await qualify(lead, rules.rule_cap, rules.cap_reason)
        score = final_score(llm.fit_score, rules.rule_cap)

        # Latency is measured just before sending, since the message includes it
        latency = (datetime.now(timezone.utc) - received_at).total_seconds()
        content = "\n".join(
            [
                f"**New lead** `{lead.leadgen_id}`",
                f"Name: {lead.name or '-'}",
                f"Company: {lead.company or '-'}",
                f"Doors: {lead.doors_raw or '-'}",
                f"Score: final {_show(score)} (LLM {_show(llm.fit_score)}, rule cap {_show(rules.rule_cap)})",
                f"Reason: {llm.reason}",
                f"Flags: {', '.join(lead.flags) or 'none'}",
                f"Reply:\n> {llm.reply}",
                f"Latency: {latency:.2f}s",
            ]
        )
        sent = await send_to_discord(content)
        log.info(
            "Lead %s processed, final=%s, fallback=%s, discord=%s, latency=%.2fs",
            lead.leadgen_id, score, llm.fallback, sent, latency,
        )
    except Exception:
        log.exception("Lead processing failed")
