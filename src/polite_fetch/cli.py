"""polite-fetch CLI: `polite-fetch <url> [--max-attempts N] [--json]`."""
from __future__ import annotations

import argparse
import json
import sys

from . import polite_fetch


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="polite-fetch",
        description="Three-tier polite HTTP fetcher with auto-escalation.",
    )
    parser.add_argument("url", help="URL to fetch")
    parser.add_argument("--max-attempts", type=int, default=4, help="Max retries (default: 4)")
    parser.add_argument("--timeout", type=int, default=30, help="Per-request timeout (default: 30)")
    parser.add_argument("--json", action="store_true", help="Emit full result as JSON")
    args = parser.parse_args()

    result = polite_fetch(args.url, max_attempts=args.max_attempts, timeout=args.timeout)
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print(result["content"])
    else:
        print(f"FAIL: {result.get('reason', 'unknown')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
