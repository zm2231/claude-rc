import urllib.parse
import os
from pathlib import Path

from curl_cffi import requests

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
r = requests.get(
    "https://claude.ai/v1/code/sessions?statuses=active&statuses=paused&limit=5",
    headers=H,
    impersonate="chrome",
    timeout=20,
)
token = r.json()["resume_token"]
print("resume_token:", token[:30], "...")

# 2. optionally send a message so there's streaming activity
cse = "cse_" + sid.replace("session_", "")
import uuid as _uuid

ev_uuid = str(_uuid.uuid4())
if os.environ.get("PROBE_ALLOW_SEND") == "1":
    send = requests.post(
        f"https://claude.ai/v1/code/sessions/{cse}/events",
        headers={**H, "content-type": "application/json"},
        impersonate="chrome",
        timeout=20,
        json={
            "events": [
                {
                    "payload": {
                        "type": "user",
                        "uuid": ev_uuid,
                        "session_id": sid,
                        "parent_tool_use_id": None,
                        "message": {
                            "role": "user",
                            "content": "sse-probe: reply exactly ACK_CDX_SSE only.",
                        },
                    }
                }
            ]
        },
    )
    print("send:", send.status_code, send.text[:120])
else:
    print("send: skipped (set PROBE_ALLOW_SEND=1 to post a probe message)")

# 3. open watch as a stream, read for ~10s
wurl = (
    "https://claude.ai/v1/code/sessions/watch?exclude_tags=-&resume_token="
    + urllib.parse.quote(token)
)
print("\n=== watch SSE stream ===")
resp = requests.get(
    wurl,
    headers={**H, "accept": "text/event-stream"},
    impersonate="chrome",
    stream=True,
    timeout=30,
)
print("status:", resp.status_code, "ct:", resp.headers.get("content-type"))
chunks = []
start = __import__("time").time()
for raw in resp.iter_content():
    if raw:
        chunks.append(raw.decode("utf-8", "replace"))
    if __import__("time").time() - start > 10:
        break
    body = "".join(chunks)
    print("collected bytes:", len(body))
    print("=== SSE (first 2000) ===")
    print(body[:2000])
