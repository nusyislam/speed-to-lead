import csv
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import requests

from app.models import LLMResult, Lead
from app.sinks import airtable
from app.sinks.airtable import FIELDS, NOTIFY_FAILED, NOTIFY_SENT, build_record, save_lead

TOKEN = "patSECRET123.abcdef"
RECEIVED = datetime(2026, 10, 9, 20, 0, 5, tzinfo=timezone.utc)


def make_record(**overrides):
    lead = Lead(
        leadgen_id="fake-001",
        received_at=RECEIVED,
        name="Dana Ruiz",
        email="dana@ruizproperties.com",
        phone=None,
        company="Ruiz Property Group",
        doors=None,
        doors_raw="2 buildings, 40 units",
        message="Chasing late rent",
        flags=["doors_ambiguous", "other"],
    )
    llm = LLMResult(fit_score=5, reason="Good fit.", reply="Hi Dana [booking link]", pain_points=["late rent"])
    kwargs = {
        "lead": lead,
        "llm": llm,
        "rule_cap": 3,
        "final_score": 3,
        "notify_status": NOTIFY_SENT,
        "notified_at": RECEIVED + timedelta(seconds=4.567),
    } | overrides
    return build_record(**kwargs)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AIRTABLE_TOKEN", TOKEN)
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appTEST")
    monkeypatch.setenv("AIRTABLE_TABLE_NAME", "Leads")


@pytest.fixture
def api(monkeypatch):
    """Replace pyairtable.Api; api.table is what .table(...) returns."""
    mock_api = MagicMock()
    mock_api.table = mock_api.return_value.table.return_value
    monkeypatch.setattr(airtable, "Api", mock_api)
    return mock_api


@pytest.fixture
def csv_path(tmp_path):
    return tmp_path / "leads_fallback.csv"


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def http_error(status):
    response = requests.Response()
    response.status_code = status
    response.url = "https://api.airtable.com/v0/appTEST/Leads"
    return requests.HTTPError(f"{status} Client Error for url: {response.url}", response=response)


# --- record mapping ---

def test_build_record_fields():
    record = make_record()
    assert list(record) == FIELDS
    assert record["leadgen_id"] == "fake-001"
    assert record["received_at"] == "2026-10-09T20:00:05.000Z"
    assert record["notified_at"] == "2026-10-09T20:00:09.567Z"
    assert record["latency_seconds"] == 4.57
    assert record["flags"] == "doors_ambiguous, other"
    assert record["llm_score"] == 5
    assert record["rule_cap"] == 3
    assert record["final_score"] == 3
    assert record["notify_status"] == "sent"
    assert record["doors"] is None
    assert record["phone"] is None


def test_build_record_not_notified():
    record = make_record(notify_status=NOTIFY_FAILED, notified_at=None)
    assert record["notify_status"] == "failed"
    assert record["notified_at"] is None
    assert record["latency_seconds"] is None


# --- timezones ---

NEW_YORK = timezone(timedelta(hours=-4), "EDT")  # New York in October; fixed so no tzdata needed


def assert_utc_iso(value):
    assert value.endswith("Z") or value.endswith("+00:00"), value
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0)
    return parsed


@pytest.mark.parametrize(
    "received, notified",
    [
        # aware UTC, as main.py produces
        (RECEIVED, RECEIVED + timedelta(seconds=3)),
        # aware New York time (17:38 EDT == 21:38 UTC)
        (
            datetime(2026, 10, 9, 17, 38, 0, tzinfo=NEW_YORK),
            datetime(2026, 10, 9, 17, 38, 3, tzinfo=NEW_YORK),
        ),
        # naive, treated as UTC
        (datetime(2026, 10, 9, 21, 38, 0), datetime(2026, 10, 9, 21, 38, 3)),
        # mixed naive and aware must not crash the latency calculation
        (datetime(2026, 10, 9, 21, 38, 0), datetime(2026, 10, 9, 21, 38, 3, tzinfo=timezone.utc)),
    ],
)
def test_datetimes_sent_as_utc_with_offset(received, notified):
    record = build_record(
        lead=Lead(leadgen_id="tz", received_at=received),
        llm=LLMResult(fit_score=3, reason="r", reply="hi"),
        rule_cap=None,
        final_score=3,
        notify_status=NOTIFY_SENT,
        notified_at=notified,
    )
    r = assert_utc_iso(record["received_at"])
    n = assert_utc_iso(record["notified_at"])
    assert record["latency_seconds"] == 3.0
    assert (n - r).total_seconds() == 3.0


