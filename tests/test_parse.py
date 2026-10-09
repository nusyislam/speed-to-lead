import re
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import DOORS_AMBIGUOUS, Qualification
from app.parse import parse_doors, parse_lead

RECEIVED = datetime(2026, 10, 9, 20, 0, 5, tzinfo=timezone.utc)


def payload(*fields, **top):
    body = {"leadgen_id": "fake-001", "created_time": "2026-10-09T20:00:00+0000"}
    body.update(top)
    body["field_data"] = [{"name": n, "values": [v]} for n, v in fields]
    return body


def test_full_payload():
    lead = parse_lead(
        payload(
            ("full_name", "Dana Ruiz"),
            ("email", "dana@ruizproperties.com"),
            ("phone_number", "+15125550142"),
            ("company_name", "Ruiz Property Group"),
            ("number_of_doors", "180"),
            ("message", "Chasing late rent every month is killing us"),
        ),
        received_at=RECEIVED,
    )
    assert lead.leadgen_id == "fake-001"
    assert lead.created_time == "2026-10-09T20:00:00+0000"
    assert lead.received_at == RECEIVED
    assert lead.name == "Dana Ruiz"
    assert lead.email == "dana@ruizproperties.com"
    assert lead.phone == "+15125550142"
    assert lead.company == "Ruiz Property Group"
    assert lead.doors == 180
    assert lead.doors_raw == "180"
    assert lead.message == "Chasing late rent every month is killing us"
    assert lead.extra == {}
    assert lead.flags == []


def test_received_at_defaults_to_now_utc():
    lead = parse_lead(payload(("full_name", "Dana")))
    assert lead.received_at.tzinfo is not None


# --- missing fields ---

def test_missing_fields_are_none():
    lead = parse_lead(payload(("full_name", "Dana Ruiz")))
    assert lead.email is None
    assert lead.phone is None
    assert lead.company is None
    assert lead.doors is None
    assert lead.doors_raw is None
    assert lead.message is None


@pytest.mark.parametrize("values", [[], [""], ["   "], [None], None])
def test_empty_values_treated_as_missing(values):
    lead = parse_lead({"field_data": [{"name": "email", "values": values}]})
    assert lead.email is None


def test_no_field_data():
    lead = parse_lead({"leadgen_id": "fake-002"})
    assert lead.leadgen_id == "fake-002"
    assert lead.name is None


@pytest.mark.parametrize("body", [{}, {"leadgen_id": None}, {"leadgen_id": ""}, {"leadgen_id": "  "}])
def test_missing_leadgen_id_gets_fallback(body):
    lead = parse_lead(body)
    assert re.fullmatch(r"missing-[0-9a-f]{8}", lead.leadgen_id)
    assert lead.created_time is None


def test_fallback_leadgen_ids_are_unique():
    assert parse_lead({}).leadgen_id != parse_lead({}).leadgen_id


def test_numeric_leadgen_id_coerced_to_string():
    assert parse_lead({"leadgen_id": 12345}).leadgen_id == "12345"


def test_malformed_entries_skipped():
    lead = parse_lead(
        {
            "field_data": [
                "not a dict",
                {"values": ["no name"]},
                {"name": 42, "values": ["bad name"]},
                {"name": "email"},  # no values key
                {"name": "full_name", "values": ["Dana Ruiz"]},
            ]
        }
    )
    assert lead.name == "Dana Ruiz"
    assert lead.email is None
    assert lead.extra == {}


@pytest.mark.parametrize("bad", [None, [], "string"])
def test_non_object_payload_rejected(bad):
    with pytest.raises(ValueError):
        parse_lead(bad)


# --- extra / odd fields ---

def test_unknown_fields_kept_in_extra():
    lead = parse_lead(
        payload(("full_name", "Dana Ruiz"), ("city", "Austin"), ("utm_source", "fb"))
    )
    assert lead.extra == {"city": "Austin", "utm_source": "fb"}


def test_unknown_top_level_keys_ignored():
    lead = parse_lead(payload(("full_name", "Dana"), ad_id="123", form_id="456"))
    assert lead.name == "Dana"


def test_field_names_case_and_whitespace_insensitive():
    lead = parse_lead({"field_data": [{"name": " Email ", "values": ["a@b.com"]}]})
    assert lead.email == "a@b.com"


