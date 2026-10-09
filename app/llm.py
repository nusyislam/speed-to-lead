"""Score a lead and draft its first reply with one Claude call."""

import json
import logging
import os
from pathlib import Path

import anthropic
from pydantic import ValidationError

from app.models import Lead, LLMResult, Qualification

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"
TIMEOUT_SECONDS = 20.0
MAX_ATTEMPTS = 2  # first try + one retry
MAX_TOKENS = 1024
PROMPT_PATH = Path(__file__).parent / "prompts" / "qualify_and_reply.md"

TOOL_NAME = "record_qualification"
# Strict tool schemas can't carry minimum/maximum/maxLength, so the score is an
# enum and the length limits are enforced by the Qualification model instead.
QUALIFY_TOOL = {
    "name": TOOL_NAME,
    "description": "Record the lead's fit score, the reason, the drafted reply, and pain points.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "fit_score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "reason": {"type": "string", "description": "One sentence, at most 140 characters."},
            "reply": {"type": "string", "description": "Plain text, at most 320 characters."},
            "pain_points": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["fit_score", "reason", "reply", "pain_points"],
        "additionalProperties": False,
    },
}

FALLBACK_REASON = "LLM unavailable"
FALLBACK_REPLIES = {
    # Tier 1-2: polite, no sales push, no link
    "low": "Hi {first_name}, thanks for reaching out to Blaze. We've received your message and will be in touch if we can help.",
    # Tier 3: one qualifying question
    "mid": "Hi {first_name}, thanks for reaching out to Blaze! So we can point you the right way: roughly how many doors do you manage today?",
    # No cap: we know nothing about fit, so stay neutral
    "none": "Hi {first_name}, thanks for reaching out to Blaze! Someone from our team will be in touch with you shortly.",
}


class InvalidResponse(Exception):
    """The model answered, but not with a valid record_qualification call."""


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_user_message(lead: Lead, rule_cap: int | None, cap_reason: str | None) -> str:
    lead_data = json.dumps(
        {
            "name": lead.name,
            "company": lead.company,
            "doors_raw": lead.doors_raw,
            "message": lead.message,
            "flags": lead.flags,
        },
        ensure_ascii=False,
        indent=2,
    )
    # Escape angle brackets so lead text can't close the <lead_data> block.
    # < and > are still valid JSON for the same characters.
    lead_data = lead_data.replace("<", "\\u003c").replace(">", "\\u003e")
    cap_line = (
        f"Rule cap: {rule_cap} ({cap_reason})" if rule_cap is not None else "Rule cap: none"
    )
    return f"<lead_data>\n{lead_data}\n</lead_data>\n\n{cap_line}"


def fallback_result(lead: Lead, rule_cap: int | None) -> LLMResult:
    if rule_cap is None:
        tier = "none"
    elif rule_cap <= 2:
        tier = "low"
    else:
        tier = "mid"
    first_name = (lead.name or "").split()[0] if (lead.name or "").strip() else "there"
    return LLMResult(
        fit_score=None,
        reason=FALLBACK_REASON,
        reply=FALLBACK_REPLIES[tier].format(first_name=first_name),
        fallback=True,
    )


def parse_response(response) -> Qualification:
    if response.stop_reason == "refusal":
        raise InvalidResponse("model refused")
    for block in response.content:
        if block.type == "tool_use" and block.name == TOOL_NAME:
            try:
                qualification = Qualification.model_validate(block.input)
            except ValidationError as exc:
                raise InvalidResponse(f"schema validation failed: {exc.error_count()} errors") from exc
            # The prompt forbids em dashes; clean up any that slip through
            # rather than spending a retry on them (never makes the reply longer).
            qualification.reply = qualification.reply.replace(" — ", ", ").replace("—", "-")
            return qualification
    raise InvalidResponse(f"no {TOOL_NAME} call (stop_reason={response.stop_reason})")


def make_client() -> anthropic.AsyncAnthropic:
    # The SDK retries on its own by default; we retry once ourselves, so turn
    # its retries off to keep the worst case at 2 x 20s inside the 60s budget.
    return anthropic.AsyncAnthropic(timeout=TIMEOUT_SECONDS, max_retries=0)


async def qualify(
    lead: Lead,
    rule_cap: int | None,
    cap_reason: str | None = None,
    client: anthropic.AsyncAnthropic | None = None,  # tests pass a mock
) -> LLMResult:
    """Score the lead and draft a reply. Never raises: after two failed
    attempts it returns a template reply chosen by the rule cap."""
    try:
        client = client or make_client()
        model = os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL
        system = load_prompt()
        user_message = build_user_message(lead, rule_cap, cap_reason)
    except Exception as exc:
        log.error("LLM setup failed for %s: %s", lead.leadgen_id, type(exc).__name__)
        return fallback_result(lead, rule_cap)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user_message}],
                tools=[QUALIFY_TOOL],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                output_config={"effort": "low"},
            )
            q = parse_response(response)
            return LLMResult(**q.model_dump())
        except anthropic.APIStatusError as exc:
            log.warning("LLM attempt %d for %s: HTTP %s", attempt, lead.leadgen_id, exc.status_code)
        except anthropic.APITimeoutError:
            log.warning("LLM attempt %d for %s: timed out", attempt, lead.leadgen_id)
        except anthropic.APIConnectionError:
            log.warning("LLM attempt %d for %s: connection error", attempt, lead.leadgen_id)
        except InvalidResponse as exc:
            log.warning("LLM attempt %d for %s: %s", attempt, lead.leadgen_id, exc)
        except Exception as exc:
            log.warning("LLM attempt %d for %s: %s", attempt, lead.leadgen_id, type(exc).__name__)

    log.error("LLM unavailable for %s, using template reply", lead.leadgen_id)
    return fallback_result(lead, rule_cap)
