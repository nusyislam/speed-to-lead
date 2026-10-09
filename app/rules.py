"""Deterministic hard rules that cap the LLM's fit score. Pure functions, no I/O."""

import re
from dataclasses import dataclass

from app.models import DOORS_AMBIGUOUS, DOORS_UNKNOWN, NO_VALID_CONTACT, Lead

# Basic shape only: something@something.tld, no spaces. Whether the address is
# real (test@test.com) is left to the LLM.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MIN_PHONE_DIGITS = 10


@dataclass(frozen=True)
class RuleResult:
    rule_cap: int | None  # lowest cap that applies; None means no cap
    cap_reason: str | None  # why each applied cap applies, "; "-joined; None if no cap
    lead: Lead  # copy of the input lead with rule flags added


def is_valid_email(email: str | None) -> bool:
    return bool(email) and _EMAIL_RE.fullmatch(email.strip()) is not None


def is_valid_phone(phone: str | None) -> bool:
    return bool(phone) and sum(c.isdigit() for c in phone) >= MIN_PHONE_DIGITS


def contact_cap(lead: Lead) -> int | None:
    if is_valid_email(lead.email) or is_valid_phone(lead.phone):
        return None
    return 1


def doors_cap(doors: int | None) -> int | None:
    if doors is None:
        return None
    if doors < 20:
        return 2
    if doors < 50 or doors > 500:
        return 3
    return None


def doors_cap_reason(doors: int) -> str:
    if doors < 20:
        return f"{doors} doors is under 20"
    if doors < 50:
        return f"{doors} doors is below the 50-500 target range"
    return f"{doors} doors is above the 50-500 target range"


def apply_rules(lead: Lead) -> RuleResult:
    """Run every hard rule. Returns the lowest applicable cap, the reasons for
    it, and a copy of the lead with any new flags appended (the input lead is
    not modified)."""
    new_flags: list[str] = []
    caps: list[int] = []
    reasons: list[str] = []

    if (cap := contact_cap(lead)) is not None:
        caps.append(cap)
        reasons.append("no valid email or phone number")
        new_flags.append(NO_VALID_CONTACT)
    if (cap := doors_cap(lead.doors)) is not None:
        caps.append(cap)
        reasons.append(doors_cap_reason(lead.doors))
    if lead.doors is None and DOORS_AMBIGUOUS not in lead.flags:
        new_flags.append(DOORS_UNKNOWN)

    flags = lead.flags + [f for f in new_flags if f not in lead.flags]
    return RuleResult(
        rule_cap=min(caps) if caps else None,
        cap_reason="; ".join(reasons) or None,
        lead=lead.model_copy(update={"flags": flags}),
    )


def final_score(llm_score: int | None, rule_cap: int | None) -> int | None:
    """Rules win: the LLM score capped by the rule cap. No LLM score means no
    final score (a cap is a ceiling, not a score)."""
    if llm_score is None:
        return None
    return llm_score if rule_cap is None else min(llm_score, rule_cap)
