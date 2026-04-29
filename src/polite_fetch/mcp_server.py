"""polite-fetch MCP server (FastMCP).

Exposes one tool: `polite_fetch_url(url, max_attempts, timeout)` that any
MCP client (Claude Code, Cursor, Continue.dev) can call.

Run via:
    polite-fetch-mcp                    # stdio transport (Claude Desktop)
    polite-fetch-mcp --http :8090       # HTTP transport

Requires fastmcp:  pip install polite-fetch[mcp]

Sample claude_desktop_config.json snippet:
    {
      "mcpServers": {
        "polite-fetch": { "command": "polite-fetch-mcp" }
      }
    }
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from . import polite_fetch as _polite_fetch

try:
    from fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    raise SystemExit("fastmcp not installed. Install with: pip install polite-fetch[mcp]") from e


mcp = FastMCP("polite-fetch")


@mcp.tool()
def polite_fetch_url(url: str, max_attempts: int = 4, timeout: int = 30) -> str:
    """Fetch a URL with three-tier escalation, Retry-After honoring, and rate limiting.

    Args:
        url:          URL to fetch.
        max_attempts: Max retries (default 4).
        timeout:      Per-request timeout in seconds (default 30).

    Returns: JSON {ok, status, headers, content, tier, attempts, escalated, reason?}
    """
    result: dict[str, Any] = _polite_fetch(url, max_attempts=max_attempts, timeout=timeout)
    return json.dumps(result)


def run() -> None:
    """Entry point for `polite-fetch-mcp` script."""
    parser = argparse.ArgumentParser(description="polite-fetch MCP server")
    parser.add_argument("--http", metavar="HOST:PORT", help="HTTP transport (default: stdio)")
    args = parser.parse_args()
    if args.http:
        host, _, port = args.http.partition(":")
        mcp.run(transport="http", host=host or "127.0.0.1", port=int(port or 8090))
    else:
        mcp.run()


if __name__ == "__main__":
    run()
