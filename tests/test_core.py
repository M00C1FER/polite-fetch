"""Tests for polite_fetch."""
from __future__ import annotations

import time

from polite_fetch import _core as pf


# ── parse_retry_after ────────────────────────────────────────────────────────


def test_parse_retry_after_integer_seconds():
    assert pf.parse_retry_after("30") == 30.0
    assert pf.parse_retry_after("0") == 0.0
    assert pf.parse_retry_after("300") == 300.0


def test_parse_retry_after_clamps_to_zero():
    # Negative → 0
    assert pf.parse_retry_after("-5") == 0.0


def test_parse_retry_after_invalid():
    assert pf.parse_retry_after("") is None
    assert pf.parse_retry_after(None) is None
    assert pf.parse_retry_after("not-a-date") is None


def test_parse_retry_after_http_date_future():
    # 60 seconds in the future
    future = time.time() + 60
    from email.utils import formatdate
    val = formatdate(future, usegmt=True)
    parsed = pf.parse_retry_after(val)
    assert parsed is not None
    assert 55 <= parsed <= 65  # rounding tolerance


def test_parse_retry_after_http_date_past():
    # Past dates should clamp to 0
    past = time.time() - 60
    from email.utils import formatdate
    val = formatdate(past, usegmt=True)
    parsed = pf.parse_retry_after(val)
    assert parsed == 0.0


# ── full_jitter_backoff ──────────────────────────────────────────────────────


def test_full_jitter_backoff_attempts_increase_cap():
    # attempt=0: range [0, 1]
    # attempt=3: range [0, 8]
    # attempt=10: range [0, cap=60]
    for _ in range(50):
        assert 0.0 <= pf.full_jitter_backoff(0, base=1.0, cap=60.0) <= 1.0
        assert 0.0 <= pf.full_jitter_backoff(3, base=1.0, cap=60.0) <= 8.0
        assert 0.0 <= pf.full_jitter_backoff(10, base=1.0, cap=60.0) <= 60.0


def test_full_jitter_backoff_negative_attempt():
    # attempt=-1 should be treated as 0
    val = pf.full_jitter_backoff(-1, base=1.0, cap=60.0)
    assert 0.0 <= val <= 1.0


# ── looks_like_anti_bot ──────────────────────────────────────────────────────


def test_looks_like_anti_bot_status_filter():
    # Only 4xx/503 considered
    assert not pf.looks_like_anti_bot(200, "cloudflare", {})
    assert not pf.looks_like_anti_bot(404, "page not found", {})
    assert pf.looks_like_anti_bot(403, "Cloudflare challenge", {})


def test_looks_like_anti_bot_body_signature():
    # Body must contain one of the _ANTI_BOT_SIGNATURES (cloudflare/datadome/etc.)
    assert pf.looks_like_anti_bot(403, "<html>cloudflare challenge — verifying</html>", {})
    assert pf.looks_like_anti_bot(429, "datadome blocked your request", {})
    assert pf.looks_like_anti_bot(503, "<title>perimeterx challenge</title>", {})


def test_looks_like_anti_bot_header_signature():
    # Header VALUE (not name) must contain a signature
    assert pf.looks_like_anti_bot(403, "", {"server": "cloudflare"})
    # x-datadome header VALUE containing "datadome" → match
    assert pf.looks_like_anti_bot(429, "", {"x-datadome": "datadome challenge"})


def test_looks_like_anti_bot_clean_4xx():
    # 404 with no anti-bot signal → not a bot challenge
    assert not pf.looks_like_anti_bot(404, "Page not found", {"server": "nginx"})


# ── _TokenBucket ─────────────────────────────────────────────────────────────


def test_token_bucket_burst_drains():
    bucket = pf._TokenBucket(rate=1.0, burst=3)
    # Initial burst — 3 tokens available
    assert bucket.acquire(blocking=False)
    assert bucket.acquire(blocking=False)
    assert bucket.acquire(blocking=False)
    # 4th immediate request fails (no time for refill)
    assert not bucket.acquire(blocking=False)


def test_token_bucket_refills_over_time():
    bucket = pf._TokenBucket(rate=10.0, burst=1)  # 10/sec
    assert bucket.acquire(blocking=False)
    # Wait 0.15s — should have refilled enough for one more
    time.sleep(0.15)
    assert bucket.acquire(blocking=False)


