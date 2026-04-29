"""polite-fetch: three-tier polite HTTP fetching for Python."""
from ._core import (
    polite_fetch,
    parse_retry_after,
    full_jitter_backoff,
    looks_like_anti_bot,
    browser_hint_headers,
)
from ._core import _TokenBucket, _tier1_fetch, _tier2_fetch  # noqa: F401  (test-suite imports)

__version__ = "0.1.0"
__all__ = [
    "polite_fetch",
    "parse_retry_after",
    "full_jitter_backoff",
    "looks_like_anti_bot",
    "browser_hint_headers",
    "get",
]


def get(url: str, **kwargs):
    """Drop-in `requests.get` replacement returning a `requests.Response`.

    Auto-escalates from Tier 1 → Tier 2 (curl_cffi) on WAF detection.
    Honors Retry-After. Raises `RuntimeError` if all attempts exhausted.
    """
    import requests
    result = polite_fetch(url, **kwargs)
    if not result["ok"]:
        raise RuntimeError(f"polite_fetch failed: {result.get('reason', 'unknown')}")
    response = requests.Response()
    response.status_code = result["status"]
    response.headers.update(result["headers"])
    response._content = result["content"].encode("utf-8") if isinstance(result["content"], str) else result["content"]
    response.url = url
    return response
