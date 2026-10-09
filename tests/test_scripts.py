import asyncio
import json

import httpx
import pytest

from app.models import LLMResult
from app.parse import parse_lead
from app.rules import apply_rules
from scripts import eval_scoring, send_test_lead
from scripts.eval_scoring import RunResult, check_reply, check_run, evaluate, render
from scripts.fixtures import EXPECTED_KEY, fixture_names, load_fixture, payload_only

EXPECTED_FIXTURES = {
    "ideal_late_rent",
    "in_range_no_message",
    "landlord_one_unit",
    "tenant_complaint",
    "junk_gibberish",
    "enterprise_2000",
    "prompt_injection",
}


# --- fixtures ---

def test_all_seven_fixtures_present():
    assert set(fixture_names()) == EXPECTED_FIXTURES


@pytest.mark.parametrize("name", sorted(EXPECTED_FIXTURES))
def test_fixture_shape(name):
    fixture = load_fixture(name)
    expected = fixture[EXPECTED_KEY]
    assert 1 <= expected["min"] <= expected["max"] <= 5
    assert expected["why"]
    payload = payload_only(fixture)
    assert EXPECTED_KEY not in payload
    lead = parse_lead(payload)
    assert lead.leadgen_id.startswith("fake-")
    assert lead.name


@pytest.mark.parametrize(
    "name, cap",
    [
        ("ideal_late_rent", None),
        ("in_range_no_message", None),
        ("landlord_one_unit", 2),
        ("tenant_complaint", None),
        ("junk_gibberish", 1),
        ("enterprise_2000", 3),
        ("prompt_injection", None),  # only the model's judgment stands between it and a 5
    ],
)
def test_fixture_rule_caps(name, cap):
    result = apply_rules(parse_lead(payload_only(load_fixture(name))))
    assert result.rule_cap == cap
    if cap is not None:
        # an expected range above the cap could never pass
        assert load_fixture(name)[EXPECTED_KEY]["max"] <= cap


def test_unknown_fixture_lists_available():
    with pytest.raises(SystemExit, match="Available: .*ideal_late_rent"):
        load_fixture("nope")


# --- reply checks ---

SIGNED = "\nThe Blaze team"
NO_SIGN_OFF = 'reply does not end with "The Blaze team" on its own line'


@pytest.mark.parametrize(
    "reply, final, problems",
    [
        ("Hi Dana, book here: [booking link]" + SIGNED, 5, 0),
        ("Hi Dana, book here: [booking link]" + SIGNED, 4, 0),
        ("Hi Dana, thanks!" + SIGNED, 5, 1),  # missing link
        ("Hi Dana, how many doors? [booking link]" + SIGNED, 3, 1),  # link at 3
        ("Thanks for reaching out." + SIGNED, 1, 0),
        ("Thanks [booking link]" + SIGNED, 2, 1),
        ("x" * 310 + SIGNED, 1, 1),
        ("Hi Dana — thanks" + SIGNED, 1, 1),
        ("x" * 310 + "—" + SIGNED, 3, 2),
        ("Thanks." + SIGNED, None, 0),  # link rule needs a final score
    ],
)
def test_check_reply(reply, final, problems):
    assert len(check_reply(reply, final)) == problems


@pytest.mark.parametrize(
    "reply, ok",
    [
        ("Thanks for reaching out.\nThe Blaze team", True),
        ("Thanks for reaching out.\nThe Blaze team\n", True),  # trailing newline is fine
        ("Thanks for reaching out.\n\nThe Blaze team", True),  # blank line before it is fine
        ("Thanks for reaching out.", False),
        ("Thanks for reaching out. The Blaze team", False),  # not on its own line
        ("Thanks for reaching out.\nthe Blaze team", False),  # not exact
        ("Thanks for reaching out.\nThe Blaze Team", False),
        ("Thanks for reaching out.\n- The Blaze team", False),
        ("Thanks.\nThe Blaze team\nP.S. call us", False),  # something after it
        ("The Blaze team", False),  # sign-off alone is not a reply
    ],
)
def test_sign_off_check(reply, ok):
    assert (NO_SIGN_OFF not in check_reply(reply, 1)) is ok


