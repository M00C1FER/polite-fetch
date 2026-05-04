# References

Peer projects and standards that informed `polite-fetch` design decisions.

## Standards

### RFC 9309 — Robots Exclusion Protocol (2022)
*Koster, M., Illyes, G., Zeller, H., Websling, L.*
<https://www.rfc-editor.org/rfc/rfc9309>

The formal specification for `robots.txt`. Defines status-code semantics for
fetching robots.txt (200/401/403/404), the `User-agent` and `Disallow`/`Allow`
directive grammar, and the `Crawl-delay` extension field we implement. Our
`_fetch_and_parse_robots()` follows §2.2.3 exactly.

### RFC 7231 §7.1.3 — Retry-After
*Fielding, R., Reschke, J.*
<https://www.rfc-editor.org/rfc/rfc7231#section-7.1.3>

Governs `parse_retry_after()` — supports both integer-seconds and HTTP-date
forms of the `Retry-After` header returned by 429/503 responses.

---

## Peer Libraries

### Scrapy — Download Middleware (robots.txt + download delays)
*Scrapy project contributors*
<https://docs.scrapy.org/en/latest/topics/downloader-middleware.html>
<https://docs.scrapy.org/en/latest/topics/settings.html#download-delay>

Scrapy's `RobotsTxtMiddleware` (enabled via `ROBOTSTXT_OBEY = True`) fetches
robots.txt per domain, caches it, and blocks disallowed URLs. Its
`DOWNLOAD_DELAY` setting feeds a per-domain rate limiter — the same pattern as
our `Crawl-Delay` → token-bucket integration. We adopted the "never speed up"
policy (Scrapy's `RANDOMIZE_DOWNLOAD_DELAY` only adds jitter downward).

### urllib.robotparser — Python Standard Library
*Python Software Foundation*
<https://docs.python.org/3/library/urllib.robotparser.html>

We use `RobotFileParser` (stdlib) for parsing rather than introducing a third-
party dependency. One notable gap: the stdlib parser does not implement
`Request-rate` (an older extension replaced by `Crawl-delay`). We call
`crawl_delay(agent)` to retrieve the delay value.

### reppy — Robots.txt Parsing for Python
*Moz Open Source*
<https://github.com/seomoz/reppy>

Reppy adds LRU caching, sitemaps parsing, and TTL-aware expiry to robots.txt
handling. Its per-domain cache design (keyed on scheme+host) matches our
`_ROBOTS_CACHE` dict. Now archived; we use stdlib instead but the API design
informed our `_get_robots()` interface.

### Trafilatura — Web Content Extraction
*Adrien Barbaresi*
<https://trafilatura.readthedocs.io>

A polite web-extraction library that performs tier-based escalation (native
HTTP → headless browser) for pages blocked by WAF — the same escalation
pattern implemented by our Tier-1/Tier-2/Tier-3 design. Trafilatura respects
`robots.txt` via its own check before content extraction.

### AWS Full-Jitter Exponential Backoff
*Amazon Web Services*
<https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/>

The canonical reference for `full_jitter_backoff()`. The formula
`random(0, min(cap, base × 2ⁿ))` produces the lowest average retransmission
latency while avoiding thundering-herd synchronization.
