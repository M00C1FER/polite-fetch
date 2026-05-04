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


# ── robots.txt: _robots_base_url ─────────────────────────────────────────────


def test_robots_base_url_standard():
    assert pf._robots_base_url("https://example.com/some/page") == "https://example.com/robots.txt"


def test_robots_base_url_preserves_port():
    assert pf._robots_base_url("http://example.com:8080/page") == "http://example.com:8080/robots.txt"


# ── robots.txt: _fetch_and_parse_robots ──────────────────────────────────────


def test_fetch_robots_200_parses(monkeypatch):
    """200 response with disallow rule is parsed correctly."""
    class FakeResp:
        status_code = 200
        text = "User-agent: *\nDisallow: /private/"

    def fake_get(url, **kwargs):
        return FakeResp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    parser = pf._fetch_and_parse_robots("https://example.com/robots.txt", "TestBot")
    assert parser is not None
    assert not parser.can_fetch("TestBot", "https://example.com/private/page")
    assert parser.can_fetch("TestBot", "https://example.com/public/page")


def test_fetch_robots_403_disallows_all(monkeypatch):
    """403 response → treat as Disallow: / (RFC 9309 §2.2.3)."""
    class FakeResp:
        status_code = 403
        text = "Forbidden"

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResp())
    parser = pf._fetch_and_parse_robots("https://example.com/robots.txt", "TestBot")
    assert parser is not None
    assert not parser.can_fetch("TestBot", "https://example.com/any/path")


def test_fetch_robots_404_allows_all(monkeypatch):
    """404 response → treat as allow all (RFC 9309 §2.2.3)."""
    class FakeResp:
        status_code = 404
        text = "Not Found"

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResp())
    parser = pf._fetch_and_parse_robots("https://example.com/robots.txt", "TestBot")
    assert parser is not None
    assert parser.can_fetch("TestBot", "https://example.com/any/path")


def test_fetch_robots_network_error_returns_none(monkeypatch):
    """Network error → returns None (caller treats as allow)."""
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("refused")))
    parser = pf._fetch_and_parse_robots("https://example.com/robots.txt", "TestBot")
    assert parser is None


# ── robots.txt: can_fetch ────────────────────────────────────────────────────


def test_can_fetch_allows_when_robots_unreachable(monkeypatch):
    """If robots.txt cannot be fetched, allow (conservative)."""
    monkeypatch.setattr(pf, "_get_robots", lambda url, ua: None)
    assert pf.can_fetch("https://example.com/page", "TestBot")


def test_can_fetch_blocks_disallowed(monkeypatch):
    """can_fetch returns False when robots.txt disallows the path."""
    import urllib.robotparser
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /private/"])
    monkeypatch.setattr(pf, "_get_robots", lambda url, ua: parser)
    assert not pf.can_fetch("https://example.com/private/page", "TestBot")


def test_can_fetch_allows_permitted_path(monkeypatch):
    """can_fetch returns True for paths not in Disallow."""
    import urllib.robotparser
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /private/"])
    monkeypatch.setattr(pf, "_get_robots", lambda url, ua: parser)
    assert pf.can_fetch("https://example.com/public/page", "TestBot")


# ── robots.txt: robots_crawl_delay ───────────────────────────────────────────


def test_robots_crawl_delay_returns_value(monkeypatch):
    """Returns the Crawl-Delay when present in robots.txt."""
    import urllib.robotparser
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Crawl-delay: 5"])
    monkeypatch.setattr(pf, "_get_robots", lambda url, ua: parser)
    delay = pf.robots_crawl_delay("https://example.com/page", "TestBot")
    assert delay == 5.0


def test_robots_crawl_delay_none_when_not_set(monkeypatch):
    """Returns None when Crawl-Delay is absent."""
    import urllib.robotparser
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /private/"])
    monkeypatch.setattr(pf, "_get_robots", lambda url, ua: parser)
    assert pf.robots_crawl_delay("https://example.com/page", "TestBot") is None


def test_robots_crawl_delay_none_when_unreachable(monkeypatch):
    """Returns None when robots.txt is unreachable."""
    monkeypatch.setattr(pf, "_get_robots", lambda url, ua: None)
    assert pf.robots_crawl_delay("https://example.com/page", "TestBot") is None


# ── robots.txt: _apply_robots_crawl_delay ────────────────────────────────────


def test_apply_robots_crawl_delay_slows_bucket():
    """Crawl-Delay slower than bucket rate → bucket rate is lowered."""
    bucket = pf._TokenBucket(rate=2.0, burst=5)  # 2 req/sec
    pf._apply_robots_crawl_delay(bucket, crawl_delay_sec=10.0)  # 0.1 req/sec
    assert abs(bucket.rate - 0.1) < 1e-9


def test_apply_robots_crawl_delay_no_op_when_already_slower():
    """Crawl-Delay faster than bucket rate → bucket rate unchanged."""
    bucket = pf._TokenBucket(rate=0.5, burst=5)  # 0.5 req/sec (2s between req)
    pf._apply_robots_crawl_delay(bucket, crawl_delay_sec=1.0)  # 1 req/sec (faster)
    assert abs(bucket.rate - 0.5) < 1e-9  # unchanged