def test_no_valid_contact_requires_empty_reply():
    flags = ["no_valid_contact", "doors_unknown"]
    assert check_reply("", 1, flags) == []
    assert check_reply("   ", 1, flags) == []
    assert check_reply("Thanks." + SIGNED, 1, flags) == ["no valid contact but reply is not empty"]


def test_empty_reply_with_valid_contact_fails():
    assert check_reply("", 1, ["doors_unknown"]) == ["reply is empty but the lead has a valid contact"]
    assert check_reply("", 5) == ["reply is empty but the lead has a valid contact"]


def test_check_run_out_of_range_and_fallback():
    run = RunResult(llm_score=None, rule_cap=None, final=None, reason="LLM unavailable", reply="Hi" + SIGNED, fallback=True)
    problems = check_run(run, 1, 2)
    assert "LLM unavailable, template reply used" in problems
    assert "no final score" in problems

    run = RunResult(llm_score=5, rule_cap=None, final=5, reason="r", reply="[booking link]" + SIGNED, fallback=False)
    assert check_run(run, 1, 2) == ["final 5 outside expected 1-2"]
    assert check_run(run, 5, 5) == []


def test_check_run_uses_lead_flags():
    run = RunResult(
        llm_score=1, rule_cap=1, final=1, reason="junk", reply="Thanks." + SIGNED,
        fallback=False, flags=["no_valid_contact"],
    )
    assert check_run(run, 1, 1) == ["no valid contact but reply is not empty"]


# --- evaluate with a fake LLM ---

def fake_qualify(scores_by_company, dashes_by_company=None):
    """Returns a fixed score per lead; replies follow the reply rules for the final tier."""
    dashes_by_company = dashes_by_company or {}

    async def qualify(lead, rule_cap, cap_reason=None, client=None):
        score = scores_by_company.get(lead.company, 1)
        tier = score if rule_cap is None else min(score, rule_cap)
        if "no_valid_contact" in lead.flags:
            reply = ""
        elif tier >= 4:
            reply = "Hi, book here: [booking link]" + SIGNED
        else:
            reply = "Hi, thanks for reaching out." + SIGNED
        return LLMResult(
            fit_score=score,
            reason=f"scored {score}",
            reply=reply,
            em_dashes_stripped=dashes_by_company.get(lead.company, 0),
        )

    return qualify


def test_evaluate_pass_and_fail():
    scores = {"Ruiz Property Group": 5, "Morgan Property Co": 5}  # injection fooled the model
    results = asyncio.run(
        evaluate(["ideal_late_rent", "prompt_injection", "enterprise_2000"], 2, qualify_fn=fake_qualify(scores))
    )
    by_name = {r.name: r for r in results}
    assert by_name["ideal_late_rent"].passed
    assert [run.final for run in by_name["ideal_late_rent"].runs] == [5, 5]
    assert not by_name["prompt_injection"].passed
    assert by_name["enterprise_2000"].passed  # llm 1, cap 3, final 1


def test_rule_cap_overrides_llm_in_results():
    results = asyncio.run(
        evaluate(["enterprise_2000"], 1, qualify_fn=fake_qualify({"Summit Ridge Communities": 5}))
    )
    run = results[0].runs[0]
    assert (run.llm_score, run.rule_cap, run.final) == (5, 3, 3)
    assert results[0].passed  # reply was written for tier 3, no link


def test_render_table_failures_and_replies():
    scores = {"Ruiz Property Group": 5, "Morgan Property Co": 5}
    results = asyncio.run(
        evaluate(["ideal_late_rent", "prompt_injection"], 3, qualify_fn=fake_qualify(scores))
    )
    text = render(results)
    lines = text.splitlines()
    assert lines[0].split() == ["fixture", "expected", "final", "llm", "cap", "em-dash", "result"]
    assert any(l.startswith("ideal_late_rent") and l.endswith("PASS") and "5 5 5" in l for l in lines)
    assert any(l.startswith("prompt_injection") and l.endswith("FAIL") and "1-2" in l for l in lines)
    assert "1/2 fixtures passed" in text
    assert "FAILURES" in text
    assert "prompt_injection run 3" in text
    assert "final 5 outside expected 1-2" in text
    assert "ALL REPLIES" in text
    assert text.count("Hi, book here: [booking link]") >= 6


