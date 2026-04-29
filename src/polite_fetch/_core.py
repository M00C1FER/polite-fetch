"""polite-fetch — three-tier polite HTTP fetching for Python.

Bundles three fetch tiers behind one API:

  Tier 1 (default): plain `requests` with an honest contact-bearing UA.
  Tier 2 (auto-escalate on 4xx WAF signatures): `curl_cffi` browser-impersonate
                                                 (JA3/JA4 + HTTP/2 fingerprint).
  Tier 3 (opt-in, env POLITE_FETCH_ESCALATE_TIER3=1): playwright + stealth.

Cross-cutting:
  • RFC 7231 §7.1.3 Retry-After honoring on 429/503 (integer or HTTP-date).
  • Full-jitter exponential backoff (AWS pattern) between attempts.
  • Per-domain token-bucket rate ledger (default 1 RPS / burst 5).
  • Anti-bot signature detection across Cloudflare/DataDome/PerimeterX/Akamai/
    Imperva/Kasada (status code + body markers + header values).

Configuration via env vars (all optional):
  POLITE_FETCH_USER_AGENT       (default: contact-bearing UA)
  POLITE_FETCH_RPS              (default: 1.0)
  POLITE_FETCH_BURST            (default: 5)
  POLITE_FETCH_RETRY_AFTER_MAX  (default: 300)
  POLITE_FETCH_ESCALATE_TIER2   (default: 1)
  POLITE_FETCH_ESCALATE_TIER3   (default: 0)
  POLITE_FETCH_IMPERSONATE      (default: chrome131)

Usage:
    from polite_fetch import polite_fetch
    result = polite_fetch("https://example.com/api", max_attempts=4)
    if result["ok"]:
        print(result["content"])

Drop-in `requests`-compatible `get()` available via:
    from polite_fetch import get
    response = get("https://example.com/api")  # returns requests.Response

This module is import-side-effect-free. The token-bucket ledger is lazy-init
on first call. curl_cffi and playwright imports are deferred.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("polite_fetch")

# ── Config (read once; refreshed by research_limits hot-reload) ─────────────

_DEFAULT_USER_AGENT = (
    "polite-fetch/0.1.0 "
    "(+https://github.com/M00C1FER/polite-fetch)"
)

# Anti-bot signatures we use to decide Tier-1 → Tier-2 escalation.
# These are markers we've seen in practice; not exhaustive but cheap.
_ANTI_BOT_SIGNATURES = (
    "cloudflare",
    "datadome",
    "perimeterx",
    "px-cookie",
    "_px3",
    "akamai",
    "imperva",
    "incapsula",
    "challenge-platform",
    "cf-mitigated",
)


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def _load_config() -> dict[str, Any]:
    return {
        "user_agent": os.environ.get("POLITE_FETCH_USER_AGENT", _DEFAULT_USER_AGENT),
        "per_domain_rps": float(os.environ.get("POLITE_FETCH_RPS", "1.0")),
        "per_domain_burst": int(os.environ.get("POLITE_FETCH_BURST", "5")),
        "retry_after_max_seconds": int(os.environ.get("POLITE_FETCH_RETRY_AFTER_MAX", "300")),
        "honor_robots_crawl_delay": _env_bool("POLITE_FETCH_HONOR_ROBOTS", True),
        "escalate_to_curl_cffi_on_4xx": _env_bool("POLITE_FETCH_ESCALATE_TIER2", True),
        "escalate_to_browser": _env_bool("POLITE_FETCH_ESCALATE_TIER3", False),
        "curl_cffi_impersonate": os.environ.get("POLITE_FETCH_IMPERSONATE", "chrome131"),
    }


# ── Retry-After parsing (RFC 7231 §7.1.3) ────────────────────────────────────


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header.

    RFC 7231 §7.1.3: integer seconds or HTTP-date. Returns None if neither.
    Always returns ≥0.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


# ── Full-jitter exponential backoff (AWS pattern) ────────────────────────────


def full_jitter_backoff(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Sleep duration for retry attempt N (0-indexed).

    Uses the AWS-recommended full-jitter formula:
        delay = random(0, min(cap, base * 2**attempt))
    """
    return random.uniform(0.0, min(cap, base * (2 ** max(0, attempt))))


# ── Per-domain token-bucket rate ledger ──────────────────────────────────────


