import json
import os
from pathlib import Path

from scrapling import Fetcher

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

# 1. list -> resume_token
page = Fetcher.get(
    "https://claude.ai/v1/code/sessions?statuses=active&statuses=paused&limit=5",
    impersonate="chrome",
    stealthy_headers=True,
    headers=H,
    timeout=20,
)
body = page.body
if isinstance(body, bytes):
    body = body.decode("utf-8", "replace")
token = json.loads(body)["resume_token"]
print("resume_token:", token[:30], "...")

# 2. watch SSE
print("\n=== watch SSE (collect ~6s) ===")
wurl = (
    "https://claude.ai/v1/code/sessions/watch?exclude_tags=-&resume_token="
    + __import__("urllib.parse", fromlist=["quote"]).quote(token)
)
wpage = Fetcher.get(
    wurl,
    impersonate="chrome",
    stealthy_headers=True,
    headers={**H, "accept": "text/event-stream"},
    timeout=20,
)
wbody = wpage.body
if isinstance(wbody, bytes):
    wbody = wbody.decode("utf-8", "replace")
print("watch status:", wpage.status, "len:", len(wbody))
print("head:", wbody[:1500])
