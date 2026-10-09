"""Run every fixture through parse + rules + the LLM and compare to _expected.

No server, Discord, or Airtable: this calls the Anthropic API only.

    python scripts/eval_scoring.py            # 3 runs per fixture
    python scripts/eval_scoring.py --runs 5
    python scripts/eval_scoring.py --only prompt_injection
"""

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from app.llm import DEFAULT_MODEL, SIGN_OFF, make_client, qualify  # noqa: E402
from app.models import NO_VALID_CONTACT  # noqa: E402
from app.parse import parse_lead  # noqa: E402
from app.rules import apply_rules, final_score  # noqa: E402
from scripts.fixtures import EXPECTED_KEY, fixture_names, load_fixture, payload_only  # noqa: E402

MAX_REPLY_CHARS = 320
BOOKING_LINK = "[booking link]"
EM_DASH = "—"
CONCURRENCY = 5


@dataclass
class RunResult:
    llm_score: int | None
    rule_cap: int | None
    final: int | None
    reason: str
    reply: str
    fallback: bool
    flags: list[str] = field(default_factory=list)
    em_dashes_stripped: int = 0  # a warning, not a failure: qualify already cleaned them up
    problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems


@dataclass
class FixtureResult:
    name: str
    expected_min: int
    expected_max: int
    why: str
    runs: list[RunResult]

    @property
    def passed(self) -> bool:
        return all(run.passed for run in self.runs)

    @property
    def expected(self) -> str:
        if self.expected_min == self.expected_max:
            return str(self.expected_min)
        return f"{self.expected_min}-{self.expected_max}"


def check_reply(reply: str, final: int | None, flags: list[str] = ()) -> list[str]:
    if NO_VALID_CONTACT in flags:
        # Nowhere to send it, so the only correct reply is an empty one
        return ["no valid contact but reply is not empty"] if reply.strip() else []
    if not reply.strip():
        return ["reply is empty but the lead has a valid contact"]

    problems = []
    lines = reply.rstrip().splitlines()
    if len(lines) < 2 or lines[-1] != SIGN_OFF:
        problems.append(f'reply does not end with "{SIGN_OFF}" on its own line')
    if len(reply) > MAX_REPLY_CHARS:
        problems.append(f"reply is {len(reply)} chars (max {MAX_REPLY_CHARS})")
    if EM_DASH in reply:
        problems.append("reply contains an em dash")
    if final is not None:
        has_link = BOOKING_LINK in reply
        if final >= 4 and not has_link:
            problems.append(f"final {final} but reply has no {BOOKING_LINK}")
        if final <= 3 and has_link:
            problems.append(f"final {final} but reply includes {BOOKING_LINK}")
    return problems


def check_run(run: RunResult, expected_min: int, expected_max: int) -> list[str]:
    problems = []
    if run.fallback:
        problems.append("LLM unavailable, template reply used")
    if run.final is None:
        problems.append("no final score")
    elif not expected_min <= run.final <= expected_max:
        problems.append(f"final {run.final} outside expected {expected_min}-{expected_max}")
    return problems + check_reply(run.reply, run.final, run.flags)


async def run_once(payload: dict, expected_min: int, expected_max: int, qualify_fn, client, sem) -> RunResult:
    async with sem:
        rules = apply_rules(parse_lead(payload))
        llm = await qualify_fn(rules.lead, rules.rule_cap, rules.cap_reason, client=client)
    run = RunResult(
        llm_score=llm.fit_score,
        rule_cap=rules.rule_cap,
        final=final_score(llm.fit_score, rules.rule_cap),
        reason=llm.reason,
        reply=llm.reply,
        fallback=llm.fallback,
        flags=rules.lead.flags,
        em_dashes_stripped=llm.em_dashes_stripped,
    )
    run.problems = check_run(run, expected_min, expected_max)
    return run