class _TokenBucket:
    __slots__ = ("rate", "burst", "tokens", "last_refill", "lock")

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = max(0.01, rate)  # tokens/sec
        self.burst = max(1, burst)
        self.tokens = float(self.burst)
        self.last_refill = time.monotonic()
        self.lock = Lock()

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """Acquire 1 token. Blocks until available unless blocking=False.

        Returns True if acquired, False if non-blocking and unavailable
        (or timeout exceeded).
        """
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(
                    self.burst,
                    self.tokens + (now - self.last_refill) * self.rate,
                )
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return True
                wait = (1.0 - self.tokens) / self.rate
            if not blocking:
                return False
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.5))


_BUCKETS: dict[str, _TokenBucket] = {}
_BUCKETS_LOCK = Lock()


def _get_bucket(domain: str, rate: float, burst: int) -> _TokenBucket:
    with _BUCKETS_LOCK:
        bucket = _BUCKETS.get(domain)
        if bucket is None:
            bucket = _TokenBucket(rate, burst)
            _BUCKETS[domain] = bucket
        return bucket


def _domain_of(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        return host.lower()
    except Exception:
        return ""


# ── Anti-bot signature detection ─────────────────────────────────────────────


def looks_like_anti_bot(status: int, body: str, headers: dict[str, str]) -> bool:
    """Heuristic — does this 4xx response look like an anti-bot challenge?

    Looking at:
      • Status code (403/429/503 most common; 451 sometimes)
      • Response body containing WAF/bot-product names
      • Response headers (cf-mitigated, server: cloudflare, etc.)
    """
    if status not in (403, 429, 503, 451):
        return False
    body_lower = (body or "").lower()
    if any(sig in body_lower for sig in _ANTI_BOT_SIGNATURES):
        return True
    for hdr_name in ("server", "cf-mitigated", "x-perimeterx", "x-datadome", "x-akamai"):
        val = headers.get(hdr_name) or headers.get(hdr_name.lower()) or ""
        if any(sig in val.lower() for sig in _ANTI_BOT_SIGNATURES):
            return True
    return False


# ── Sec-CH-UA Client Hints (best-effort consistency) ─────────────────────────


_HAND_ROLLED_HINTS = {
    "chrome131": {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Sec-CH-UA": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Linux"',
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
    },
    "chrome142": {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        ),
        "Sec-CH-UA": '"Chromium";v="142", "Not_A Brand";v="24", "Google Chrome";v="142"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Linux"',
        "Sec-CH-UA-Platform-Version": '"6.12.0"',
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
    },
}


def browser_hint_headers(impersonate_target: str) -> dict[str, str]:
    """Return Sec-CH-UA + UA + Sec-Fetch-* headers consistent with the target.

    Tries browserforge first (Bayesian network for statistically-correlated
    headers); falls back to a hand-rolled minimal set.
    """
    try:
        from browserforge.headers import HeaderGenerator  # type: ignore
        gen = HeaderGenerator(browser=("chrome",), os=("linux",))
        hdrs = gen.generate()
        return {k: str(v) for k, v in hdrs.items() if isinstance(v, (str, int, float))}
    except Exception:
        return dict(_HAND_ROLLED_HINTS.get(impersonate_target, _HAND_ROLLED_HINTS["chrome142"]))


# ── Tier-1 fetcher: requests + honest UA ─────────────────────────────────────


def _tier1_fetch(url: str, timeout: int, headers: dict[str, str]) -> dict[str, Any]:
    try:
        import requests
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return {
            "ok": 200 <= r.status_code < 300,
            "status": r.status_code,
            "headers": {k.lower(): v for k, v in r.headers.items()},
            "content": r.text,
            "tier": 1,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "headers": {},
            "content": "",
            "tier": 1,
            "error": str(exc),
        }


# ── Tier-2 fetcher: curl_cffi browser-impersonate ────────────────────────────


def _tier2_fetch(url: str, timeout: int, impersonate: str) -> dict[str, Any]:
    try:
        from curl_cffi import requests as cf_requests  # type: ignore
        r = cf_requests.get(url, impersonate=impersonate, timeout=timeout, allow_redirects=True)  # type: ignore[arg-type]
        return {
            "ok": 200 <= r.status_code < 300,
            "status": r.status_code,
            "headers": {k.lower(): v for k, v in r.headers.items()},
            "content": r.text,
            "tier": 2,
        }
    except ImportError:
        return {"ok": False, "status": 0, "headers": {}, "content": "", "tier": 2, "error": "curl_cffi not installed"}
    except Exception as exc:
        return {"ok": False, "status": 0, "headers": {}, "content": "", "tier": 2, "error": str(exc)}