def test_eval_main_exit_code(monkeypatch, capsys):
    async def fake_evaluate(names, runs, client=None):
        return await evaluate(names, runs, qualify_fn=fake_qualify({"Ruiz Property Group": 5}))

    monkeypatch.setattr(eval_scoring, "evaluate", fake_evaluate)
    monkeypatch.setattr(eval_scoring, "make_client", lambda: None)
    assert eval_scoring.main(["--runs", "1", "--only", "ideal_late_rent"]) == 0
    assert eval_scoring.main(["--runs", "1", "--only", "in_range_no_message"]) == 1
    assert "1 fixtures x 1 runs" in capsys.readouterr().out


# --- send_test_lead ---

def test_send_strips_expected_and_posts():
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"status": "received"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert send_test_lead.send("tenant_complaint", "http://test/webhook", client) is True
    url, body = seen[0]
    assert url == "http://test/webhook"
    assert EXPECTED_KEY not in body
    assert body["leadgen_id"] == "fake-004"


def test_send_reports_connection_error(capsys):
    def handler(request):
        raise httpx.ConnectError("refused")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert send_test_lead.send("tenant_complaint", "http://test/webhook", client) is False
    assert "Is the server running?" in capsys.readouterr().out


def test_send_all_with_gap(monkeypatch):
    sent, sleeps = [], []
    monkeypatch.setattr(send_test_lead, "send", lambda name, url, client: sent.append(name) or True)
    monkeypatch.setattr(send_test_lead.time, "sleep", sleeps.append)
    assert send_test_lead.main(["--all"]) == 0
    assert sent == fixture_names()
    assert sleeps == [2.0] * (len(sent) - 1)


def test_send_list(capsys):
    assert send_test_lead.main(["--list"]) == 0
    assert capsys.readouterr().out.split() == fixture_names()


def test_em_dashes_are_a_warning_not_a_failure():
    results = asyncio.run(
        evaluate(
            ["ideal_late_rent", "in_range_no_message"],
            2,
            qualify_fn=fake_qualify(
                {"Ruiz Property Group": 5, "Bayview Residential Management": 4},
                {"Ruiz Property Group": 2},
            ),
        )
    )
    assert all(r.passed for r in results)
    assert [run.em_dashes_stripped for run in results[0].runs] == [2, 2]

    text = render(results)
    row = next(l for l in text.splitlines() if l.startswith("ideal_late_rent"))
    assert row.split()[-3:] == ["2", "2", "PASS"]
    row = next(l for l in text.splitlines() if l.startswith("in_range_no_message"))
    assert row.split()[-3:] == ["0", "0", "PASS"]
    assert "2/2 fixtures passed" in text
    assert "Warning: 2/4 runs had em dashes in the model's reply (4 stripped in total)" in text
    assert "[warning: 2 em dashes stripped from the reply]" in text
    assert "FAILURES" not in text


def test_no_em_dash_warning_when_clean():
    results = asyncio.run(evaluate(["ideal_late_rent"], 1, qualify_fn=fake_qualify({"Ruiz Property Group": 5})))
    assert "Warning" not in render(results)


def test_junk_fixture_expects_empty_reply_and_render_labels_it():
    results = asyncio.run(evaluate(["junk_gibberish"], 1, qualify_fn=fake_qualify({})))
    assert results[0].passed
    assert results[0].runs[0].flags == ["no_valid_contact", "doors_unknown"]
    assert "(empty reply)" in render(results)


def test_render_indents_multiline_replies():
    results = asyncio.run(evaluate(["ideal_late_rent"], 1, qualify_fn=fake_qualify({"Ruiz Property Group": 5})))
    lines = render(results).splitlines()
    i = lines.index("         Hi, book here: [booking link]")
    assert lines[i + 1] == "         The Blaze team"


@pytest.mark.parametrize("rule_cap", [1, 2, 3, None])
def test_fallback_templates_pass_reply_checks(rule_cap):
    """The templates follow the same reply rules the eval enforces."""
    from app.llm import fallback_result

    lead = parse_lead(payload_only(load_fixture("ideal_late_rent")))
    reply = fallback_result(lead, rule_cap).reply
    final = rule_cap if rule_cap is not None else 1  # templates never include a link
    assert check_reply(reply, final, lead.flags) == []
