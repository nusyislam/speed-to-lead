"""Load the fake leads in fixtures/leads/."""

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "leads"
EXPECTED_KEY = "_expected"


def fixture_names() -> list[str]:
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))


def load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"Unknown fixture {name!r}. Available: {', '.join(fixture_names())}")
    return json.loads(path.read_text(encoding="utf-8"))


def payload_only(fixture: dict) -> dict:
    """The fixture as Facebook would send it, without our _expected key."""
    return {k: v for k, v in fixture.items() if k != EXPECTED_KEY}