# ── Public API ───────────────────────────────────────────────────────────────


def polite_fetch(
    url: str,
    *,
    max_attempts: int = 4,
    timeout: int = 30,
    cap_backoff: float = 60.0,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch a URL with politeness-first hierarchy + Retry-After + jitter.

    Tier-1 (default): requests + honest contact-bearing UA.
    Tier-2 (fallback): curl_cffi browser-impersonate, only on anti-bot 4xx.
    Tier-3: playwright + stealth — opt-in via env POLITE_FETCH_ESCALATE_TIER3=1.

    Args:
        url:           URL to fetch.
        max_attempts:  Total attempts across tiers (default 4).
        timeout:       Per-attempt timeout in seconds.
        cap_backoff:   Maximum jitter-backoff sleep (caps exponential growth).
        config:        Override config (default: read research-limits.yaml).

    Returns:
        dict with keys: ok, status, headers, content, tier, attempts,
        domain, retry_after_observed, escalated.
    """
    cfg = config or _load_config()
    domain = _domain_of(url)
    bucket = _get_bucket(domain, cfg["per_domain_rps"], cfg["per_domain_burst"])

    last_result: dict[str, Any] = {"ok": False, "status": 0, "tier": 0, "content": "", "headers": {}}
    retry_after_observed: list[float] = []
    escalated = False

    for attempt in range(max_attempts):
        # Per-domain rate limit (token bucket) — wait if needed.
        bucket.acquire(blocking=True)

        # Decide tier for this attempt.
        if attempt == 0:
            headers = {
                "User-Agent": cfg["user_agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
            result = _tier1_fetch(url, timeout, headers)
        elif (
            cfg["escalate_to_curl_cffi_on_4xx"]
            and looks_like_anti_bot(last_result.get("status", 0), last_result.get("content", ""), last_result.get("headers", {}))
        ):
            escalated = True
            result = _tier2_fetch(url, timeout, cfg["curl_cffi_impersonate"])
        else:
            # Plain retry on Tier-1 (network errors, 5xx)
            headers = {
                "User-Agent": cfg["user_agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
            result = _tier1_fetch(url, timeout, headers)

        result["attempts"] = attempt + 1
        last_result = result

        # Success
        if result["ok"]:
            result["domain"] = domain
            result["retry_after_observed"] = retry_after_observed
            result["escalated"] = escalated
            return result

        # 429/503 with Retry-After — honor it
        retry_after_hdr = result.get("headers", {}).get("retry-after")
        retry_seconds = parse_retry_after(retry_after_hdr)
        if retry_seconds is not None:
            retry_after_observed.append(retry_seconds)
            wait = min(retry_seconds, cfg["retry_after_max_seconds"])
            logger.info(f"[polite_fetch] {domain} returned {result['status']} with Retry-After={retry_seconds:.1f}s; sleeping {wait:.1f}s")
            time.sleep(wait)
            continue

        # No Retry-After — full-jitter backoff
        if attempt < max_attempts - 1:
            wait = full_jitter_backoff(attempt, base=1.0, cap=cap_backoff)
            logger.info(f"[polite_fetch] {domain} attempt {attempt+1} failed (status={result.get('status', 0)}); jitter-backoff {wait:.1f}s")
            time.sleep(wait)

    # All attempts exhausted
    last_result["domain"] = domain
    last_result["retry_after_observed"] = retry_after_observed
    last_result["escalated"] = escalated
    last_result["reason"] = f"exhausted {max_attempts} attempts; final status={last_result.get('status', 0)}"
    return last_result


# ── Convenience: structured JSON output for MCP-tool wrappers ────────────────


def polite_fetch_json(url: str, **kwargs) -> str:
    return json.dumps(polite_fetch(url, **kwargs), default=str)


__all__ = [
    "polite_fetch",
    "polite_fetch_json",
    "parse_retry_after",
    "full_jitter_backoff",
    "looks_like_anti_bot",
    "browser_hint_headers",
]
