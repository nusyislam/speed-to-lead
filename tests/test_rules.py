from datetime import datetime, timezone

import pytest

from app.models import DOORS_AMBIGUOUS, DOORS_UNKNOWN, NO_VALID_CONTACT, Lead
from app.rules import (
    apply_rules,
    final_score,
    doors_cap,
    is_valid_email,
    is_valid_phone,
)

RECEIVED = datetime(2026, 10, 9, 20, 0, 5, tzinfo=timezone.utc)


def make_lead(**overrides):
    data = {
        "leadgen_id": "fake-001",
        "received_at": RECEIVED,
        "email": "dana@ruizproperties.com",
        "phone": "+15125550142",
        "doors": 180,
        "doors_raw": "180",
    } | overrides
    return Lead(**data)


# --- email / phone validity ---

@pytest.mark.parametrize(
    "email",
    ["dana@ruizproperties.com", "a.b+tag@sub.example.co", "  dana@x.io  ", "test@test.com"],
)
def test_valid_email(email):
    assert is_valid_email(email)


@pytest.mark.parametrize(
    "email",
    [None, "", "dana", "dana@", "@x.com", "dana@x", "dana@@x.com", "dana @x.com", "dana@x.com extra"],
)
def test_invalid_email(email):
    assert not is_valid_email(email)


@pytest.mark.parametrize(
    "phone",
    ["+15125550142", "512-555-0142", "(512) 555 0142", "5125550142", "+44 20 7946 0958"],
)
def test_valid_phone(phone):
    assert is_valid_phone(phone)


@pytest.mark.parametrize("phone", [None, "", "555-0142", "512555014", "call me", "+1 (512)"])
def test_invalid_phone(phone):
    assert not is_valid_phone(phone)


# --- contact rule ---

def test_email_only_is_enough():
    result = apply_rules(make_lead(phone=None))
    assert result.rule_cap is None
    assert NO_VALID_CONTACT not in result.lead.flags


def test_phone_only_is_enough():
    result = apply_rules(make_lead(email="not-an-email"))
    assert result.rule_cap is None
    assert NO_VALID_CONTACT not in result.lead.flags


@pytest.mark.parametrize("email, phone", [(None, None), ("junk", "123"), ("dana@", "555-0142")])
def test_no_valid_contact_caps_at_1(email, phone):
    result = apply_rules(make_lead(email=email, phone=phone))
    assert result.rule_cap == 1
    assert NO_VALID_CONTACT in result.lead.flags


# --- door caps ---

@pytest.mark.parametrize(
    "doors, expected",
    [
        (0, 2),
        (1, 2),
        (19, 2),
        (20, 3),
        (49, 3),
        (50, None),
        (180, None),
        (500, None),
        (501, 3),
        (1000, 3),
        (1001, 3),
        (2000, 3),
        (None, None),
    ],
)
def test_doors_cap(doors, expected):
    assert doors_cap(doors) == expected


def test_in_range_lead_has_no_cap_or_flags():
    result = apply_rules(make_lead())
    assert result.rule_cap is None
    assert result.lead.flags == []


def test_doors_unknown_no_cap_and_flagged():
    result = apply_rules(make_lead(doors=None, doors_raw="lots"))
    assert result.rule_cap is None
    assert result.lead.flags == [DOORS_UNKNOWN]


def test_doors_missing_entirely_flagged_unknown():
    result = apply_rules(make_lead(doors=None, doors_raw=None))
    assert result.lead.flags == [DOORS_UNKNOWN]


def test_doors_ambiguous_no_cap_and_not_flagged_unknown():
    lead = make_lead(doors=None, doors_raw="2 buildings, 40 units", flags=[DOORS_AMBIGUOUS])
    result = apply_rules(lead)
    assert result.rule_cap is None
    assert result.lead.flags == [DOORS_AMBIGUOUS]


# --- combining caps ---

def test_lowest_cap_wins():
    result = apply_rules(make_lead(email=None, phone=None, doors=2000))
    assert result.rule_cap == 1
    assert result.lead.flags == [NO_VALID_CONTACT]


def test_lowest_cap_wins_doors_only():
    assert apply_rules(make_lead(doors=10)).rule_cap == 2


def test_no_contact_and_doors_unknown_both_flagged():
    result = apply_rules(make_lead(email=None, phone=None, doors=None, doors_raw=None))
    assert result.rule_cap == 1
    assert result.lead.flags == [NO_VALID_CONTACT, DOORS_UNKNOWN]


# --- purity ---

def test_input_lead_not_modified():
    lead = make_lead(email=None, phone=None, doors=None)
    apply_rules(lead)
    assert lead.flags == []


def test_existing_flags_kept_and_not_duplicated():
    lead = make_lead(email=None, phone=None, flags=["other", NO_VALID_CONTACT])
    result = apply_rules(lead)
    assert result.lead.flags == ["other", NO_VALID_CONTACT]


def test_rerunning_is_idempotent():
    once = apply_rules(make_lead(email=None, phone=None, doors=None))
    twice = apply_rules(once.lead)
    assert twice.rule_cap == once.rule_cap
    assert twice.lead.flags == once.lead.flags


# --- cap reasons ---

def test_no_cap_no_reason():
    assert apply_rules(make_lead()).cap_reason is None


@pytest.mark.parametrize(
    "doors, reason",
    [
        (5, "5 doors is under 20"),
        (30, "30 doors is below the 50-500 target range"),
        (2000, "2000 doors is above the 50-500 target range"),
    ],
)
def test_doors_cap_reason(doors, reason):
    assert apply_rules(make_lead(doors=doors)).cap_reason == reason


def test_all_cap_reasons_listed():
    result = apply_rules(make_lead(email=None, phone=None, doors=2000))
    assert result.cap_reason == "no valid email or phone number; 2000 doors is above the 50-500 target range"


# --- final score ---

@pytest.mark.parametrize(
    "llm_score, rule_cap, expected",
    [
        (5, None, 5),
        (5, 3, 3),
        (2, 3, 2),
        (3, 3, 3),
        (5, 1, 1),
        (None, None, None),
        (None, 2, None),
    ],
)
def test_final_score(llm_score, rule_cap, expected):
    assert final_score(llm_score, rule_cap) == expected
