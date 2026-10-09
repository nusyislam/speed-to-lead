import pytest
from fastapi.testclient import TestClient

import app.main as main

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


@pytest.fixture
def sent(monkeypatch):
    """Replace the Discord sink; collects message contents."""
    messages = []

    async def fake_send(content):
        messages.append(content)
        return True

    monkeypatch.setattr(main, "send_to_discord", fake_send)
    return messages


client = TestClient(main.app)


def test_webhook_acks_and_sends_placeholder(sent):
    # TestClient runs background tasks before returning
    response = client.post("/webhook/facebook-lead", json=SAMPLE)
    assert response.status_code == 200
    assert response.json() == {"status": "received"}

    assert len(sent) == 1
    content = sent[0]
    assert "fake-001" in content
    assert "Name: Dana Ruiz" in content
    assert "Company: Ruiz Property Group" in content
    assert "Doors: 2 buildings, 40 units" in content
    assert "Rule cap: none" in content
    assert "Flags: doors_ambiguous" in content
    assert "Latency: " in content


def test_rule_cap_and_flags_in_message(sent):
    client.post("/webhook/facebook-lead", json={"leadgen_id": "x", "field_data": []})
    assert "Rule cap: 1" in sent[0]
    assert "Flags: no_valid_contact, doors_unknown" in sent[0]
    assert "Name: -" in sent[0]


def test_discord_failure_still_acks(monkeypatch):
    async def failing_send(content):
        return False

    monkeypatch.setattr(main, "send_to_discord", failing_send)
    response = client.post("/webhook/facebook-lead", json=SAMPLE)
    assert response.status_code == 200


def test_processing_error_is_contained(monkeypatch, sent):
    def broken_parse(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "parse_lead", broken_parse)
    response = client.post("/webhook/facebook-lead", json=SAMPLE)
    assert response.status_code == 200
    assert sent == []


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