def test_token_bucket_blocking_with_timeout():
    bucket = pf._TokenBucket(rate=1.0, burst=1)
    bucket.acquire(blocking=False)  # drain
    # 100ms timeout, 1 token/sec — won't refill in time
    assert not bucket.acquire(blocking=True, timeout=0.1)


# ── polite_fetch integration (mocked tier-1) ─────────────────────────────────


def test_polite_fetch_success_first_try(monkeypatch):
    """200 response on first attempt: returns immediately, no escalation."""
    def fake_tier1(url, timeout, headers):
        return {
            "ok": True,
            "status": 200,
            "headers": {},
            "content": "<html>ok</html>",
            "tier": 1,
        }
    monkeypatch.setattr(pf, "_tier1_fetch", fake_tier1)
    result = pf.polite_fetch("https://example.com/page", max_attempts=4)
    assert result["ok"]
    assert result["status"] == 200
    assert result["tier"] == 1
    assert result["attempts"] == 1
    assert not result["escalated"]


def test_polite_fetch_429_with_retry_after(monkeypatch):
    """429 with Retry-After: sleeps the indicated time and retries."""
    state = {"calls": 0}
    def fake_tier1(url, timeout, headers):
        state["calls"] += 1
        if state["calls"] == 1:
            return {
                "ok": False,
                "status": 429,
                "headers": {"retry-after": "1"},
                "content": "rate limited",
                "tier": 1,
            }
        return {
            "ok": True,
            "status": 200,
            "headers": {},
            "content": "ok",
            "tier": 1,
        }
    monkeypatch.setattr(pf, "_tier1_fetch", fake_tier1)
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    result = pf.polite_fetch("https://example.com/page", max_attempts=3)
    assert result["ok"]
    assert result["attempts"] == 2
    # Retry-After value of 1 second was honored
    assert any(0.99 <= s <= 1.01 for s in sleeps)


def test_polite_fetch_anti_bot_escalates_to_tier2(monkeypatch):
    """403 with Cloudflare body → escalate to curl_cffi tier-2."""
    state = {"tier1_calls": 0, "tier2_calls": 0}
    def fake_tier1(url, timeout, headers):
        state["tier1_calls"] += 1
        return {
            "ok": False,
            "status": 403,
            "headers": {"server": "cloudflare"},
            "content": "<html>cloudflare just a moment</html>",
            "tier": 1,
        }
    def fake_tier2(url, timeout, impersonate):
        state["tier2_calls"] += 1
        return {
            "ok": True,
            "status": 200,
            "headers": {},
            "content": "<html>cf-cleared</html>",
            "tier": 2,
        }
    monkeypatch.setattr(pf, "_tier1_fetch", fake_tier1)
    monkeypatch.setattr(pf, "_tier2_fetch", fake_tier2)
    monkeypatch.setattr("time.sleep", lambda s: None)
    result = pf.polite_fetch("https://example.com/page", max_attempts=3)
    assert result["ok"]
    assert result["tier"] == 2
    assert result["escalated"]
    assert state["tier1_calls"] == 1
    assert state["tier2_calls"] == 1


def test_polite_fetch_exhausted_attempts(monkeypatch):
    """All attempts fail → returns last_result with reason."""
    def fake_tier1(url, timeout, headers):
        return {
            "ok": False,
            "status": 500,
            "headers": {},
            "content": "server error",
            "tier": 1,
        }
    monkeypatch.setattr(pf, "_tier1_fetch", fake_tier1)
    monkeypatch.setattr("time.sleep", lambda s: None)
    result = pf.polite_fetch("https://example.com/page", max_attempts=2)
    assert not result["ok"]
    assert "exhausted" in result["reason"]
    assert result["attempts"] == 2


# ── Browser hint headers ─────────────────────────────────────────────────────


def test_browser_hint_headers_returns_dict():
    hdrs = pf.browser_hint_headers("chrome142")
    assert isinstance(hdrs, dict)
    # User-Agent always present (case-insensitive — browserforge may normalize)
    has_ua = any(k.lower() == "user-agent" for k in hdrs)
    assert has_ua, f"no User-Agent in {list(hdrs.keys())}"
