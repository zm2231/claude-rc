import os
from pathlib import Path

from scrapling.fetchers import Fetcher

cookie_file = Path(os.environ.get("CLAUDE_COOKIE_FILE", "/tmp/claude_cookie.txt"))
cookie = cookie_file.read_text().strip()
sid = os.environ.get("PROBE_SESSION_ID", "session_REPLACE_ME")
if sid == "session_REPLACE_ME":
    raise SystemExit("set PROBE_SESSION_ID=session_... before running this probe")
H = {
    "cookie": cookie,
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://claude.ai",
    "referer": "https://claude.ai/code/" + sid,
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "ccr-byoc-2025-07-29",
    "anthropic-client-platform": "web_claude_ai",
    "anthropic-client-feature": "ccr",
    "anthropic-client-version": "1.0.0",
}

print("=== TIER 1: TLS-impersonating Fetcher on the CF-blocked list GET ===")
url = "https://claude.ai/v1/code/sessions?statuses=active&limit=3"
try:
    page = Fetcher.get(
        url, impersonate="chrome", stealthy_headers=True, headers=H, timeout=20
    )
    body = page.body
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    cf = "Just a moment" in body or "cf-chl" in body
    print("status:", page.status)
    print("CF challenged:", cf)
    print("body head:", body[:200])
except Exception as e:
    print("error:", repr(e)[:200])
