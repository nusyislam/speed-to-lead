"""POST fixture leads to the local server.

    python scripts/send_test_lead.py ideal_late_rent
    python scripts/send_test_lead.py --all
    python scripts/send_test_lead.py --list
"""

import argparse
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.fixtures import fixture_names, load_fixture, payload_only  # noqa: E402

DEFAULT_URL = "http://localhost:8000/webhook/facebook-lead"
GAP_SECONDS = 2.0


def send(name: str, url: str, client: httpx.Client) -> bool:
    payload = payload_only(load_fixture(name))
    try:
        response = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        print(f"{name}: request failed ({type(exc).__name__}). Is the server running?")
        return False
    print(f"{name}: HTTP {response.status_code} {response.text}")
    return response.is_success


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("name", nargs="?", help="fixture name (file stem in fixtures/leads)")
    group.add_argument("--all", action="store_true", help=f"send every fixture, {GAP_SECONDS:g}s apart")
    group.add_argument("--list", action="store_true", help="list fixture names")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"webhook URL (default {DEFAULT_URL})")
    args = parser.parse_args(argv)

    if args.list:
        print("\n".join(fixture_names()))
        return 0

    names = fixture_names() if args.all else [args.name]
    ok = True
    with httpx.Client(timeout=10.0) as client:
        for i, name in enumerate(names):
            if i:
                time.sleep(GAP_SECONDS)
            ok = send(name, args.url, client) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
