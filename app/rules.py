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


def apply_rules(lead: Lead) -> RuleResult:
    """Run every hard rule. Returns the lowest applicable cap and a copy of
    the lead with any new flags appended (the input lead is not modified)."""
    new_flags: list[str] = []

    caps = [contact_cap(lead), doors_cap(lead.doors)]
    if caps[0] is not None:
        new_flags.append(NO_VALID_CONTACT)
    if lead.doors is None and DOORS_AMBIGUOUS not in lead.flags:
        new_flags.append(DOORS_UNKNOWN)

    applied = [cap for cap in caps if cap is not None]
    flags = lead.flags + [f for f in new_flags if f not in lead.flags]
    return RuleResult(
        rule_cap=min(applied) if applied else None,
        lead=lead.model_copy(update={"flags": flags}),
    )