async def evaluate(names: list[str], runs: int, qualify_fn=qualify, client=None) -> list[FixtureResult]:
    sem = asyncio.Semaphore(CONCURRENCY)
    fixtures = {name: load_fixture(name) for name in names}

    async def one_fixture(name: str) -> FixtureResult:
        fixture = fixtures[name]
        expected = fixture[EXPECTED_KEY]
        payload = payload_only(fixture)
        results = await asyncio.gather(
            *(run_once(payload, expected["min"], expected["max"], qualify_fn, client, sem) for _ in range(runs))
        )
        return FixtureResult(name, expected["min"], expected["max"], expected.get("why", ""), list(results))

    return list(await asyncio.gather(*(one_fixture(name) for name in names)))


def _fmt(value) -> str:
    return "-" if value is None else str(value)


def _show_reply(reply: str, indent: int) -> str:
    """Multi-line replies stay indented under their run; empty ones are labelled."""
    if not reply:
        return "(empty reply)"
    return ("\n" + " " * indent).join(reply.splitlines())


def _dash_note(run: RunResult) -> str:
    n = run.em_dashes_stripped
    return f"{n} em dash{'es' if n != 1 else ''} stripped from the reply"


def render(results: list[FixtureResult]) -> str:
    headers = ["fixture", "expected", "final", "llm", "cap", "em-dash", "result"]
    rows = [
        [
            r.name,
            r.expected,
            " ".join(_fmt(run.final) for run in r.runs),
            " ".join(_fmt(run.llm_score) for run in r.runs),
            _fmt(r.runs[0].rule_cap if r.runs else None),
            " ".join(str(run.em_dashes_stripped) for run in r.runs),
            "PASS" if r.passed else "FAIL",
        ]
        for r in results
    ]
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]

    def line(cells):
        return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()

    out = [line(headers), line("-" * w for w in widths), *(line(row) for row in rows)]
    passed = sum(r.passed for r in results)
    out.append("")
    out.append(f"{passed}/{len(results)} fixtures passed")
    all_runs = [run for r in results for run in r.runs]
    dashed = [run for run in all_runs if run.em_dashes_stripped]
    if dashed:
        total = sum(run.em_dashes_stripped for run in dashed)
        out.append(
            f"Warning: {len(dashed)}/{len(all_runs)} runs had em dashes in the model's reply "
            f"({total} stripped in total). Not counted as failures."
        )

    failures = [(r, i, run) for r in results for i, run in enumerate(r.runs, 1) if not run.passed]
    if failures:
        out += ["", "FAILURES", "========"]
        for r, i, run in failures:
            out.append(f"\n{r.name} run {i} (expected {r.expected}: {r.why})")
            for problem in run.problems:
                out.append(f"  - {problem}")
            out.append(f"  scores: llm {_fmt(run.llm_score)}, cap {_fmt(run.rule_cap)}, final {_fmt(run.final)}")
            out.append(f"  reason: {run.reason}")
            out.append(f"  reply ({len(run.reply)} chars): {_show_reply(run.reply, 2)}")
            if run.em_dashes_stripped:
                out.append(f"  warning: {_dash_note(run)}")

    out += ["", "ALL REPLIES", "==========="]
    for r in results:
        out.append(f"\n{r.name} (expected {r.expected})")
        for i, run in enumerate(r.runs, 1):
            mark = "ok  " if run.passed else "FAIL"
            note = f"  [warning: {_dash_note(run)}]" if run.em_dashes_stripped else ""
            out.append(f"  [{mark}] run {i}: final {_fmt(run.final)} (llm {_fmt(run.llm_score)}) - {run.reason}{note}")
            out.append(" " * 9 + _show_reply(run.reply, 9))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=3, help="runs per fixture (default 3)")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="only these fixtures")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    # Replies can contain characters the Windows console code page can't print
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv()
    names = args.only or fixture_names()
    for name in names:
        load_fixture(name)  # fail fast on a typo
    model = os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL
    print(f"Model {model}, {len(names)} fixtures x {args.runs} runs\n")

    results = asyncio.run(evaluate(names, args.runs, client=make_client()))
    print(render(results))
    if all(run.fallback for r in results for run in r.runs):
        print("\nEvery run fell back to the template reply: check ANTHROPIC_API_KEY and ANTHROPIC_MODEL.")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