def test_values_trimmed_and_first_nonempty_used():
    lead = parse_lead({"field_data": [{"name": "email", "values": ["", "  a@b.com  ", "c@d.com"]}]})
    assert lead.email == "a@b.com"


def test_values_as_bare_string():
    lead = parse_lead({"field_data": [{"name": "email", "values": "a@b.com"}]})
    assert lead.email == "a@b.com"


def test_duplicate_field_first_wins():
    lead = parse_lead(payload(("email", "first@x.com"), ("email", "second@x.com")))
    assert lead.email == "first@x.com"


def test_first_and_last_name_fallback():
    lead = parse_lead(payload(("first_name", "Dana"), ("last_name", "Ruiz")))
    assert lead.name == "Dana Ruiz"


def test_full_name_preferred_over_split_name():
    lead = parse_lead(payload(("first_name", "D"), ("full_name", "Dana Ruiz")))
    assert lead.name == "Dana Ruiz"


def test_numeric_value_coerced_to_string():
    lead = parse_lead({"field_data": [{"name": "number_of_doors", "values": [180]}]})
    assert lead.doors_raw == "180"
    assert lead.doors == 180


# --- door counts ---

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("180", 180),
        ("about 200", 200),
        ("200+", 200),
        ("~200", 200),
        ("200 doors", 200),
        ("200 units", 200),
        ("approx. 1,200 units", 1200),
        ("1,200", 1200),
        ("1.5k", 1500),
        ("2K", 2000),
        ("150.0", 150),
        ("0", 0),
    ],
)
def test_parse_doors_single_number(raw, expected):
    assert parse_doors(raw) == (expected, False)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("50-100", 75),
        ("50 - 100", 75),
        ("50–100", 75),  # en dash
        ("50 to 100", 75),
        ("about 50-100 doors", 75),
        ("50-101", 75),  # midpoint rounds down
        ("1,000-2,000", 1500),
        ("1k-2k", 1500),
        ("2-3k", 2500),  # k applies to both ends
    ],
)
def test_parse_doors_range_midpoint(raw, expected):
    assert parse_doors(raw) == (expected, False)


@pytest.mark.parametrize(
    "raw",
    [
        "2 buildings, 40 units",
        "40 units across 2 buildings",
        "50 or 100",
        "50, 100",
        "50-100-150",
        "between 50 and 100",
    ],
)
def test_parse_doors_ambiguous(raw):
    assert parse_doors(raw) == (None, True)


@pytest.mark.parametrize("raw", [None, "", "lots", "two hundred", "n/a", "?"])
def test_parse_doors_unparseable(raw):
    assert parse_doors(raw) == (None, False)


def test_unparseable_doors_keeps_raw_no_flag():
    lead = parse_lead(payload(("number_of_doors", "a bunch")))
    assert lead.doors is None
    assert lead.doors_raw == "a bunch"
    assert lead.flags == []


def test_ambiguous_doors_sets_flag_and_keeps_raw():
    lead = parse_lead(payload(("number_of_doors", "2 buildings, 40 units")))
    assert lead.doors is None
    assert lead.doors_raw == "2 buildings, 40 units"
    assert lead.flags == [DOORS_AMBIGUOUS]


def test_range_doors_keeps_raw_no_flag():
    lead = parse_lead(payload(("number_of_doors", "50 to 100")))
    assert lead.doors == 75
    assert lead.doors_raw == "50 to 100"
    assert lead.flags == []


# --- Qualification schema ---

def test_qualification_reply_300_is_valid():
    assert len(Qualification(fit_score=4, reason="ok", reply="x" * 300).reply) == 300


def test_qualification_valid():
    q = Qualification(fit_score=5, reason="ok", reply="hi", pain_points=["late rent"])
    assert q.fit_score == 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"fit_score": 0},
        {"fit_score": 6},
        {"reason": "x" * 141},
        {"reply": "x" * 301},  # the model gets 300; the sign-off is added after
    ],
)
def test_qualification_rejects_out_of_range(overrides):
    data = {"fit_score": 3, "reason": "ok", "reply": "hi", "pain_points": []} | overrides
    with pytest.raises(ValidationError):
        Qualification(**data)