def test_new_york_time_converted_to_utc_instant():
    record = build_record(
        lead=Lead(leadgen_id="tz", received_at=datetime(2026, 10, 9, 17, 38, 5, tzinfo=NEW_YORK)),
        llm=LLMResult(fit_score=3, reason="r", reply="hi"),
        rule_cap=None,
        final_score=3,
        notify_status=NOTIFY_SENT,
        notified_at=None,
    )
    assert record["received_at"] == "2026-10-09T21:38:05.000Z"


def test_utc_offset_reaches_airtable_and_csv(env, api, csv_path):
    save_lead(make_record(), csv_path)
    fields = api.table.create.call_args.args[0]
    assert_utc_iso(fields["received_at"])
    assert_utc_iso(fields["notified_at"])

    api.table.create.side_effect = http_error(500)
    save_lead(make_record(), csv_path)
    row = read_csv(csv_path)[0]
    assert_utc_iso(row["received_at"])
    assert_utc_iso(row["notified_at"])


# --- success ---

def test_success_creates_record(env, api, csv_path):
    assert save_lead(make_record(), csv_path) == "airtable"

    api.assert_called_once_with(TOKEN, timeout=airtable.TIMEOUT_SECONDS)
    api.return_value.table.assert_called_once_with("appTEST", "Leads")
    api.table.create.assert_called_once()
    fields = api.table.create.call_args.args[0]
    assert api.table.create.call_args.kwargs == {"typecast": True}
    assert fields["leadgen_id"] == "fake-001"
    assert fields["flags"] == "doors_ambiguous, other"
    assert "phone" not in fields and "doors" not in fields  # blanks omitted
    assert not csv_path.exists()


def test_table_name_defaults_to_leads(env, api, csv_path, monkeypatch):
    monkeypatch.delenv("AIRTABLE_TABLE_NAME")
    save_lead(make_record(), csv_path)
    api.return_value.table.assert_called_once_with("appTEST", "Leads")


# --- failure falls back to CSV ---

@pytest.mark.parametrize(
    "error",
    [http_error(422), http_error(401), requests.ConnectionError("down"), requests.Timeout("slow"), RuntimeError("boom")],
)
def test_airtable_failure_falls_back_to_csv(env, api, csv_path, error):
    api.table.create.side_effect = error
    assert save_lead(make_record(), csv_path) == "csv"

    rows = read_csv(csv_path)
    assert len(rows) == 1
    assert list(rows[0]) == FIELDS
    assert rows[0]["leadgen_id"] == "fake-001"
    assert rows[0]["latency_seconds"] == "4.57"
    assert rows[0]["phone"] == ""  # None written as blank
    assert rows[0]["flags"] == "doors_ambiguous, other"


def test_csv_appends_without_repeating_header(env, api, csv_path):
    api.table.create.side_effect = http_error(500)
    save_lead(make_record(), csv_path)
    save_lead(make_record(), csv_path)
    assert len(read_csv(csv_path)) == 2
    assert csv_path.read_text(encoding="utf-8").count("leadgen_id") == 1


def test_message_with_commas_quotes_newlines_round_trips(env, api, csv_path):
    api.table.create.side_effect = http_error(500)
    record = make_record()
    record["message"] = 'Late rent, "always"\nand bookkeeping'
    save_lead(record, csv_path)
    assert read_csv(csv_path)[0]["message"] == 'Late rent, "always"\nand bookkeeping'


@pytest.mark.parametrize("missing", ["AIRTABLE_TOKEN", "AIRTABLE_BASE_ID"])
def test_missing_config_goes_to_csv(env, api, csv_path, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert save_lead(make_record(), csv_path) == "csv"
    api.assert_not_called()
    assert len(read_csv(csv_path)) == 1


def test_api_constructor_failure_goes_to_csv(env, api, csv_path):
    api.side_effect = ValueError("bad token format")
    assert save_lead(make_record(), csv_path) == "csv"


def test_csv_failure_returns_failed_without_raising(env, api, tmp_path):
    api.table.create.side_effect = http_error(500)
    unwritable = tmp_path / "missing-dir" / "leads.csv"
    assert save_lead(make_record(), unwritable) == "failed"


# --- token hygiene ---

def test_token_never_logged(env, api, csv_path, caplog):
    caplog.set_level(logging.DEBUG)
    api.table.create.side_effect = requests.HTTPError(f"401 for token {TOKEN}")
    save_lead(make_record(), csv_path)
    assert caplog.records  # the failure was logged
    assert not any(TOKEN in r.getMessage() for r in caplog.records)


def test_failure_log_has_status_code(env, api, csv_path, caplog):
    api.table.create.side_effect = http_error(422)
    save_lead(make_record(), csv_path)
    assert any("HTTP 422" in r.getMessage() for r in caplog.records)
