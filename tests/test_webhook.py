from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.llm import fallback_result
from app.models import LLMResult

SAMPLE = {
    "leadgen_id": "fake-001",
    "created_time": "2026-10-09T20:00:00+0000",
    "field_data": [
        {"name": "full_name", "values": ["Dana Ruiz"]},
        {"name": "email", "values": ["dana@ruizproperties.com"]},
        {"name": "company_name", "values": ["Ruiz Property Group"]},
        {"name": "number_of_doors", "values": ["2 buildings, 40 units"]},
    ],
}

LLM_ANSWER = LLMResult(
    fit_score=4,
    reason="PM company, door count unclear.",
    reply="Hi Dana, thanks for reaching out! Book a time: [booking link]",
    pain_points=[],
)


@pytest.fixture(autouse=True)
def llm_calls(monkeypatch):
    """Replace the LLM so no test reaches the real API; records its arguments."""
    calls = []

    async def fake_qualify(lead, rule_cap, cap_reason=None):
        calls.append((lead, rule_cap, cap_reason))
        return LLM_ANSWER

    monkeypatch.setattr(main, "qualify", fake_qualify)
    return calls


@pytest.fixture
def events():
    """Order in which the sinks were called."""
    return []


@pytest.fixture(autouse=True)
def saved(monkeypatch, events):
    """Replace the Airtable sink so no test writes to the real base; collects records."""
    records = []

    def fake_save(record):
        events.append("airtable")
        records.append(record)
        return "airtable"

    monkeypatch.setattr(main, "save_lead", fake_save)
    return records


@pytest.fixture
def sent(monkeypatch, events):
    """Replace the Discord sink; collects message contents."""
    messages = []

    async def fake_send(content):
        events.append("discord")
        messages.append(content)
        return True

    monkeypatch.setattr(main, "send_to_discord", fake_send)
    return messages


client = TestClient(main.app)


def test_webhook_acks_and_sends_message(sent, llm_calls):
    # TestClient runs background tasks before returning
    response = client.post("/webhook/facebook-lead", json=SAMPLE)
    assert response.status_code == 200
    assert response.json() == {"status": "received"}

    lead, rule_cap, cap_reason = llm_calls[0]
    assert lead.flags == ["doors_ambiguous"]  # LLM sees the lead after rules ran
    assert rule_cap is None and cap_reason is None

    assert len(sent) == 1
    content = sent[0]
    assert "fake-001" in content
    assert "Name: Dana Ruiz" in content
    assert "Company: Ruiz Property Group" in content
    assert "Doors: 2 buildings, 40 units" in content
    assert "Score: final 4 (LLM 4, rule cap none)" in content
    assert "Reason: PM company, door count unclear." in content
    assert "Flags: doors_ambiguous" in content
    assert "> Hi Dana, thanks for reaching out! Book a time: [booking link]" in content
    assert "Processed in: " in content
    assert "Latency" not in content


def test_rule_cap_lowers_final_score(sent, llm_calls):
    client.post("/webhook/facebook-lead", json={"leadgen_id": "x", "field_data": []})
    assert llm_calls[0][1:] == (1, "no valid email or phone number")
    assert "Score: final 1 (LLM 4, rule cap 1)" in sent[0]
    assert "Flags: no_valid_contact, doors_unknown" in sent[0]
    assert "Name: -" in sent[0]


def test_llm_fallback_message(monkeypatch, sent):
    async def unavailable(lead, rule_cap, cap_reason=None):
        return fallback_result(lead, rule_cap)

    monkeypatch.setattr(main, "qualify", unavailable)
    client.post("/webhook/facebook-lead", json=SAMPLE)
    assert "Score: final none (LLM none, rule cap none)" in sent[0]
    assert "Reason: LLM unavailable" in sent[0]
    assert "> Hi Dana, thanks for reaching out to Blaze!" in sent[0]


def test_discord_then_airtable_with_true_latency(sent, saved, events):
    client.post("/webhook/facebook-lead", json=SAMPLE)
    assert events == ["discord", "airtable"]

    record = saved[0]
    assert record["leadgen_id"] == "fake-001"
    assert record["notify_status"] == "sent"
    received = datetime.fromisoformat(record["received_at"])
    notified = datetime.fromisoformat(record["notified_at"])
    assert notified >= received
    assert record["latency_seconds"] == round((notified - received).total_seconds(), 2)
    assert record["llm_score"] == 4
    assert record["final_score"] == 4
    assert record["rule_cap"] is None
    assert record["reason"] == LLM_ANSWER.reason
    assert record["reply"] == LLM_ANSWER.reply
    assert record["flags"] == "doors_ambiguous"


def test_rule_cap_and_final_score_stored(sent, saved):
    client.post("/webhook/facebook-lead", json={"leadgen_id": "x", "field_data": []})
    assert saved[0]["rule_cap"] == 1
    assert saved[0]["llm_score"] == 4
    assert saved[0]["final_score"] == 1
    assert saved[0]["flags"] == "no_valid_contact, doors_unknown"


def test_discord_failure_still_writes_record(monkeypatch, saved, events):
    async def failing_send(content):
        events.append("discord")
        return False

    monkeypatch.setattr(main, "send_to_discord", failing_send)
    response = client.post("/webhook/facebook-lead", json=SAMPLE)
    assert response.status_code == 200
    assert events == ["discord", "airtable"]
    assert saved[0]["notify_status"] == "failed"
    assert saved[0]["notified_at"] is None
    assert saved[0]["latency_seconds"] is None
    assert saved[0]["reply"] == LLM_ANSWER.reply  # reply kept so a rep can still send it


def test_airtable_error_after_send_is_contained(monkeypatch, sent):
    def broken_save(record):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "save_lead", broken_save)
    response = client.post("/webhook/facebook-lead", json=SAMPLE)
    assert response.status_code == 200
    assert len(sent) == 1  # Discord already went out


def test_processing_error_is_contained(monkeypatch, sent, saved):
    def broken_parse(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "parse_lead", broken_parse)
    response = client.post("/webhook/facebook-lead", json=SAMPLE)
    assert response.status_code == 200
    assert sent == []
    assert saved == []


def test_invalid_json_rejected(sent):
    response = client.post(
        "/webhook/facebook-lead", content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert sent == []


def test_non_object_json_rejected(sent):
    response = client.post("/webhook/facebook-lead", json=[1, 2, 3])
    assert response.status_code == 400
    assert sent == []