def test_apply_robots_crawl_delay_none_is_noop():
    """None crawl_delay → bucket unchanged."""
    bucket = pf._TokenBucket(rate=1.0, burst=5)
    pf._apply_robots_crawl_delay(bucket, None)
    assert bucket.rate == 1.0


def test_apply_robots_crawl_delay_zero_is_noop():
    """Zero or negative crawl_delay → bucket unchanged (defensive)."""
    bucket = pf._TokenBucket(rate=1.0, burst=5)
    pf._apply_robots_crawl_delay(bucket, 0.0)
    assert bucket.rate == 1.0


# ── Kasada anti-bot signature ─────────────────────────────────────────────────


def test_kasada_body_detected():
    """Body containing 'kasada' triggers anti-bot detection."""
    assert pf.looks_like_anti_bot(403, "kasada challenge page", {})


def test_kpsdk_body_detected():
    """Body containing 'kpsdk' (Kasada SDK marker) triggers detection."""
    assert pf.looks_like_anti_bot(403, "window.kpsdk = {}", {})


def test_kpsdk_header_detected():
    """Kasada SDK headers (x-kpsdk-ct) trigger anti-bot detection."""
    assert pf.looks_like_anti_bot(403, "", {"x-kpsdk-ct": "kpsdk-challenge-token"})


# ── polite_fetch: robots.txt gate integration ─────────────────────────────────


def test_polite_fetch_blocked_by_robots_txt(monkeypatch):
    """polite_fetch returns blocked result when can_fetch is False."""
    monkeypatch.setattr(pf, "can_fetch", lambda url, ua: False)
    monkeypatch.setattr(pf, "robots_crawl_delay", lambda url, ua: None)
    result = pf.polite_fetch(
        "https://example.com/private/page",
        config={
            "user_agent": "TestBot",
            "per_domain_rps": 1.0,
            "per_domain_burst": 5,
            "honor_robots_crawl_delay": True,
            "escalate_to_curl_cffi_on_4xx": True,
            "escalate_to_browser": False,
            "curl_cffi_impersonate": "chrome131",
            "retry_after_max_seconds": 300,
        },
    )
    assert not result["ok"]
    assert result.get("blocked_by_robots_txt") is True
    assert "robots.txt" in result.get("reason", "")


def test_polite_fetch_robots_not_checked_when_disabled(monkeypatch):
    """When honor_robots_crawl_delay=False, can_fetch is never called."""
    called = {"n": 0}

    def spy_can_fetch(url, ua):
        called["n"] += 1
        return False  # would block if called

    monkeypatch.setattr(pf, "can_fetch", spy_can_fetch)

    def fake_tier1(url, timeout, headers):
        return {"ok": True, "status": 200, "headers": {}, "content": "ok", "tier": 1}

    monkeypatch.setattr(pf, "_tier1_fetch", fake_tier1)

    result = pf.polite_fetch(
        "https://example.com/page",
        config={
            "user_agent": "TestBot",
            "per_domain_rps": 1.0,
            "per_domain_burst": 5,
            "honor_robots_crawl_delay": False,
            "escalate_to_curl_cffi_on_4xx": False,
            "escalate_to_browser": False,
            "curl_cffi_impersonate": "chrome131",
            "retry_after_max_seconds": 300,
        },
    )
    assert result["ok"]
    assert called["n"] == 0  # never called


def test_polite_fetch_applies_crawl_delay(monkeypatch):
    """Crawl-Delay from robots.txt is applied to the domain bucket."""
    applied = {"delay": None}

    def fake_can_fetch(url, ua):
        return True

    def fake_robots_crawl_delay(url, ua):
        return 5.0  # 5 second crawl delay

    orig_apply = pf._apply_robots_crawl_delay

    def spy_apply(bucket, delay):
        applied["delay"] = delay
        orig_apply(bucket, delay)

    monkeypatch.setattr(pf, "can_fetch", fake_can_fetch)
    monkeypatch.setattr(pf, "robots_crawl_delay", fake_robots_crawl_delay)
    monkeypatch.setattr(pf, "_apply_robots_crawl_delay", spy_apply)

    def fake_tier1(url, timeout, headers):
        return {"ok": True, "status": 200, "headers": {}, "content": "ok", "tier": 1}

    monkeypatch.setattr(pf, "_tier1_fetch", fake_tier1)

    pf.polite_fetch(
        "https://crawldelay.example.com/page",
        config={
            "user_agent": "TestBot",
            "per_domain_rps": 10.0,
            "per_domain_burst": 5,
            "honor_robots_crawl_delay": True,
            "escalate_to_curl_cffi_on_4xx": False,
            "escalate_to_browser": False,
            "curl_cffi_impersonate": "chrome131",
            "retry_after_max_seconds": 300,
        },
    )
    assert applied["delay"] == 5.0
