#!/usr/bin/env python3
"""Headless Claude Code Remote-Control client.

Fully browser-free. Uses curl_cffi (Chrome TLS impersonation) to pass
Cloudflare on every endpoint, and @steipete/sweet-cookie (via a tiny node
bridge) to read the claude.ai session cookie from the Chrome profile.

Capabilities:
  list                          List RC-enabled sessions (registry + live daemon).
  resolve <target>              Resolve a target spec to rc url / cse id / pid.
  send <target> <message>       Send a user message into a live session.
  watch [target]                Stream session state changes (worker_status, summary).
  send+watch <target> <msg>     Send then stream until the turn completes.

All requests hit the official claude.ai/v1/code/sessions/* routes. No tokens,
no OAuth, no browser. Cookies are read on each call, never persisted.
"""

from __future__ import annotations

import os

os.environ.setdefault("PYDANTIC_DISABLE_PLUGIN", "1")
os.environ.setdefault("LOGFIRE_LOG_LEVEL", "ERROR")

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    sys.stderr.write("error: curl_cffi not installed. Run: pip install curl_cffi\n")
    sys.exit(127)


# ---- config ----------------------------------------------------------------

HOME = Path(os.environ.get("HOME") or str(Path.home()))
SESSIONS_DIR = Path(
    os.environ.get("CLAUDE_SESSIONS_DIR", str(HOME / ".claude" / "sessions"))
)
CONTROL_KEY_PATH = HOME / ".claude" / "daemon" / "control.key"
COOKIE_BRIDGE = Path(
    os.environ.get(
        "RC_COOKIE_BRIDGE", str(Path(__file__).resolve().parent / "cookie.mjs")
    )
)
PROFILE = os.environ.get("CLAUDE_COOKIE_PROFILE", "Default")
IMPERSONATE = os.environ.get("RC_IMPERSONATE", "chrome")
BASE = "https://claude.ai/v1/code/sessions"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
ANTHROPIC_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "ccr-byoc-2025-07-29",
    "anthropic-client-platform": "web_claude_ai",
    "anthropic-client-feature": "ccr",
    "anthropic-client-version": "1.0.0",
}


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        die(f"{name} must be a number, got {raw!r}", code=2)


DEFAULT_TIMEOUT = env_float("CLAUDE_RC_TIMEOUT", 120.0)
DEFAULT_SEND_TIMEOUT = env_float("CLAUDE_RC_SEND_TIMEOUT", DEFAULT_TIMEOUT)
DEFAULT_WATCH_TIMEOUT = env_float("CLAUDE_RC_WATCH_TIMEOUT", DEFAULT_TIMEOUT)
DEFAULT_STREAM_TIMEOUT = env_float("CLAUDE_RC_STREAM_TIMEOUT", 600.0)


def die(msg: str, code: int = 1) -> "NoReturn":
    sys.stderr.write(f"error: {msg}\n")
    sys.exit(code)


class CfChallenge(Exception):
    """Raised when Cloudflare returns a managed challenge instead of JSON."""


def _cf_check(body: str, where: str):
    if "Just a moment" in body or "cf-chl" in body:
        raise CfChallenge(
            f"Cloudflare challenge on {where} (cf_clearance expired or re-solved)"
        )


# ---- cookie loading --------------------------------------------------------


def load_cookie_header() -> str:
    """Read claude.ai cookies from Chrome profile via sweet-cookie (node bridge)."""
    if not COOKIE_BRIDGE.exists():
        die(f"cookie bridge not found: {COOKIE_BRIDGE}")
    try:
        out = subprocess.run(
            ["node", str(COOKIE_BRIDGE), PROFILE],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        die(f"cookie read failed: {e.stderr or e.stdout}")
    except FileNotFoundError:
        die("node not on PATH")
    if not out or "sessionKey=" not in out:
        die(
            f"no claude.ai sessionKey cookie in Chrome profile '{PROFILE}'. "
            "Is Chrome logged in?"
        )
    return out


def base_headers(cookie: str, accept: str = "application/json") -> dict:
    return {
        "cookie": cookie,
        "user-agent": BROWSER_UA,
        "accept": accept,
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://claude.ai",
        "referer": "https://claude.ai/code/",
        **ANTHROPIC_HEADERS,
    }


# ---- session registry ------------------------------------------------------


def load_registry() -> list[dict]:
    if not SESSIONS_DIR.is_dir():
        return []
    out = []
    for f in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            raw = json.loads(f.read_text("utf8"))
            if raw.get("bridgeSessionId"):
                out.append(raw)
        except Exception:
            continue
    return out


def to_cse(bridge_id: str) -> str:
    bare = re.sub(r"^https://claude\.ai/code/", "", bridge_id or "")
    bare = bare.removeprefix("session_").removeprefix("cse_")
    return f"cse_{bare}"


def project_dir_from_cwd(cwd: str | None) -> Path | None:
    if not cwd:
        return None
    return HOME / ".claude" / "projects" / cwd.replace("/", "-")


def transcript_path(session: dict) -> str | None:
    d = project_dir_from_cwd(session.get("cwd"))
    sid = session.get("sessionId")
    if d and sid:
        return str(d / f"{sid}.jsonl")
    return None


def target_capability(session: dict) -> dict:
    transcript = transcript_path(session) if not session.get("_url") else None
    has_transcript = bool(transcript and Path(transcript).exists())
    return {
        "transcript": transcript,
        "hasLocalTranscript": has_transcript,
        "replySource": "transcript" if has_transcript else "api",
        "streamSource": "transcript" if has_transcript else "watch",
        "remoteOnly": not has_transcript,
    }


def resolve_target(raw: str) -> dict:
    """Resolve a target spec to a single session record."""
    m = re.match(r"^https://claude\.ai/code/(session_[A-Za-z0-9]+)", raw)
    if m:
        bridge_id = m.group(1)
        for s in load_registry():
            if s.get("bridgeSessionId") == bridge_id:
                return s
        return {
            "bridgeSessionId": bridge_id,
            "sessionId": None,
            "cwd": None,
            "_url": True,
        }
    if raw.startswith(("cse_", "session_")):
        for s in load_registry():
            if (
                s.get("bridgeSessionId") == raw
                or to_cse(s.get("bridgeSessionId", "")) == raw
            ):
                return s
        bid = raw.removeprefix("cse_")
        if not bid.startswith("session_"):
            bid = "session_" + bid
        return {"bridgeSessionId": bid, "sessionId": None, "cwd": None, "_url": True}

    sessions = load_registry()
    q = raw.lower()
    matches = [
        s
        for s in sessions
        if str(s.get("pid")) == raw
        or (s.get("sessionId") or "").startswith(raw)
        or (s.get("bridgeSessionId") or "").startswith(raw)
        or (s.get("cwd") or "").lower().find(q) >= 0
        or (s.get("name") or "").lower().find(q) >= 0
    ]
    if not matches:
        die(f"no RC-enabled Claude session matched '{raw}'\nhint: claude-rc-send list")
    if len(matches) > 1:
        rows = "\n".join(
            f"  pid={m.get('pid')} local={m.get('sessionId')} rc={m.get('bridgeSessionId')} "
            f"cwd={m.get('cwd') or ''} name={(m.get('name') or '')[:40]}"
            for m in matches
        )
        die(
            f"target '{raw}' matched {len(matches)} RC sessions:\n{rows}\n"
            "hint: use PID, local session prefix, or full session_* id"
        )
    return matches[0]


# ---- HTTP ops --------------------------------------------------------------


def http_list_sessions(cookie: str) -> dict:
    """GET /v1/code/sessions -> {data, next_cursor, resume_token}."""
    r = cffi_requests.get(
        f"{BASE}?statuses=active&statuses=paused&limit=50",
        headers=base_headers(cookie),
        impersonate=IMPERSONATE,
        timeout=20,
    )
    body = r.text
    _cf_check(body, "list")
    if r.status_code != 200:
        die(f"list failed (HTTP {r.status_code}): {body[:200]}")
    return r.json()


def http_send(
    cookie: str, cse_id: str, session_id_for_referer: str, message: str
) -> dict:
    import uuid as _u

    ev_uuid = str(_u.uuid4())
    payload = {
        "events": [
            {
                "payload": {
                    "type": "user",
                    "uuid": ev_uuid,
                    "session_id": session_id_for_referer,
                    "parent_tool_use_id": None,
                    "message": {"role": "user", "content": message},
                }
            }
        ]
    }
    r = cffi_requests.post(
        f"{BASE}/{cse_id}/events",
        headers={**base_headers(cookie), "content-type": "application/json"},
        impersonate=IMPERSONATE,
        timeout=20,
        json=payload,
    )
    body = r.text
    _cf_check(body, "send")
    if r.status_code != 200:
        die(f"send failed (HTTP {r.status_code}): {body[:200]}")
    out = {"uuid": ev_uuid}
    try:
        out["eventResponse"] = r.json()
    except Exception:
        out["eventResponse"] = body[:200]
    return out


def http_fetch_events(
    cookie: str, session_id: str, limit: int = 50, after_uuid: str | None = None
) -> list[dict]:
    """GET /v1/sessions/<id>/events -> full message history (incl. assistant text).

    Paginates via next_cursor. If after_uuid given, returns only events newer than
    the event with that uuid (exclusive). Used to fetch the reply to a specific send."""
    out: list[dict] = []
    cursor = None
    target_idx = None
    while True:
        q = f"?limit={limit}"
        if cursor:
            q += f"&cursor={urllib.parse.quote(cursor)}"
        r = cffi_requests.get(
            f"https://claude.ai/v1/sessions/{session_id}/events{q}",
            headers=base_headers(cookie),
            impersonate=IMPERSONATE,
            timeout=20,
        )
        body = r.text
        _cf_check(body, "events-read")
        if r.status_code != 200:
            die(f"events-read failed (HTTP {r.status_code}): {body[:200]}")
        d = r.json()
        data = d.get("data") or []
        if not data:
            break
        if after_uuid is not None:
            # stop pagination once we've seen the anchor uuid (it's oldest we care about)
            for i, e in enumerate(data):
                if e.get("uuid") == after_uuid:
                    target_idx = i
                    break
        out.extend(data)
        cursor = d.get("next_cursor")
        if not cursor:
            break
        if after_uuid is not None and target_idx is not None:
            break  # we have everything from the anchor onward
    if after_uuid is not None:
        # slice to events strictly after the anchor
        start = None
        for i, e in enumerate(out):
            if e.get("uuid") == after_uuid:
                start = i + 1
                break
        return out[start:] if start is not None else out
    return out


def extract_text(event: dict) -> tuple[str, str]:
    """Return (role, text) from a message event."""
    msg = event.get("message", {})
    role = msg.get("role", event.get("type", ""))
    content = msg.get("content")
    if isinstance(content, str):
        return role, content
    parts = []
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "text" and c.get("text", "").strip():
                parts.append(c["text"])
            elif t == "tool_use":
                parts.append(f"[tool_use: {c.get('name', '?')}]")
            elif t == "tool_result":
                rc = c.get("content", "")
                if isinstance(rc, list):
                    rc = " ".join(x.get("text", "") for x in rc if isinstance(x, dict))
                parts.append(f"[tool_result: {str(rc)[:80]}]")
    return role, "\n".join(parts)


def extract_assistant_text_record(rec: dict) -> str:
    """Return assistant text from one local transcript JSONL record."""
    if rec.get("type") != "assistant":
        return ""
    msg = rec.get("message") or {}
    if msg.get("role") not in (None, "assistant"):
        return ""
    content = msg.get("content")
    parts = []
    if isinstance(content, str) and content.strip():
        parts.append(content)
    elif isinstance(content, list):
        for c in content:
            if (
                isinstance(c, dict)
                and c.get("type") == "text"
                and c.get("text", "").strip()
            ):
                parts.append(c["text"])
    return "\n".join(parts).strip()


def transcript_assistant_reply(
    transcript: str,
    after_uuid: str | None = None,
    timeout: float = 0.0,
) -> dict | None:
    """Find assistant text in a local transcript, optionally after a user turn."""
    path = Path(transcript)
    deadline = time.time() + timeout
    while True:
        try:
            lines = path.read_text("utf8", errors="replace").splitlines()
        except Exception:
            lines = []

        start = 0
        if after_uuid:
            start = -1
            compact = f'"uuid":"{after_uuid}"'
            spaced = f'"uuid": "{after_uuid}"'
            for i, line in enumerate(lines):
                if compact in line or spaced in line:
                    start = i + 1
                    break

        if start >= 0:
            latest = None
            for line in lines[start:]:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                text = extract_assistant_text_record(rec)
                if text:
                    latest = {
                        "uuid": rec.get("uuid"),
                        "text": text,
                        "source": "transcript",
                    }
            if latest:
                return latest

        if time.time() >= deadline:
            return None
        time.sleep(0.4)


def http_watch(cookie: str, resume_token: str, on_event, idle_timeout_s: float = 120.0):
    """Open the SSE watch stream. Calls on_event(event, data) for each event.
    Streams until idle_timeout_s passes with no new event."""
    url = f"{BASE}/watch?exclude_tags=-&resume_token={urllib.parse.quote(resume_token)}"
    resp = cffi_requests.get(
        url,
        headers=base_headers(cookie, accept="text/event-stream"),
        impersonate=IMPERSONATE,
        stream=True,
        # (connect, total). curl_cffi TIMEOUT is a hard total cap; for a live
        # SSE stream use a large total and let idle_timeout_s govern death.
        timeout=(10, max(idle_timeout_s, 600.0)),
    )
    if resp.status_code != 200:
        die(f"watch failed (HTTP {resp.status_code}): {resp.text[:200]}")
    buf = ""
    last_event = time.time()
    try:
        for raw in resp.iter_content():
            if raw:
                buf += raw.decode("utf-8", "replace")
                last_event = time.time()
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                ev = parse_sse_frame(frame)
                if ev:
                    stop = on_event(ev)
                    if stop:
                        return
            if time.time() - last_event > idle_timeout_s:
                return
    finally:
        try:
            resp.close()
        except Exception:
            pass


def parse_sse_frame(frame: str) -> dict | None:
    event, data, ev_id = "message", "", None
    for line in frame.split("\n"):
        if line.startswith(":") or not line.strip():
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data += line[5:].strip()
        elif line.startswith("id:"):
            ev_id = line[3:].strip()
    if not data and event in ("keepalive", "message"):
        return {"event": event, "data": None, "id": ev_id}
    out = {"event": event, "id": ev_id, "raw": data}
    try:
        out["data"] = json.loads(data) if data else None
    except Exception:
        out["data"] = data
    return out


# ---- commands --------------------------------------------------------------


def cmd_list(args):
    sessions = load_registry()
    if args.json:
        print(json.dumps(sessions, indent=2))
        return
    rows = [["PID", "STATUS", "KIND", "RC_SESSION", "LOCAL_SESSION", "CWD", "NAME"]]
    for s in sorted(sessions, key=lambda x: int(x.get("pid") or 0)):
        rows.append(
            [
                str(s.get("pid", "")),
                s.get("status", ""),
                s.get("kind", ""),
                s.get("bridgeSessionId", ""),
                s.get("sessionId", ""),
                s.get("cwd", ""),
                (s.get("name") or "")[:50],
            ]
        )
    print("\n".join("\t".join(r) for r in rows))


def cmd_resolve(args):
    s = resolve_target(args.target)
    cse = to_cse(s["bridgeSessionId"])
    cap = target_capability(s)
    out = {
        "rcUrl": f"https://claude.ai/code/{s['bridgeSessionId']}",
        "cseId": cse,
        "localSession": s.get("sessionId"),
        "pid": s.get("pid"),
        "cwd": s.get("cwd"),
        **cap,
    }
    print(json.dumps(out, indent=2) if args.json else out["rcUrl"])


def _send_and_maybe_watch(args, watch: bool):
    s = resolve_target(args.target)
    cse = to_cse(s["bridgeSessionId"])
    cookie = load_cookie_header()
    cap = target_capability(s)
    transcript = cap["transcript"]
    if args.dry_run:
        out = {
            "resolved": {
                "rcUrl": f"https://claude.ai/code/{s['bridgeSessionId']}",
                "cseId": cse,
                "localSession": s.get("sessionId"),
                "pid": s.get("pid"),
                "cwd": s.get("cwd"),
                **cap,
            },
            "cookieLoaded": True,
            "wouldSend": args.message[:80],
        }
        print(json.dumps(out, indent=2))
        return

    sent = http_send(cookie, cse, s["bridgeSessionId"], args.message)

    # --stream: tail the local transcript live (local sessions only).
    if getattr(args, "stream", False):
        if not cap["hasLocalTranscript"]:
            print(
                "--stream needs a local transcript; falling back to RC watch",
                file=sys.stderr,
                flush=True,
            )
            watch = True
        else:
            print(f"sent (uuid {sent['uuid']}); streaming transcript:\n", flush=True)
            res = stream_transcript(
                transcript, sent["uuid"], args.timeout, json_out=args.json
            )
            if not res["completed"]:
                print(
                    f"\n[stream ended without end_turn within {args.timeout}s]",
                    flush=True,
                )
                sys.exit(1)
            return

    if not watch and not args.wait_ack and not args.json:
        print(
            f"sent via RC events API: https://claude.ai/code/{s['bridgeSessionId']} (uuid {sent['uuid']})"
        )
        return

    # ack verification: prefer transcript (local), else watch stream.
    ack = None
    if args.wait_ack and transcript and Path(transcript).exists():
        ack = wait_ack_transcript(transcript, sent["uuid"], args.wait_ack, args.timeout)
        via = "transcript"
    elif watch or args.wait_ack:
        ack = wait_ack_watch(cookie, sent["uuid"], args.wait_ack, args.timeout)
        via = "watch"
    else:
        # default: quick transcript confirm if available, else watch
        if transcript and Path(transcript).exists():
            ack = wait_ack_transcript(
                transcript, sent["uuid"], None, min(args.timeout, 8)
            )
            via = "transcript"
        else:
            ack = wait_ack_watch(cookie, sent["uuid"], None, min(args.timeout, 20))
            via = "watch"

    ack_ok = bool(ack and ack.get("acked"))
    blocking_ack_required = bool(watch or args.wait_ack)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": (ack_ok or not blocking_ack_required),
                    "sentUuid": sent["uuid"],
                    "rcUrl": f"https://claude.ai/code/{s['bridgeSessionId']}",
                    "cseId": cse,
                    "localSession": s.get("sessionId"),
                    **cap,
                    "eventResponse": sent.get("eventResponse"),
                    "ackVia": via,
                    "ack": ack,
                },
                indent=2,
            )
        )
    else:
        extra = ""
        if ack and ack.get("summary"):
            extra = f" | summary: {ack['summary']}"
        elif ack and ack.get("ackMatched"):
            extra = " | ack matched"
        print(f"sent + acked via {via} (uuid {sent['uuid']}){extra}")

    if blocking_ack_required and not ack_ok:
        die(
            f"timed out after {args.timeout:g}s waiting for ack via {via} "
            f"(uuid {sent['uuid']})",
            code=1,
        )

    # --reply: after ack, fetch the assistant reply text for this turn.
    if getattr(args, "reply", False):
        if transcript and Path(transcript).exists():
            reply = transcript_assistant_reply(
                transcript, after_uuid=sent["uuid"], timeout=args.timeout
            )
            if reply:
                if args.json:
                    print(json.dumps({"reply": reply}, ensure_ascii=False, indent=2))
                else:
                    print("\n=== reply ===")
                    print(reply["text"])
                return
            die(
                f"timed out after {args.timeout:g}s waiting for transcript reply "
                f"(uuid {sent['uuid']})",
                code=1,
            )
        try:
            tail = http_fetch_events(
                cookie, s["bridgeSessionId"], limit=80, after_uuid=sent["uuid"]
            )
            printed = False
            for e in reversed(tail):
                role, text = extract_text(e)
                if role == "assistant" and text and not text.startswith("["):
                    if args.json:
                        print(
                            json.dumps(
                                {
                                    "reply": {
                                        "uuid": e.get("uuid"),
                                        "text": text,
                                        "source": "api",
                                    }
                                },
                                ensure_ascii=False,
                                indent=2,
                            )
                        )
                    else:
                        print("\n=== reply ===")
                        print(text)
                    printed = True
                    break
            if not printed:
                die("no assistant text reply found in RC events API", code=1)
        except Exception as e:
            die(f"reply fetch failed: {e}", code=1)


def wait_ack_transcript(
    transcript: str, uuid: str, ack_regex: str | None, timeout: float
) -> dict:
    re_ack = re.compile(ack_regex) if ack_regex else None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            txt = Path(transcript).read_text("utf8", errors="replace")
        except Exception:
            time.sleep(0.4)
            continue
        lines = txt.splitlines()
        anchor_idx = None
        if uuid:
            compact = f'"uuid":"{uuid}"'
            spaced = f'"uuid": "{uuid}"'
            for i, line in enumerate(lines):
                if compact in line or spaced in line:
                    anchor_idx = i
                    break
        uuid_seen = anchor_idx is not None
        ack_line = None
        if uuid_seen and re_ack:
            for line in lines[anchor_idx + 1 :]:
                is_assistant = False
                try:
                    is_assistant = json.loads(line).get("type") == "assistant"
                except Exception:
                    is_assistant = (
                        '"type":"assistant"' in line or '"type": "assistant"' in line
                    )
                if is_assistant and re_ack.search(line):
                    ack_line = line[:200]
                    break
        if uuid_seen and (not re_ack or ack_line):
            return {"acked": True, "uuidConfirmed": True, "ackMatched": bool(ack_line)}
        time.sleep(0.5)
    return {"acked": False, "reason": "timeout"}


def stream_transcript(
    transcript: str, uuid: str | None, timeout: float, json_out: bool = False
) -> dict:
    """Tail the local transcript and emit live activity after our user turn.

    Prints tool calls, tool results, and the final assistant text as they append.
    Returns a summary dict. Stops at end_turn (or timeout)."""
    deadline = time.time() + timeout
    path = Path(transcript)
    # start from end of file; wait for our uuid turn to appear, then stream.
    pos = path.stat().st_size if path.exists() else 0
    started = False  # have we seen our user turn?
    final_text = []
    saw_end = False

    def emit(obj):
        if json_out:
            print(json.dumps(obj, ensure_ascii=False))
        else:
            kind = obj.get("kind")
            if kind == "tool_use":
                name = obj.get("name", "?")
                inp = obj.get("input_summary", "")
                print(f"  ↳ tool: {name}({inp})", flush=True)
            elif kind == "tool_result":
                st = "err" if obj.get("is_error") else "ok"
                print(
                    f"    ↳ result [{st}]: {obj.get('summary', '')[:120]}", flush=True
                )
            elif kind == "text":
                print(obj.get("text", ""), flush=True)
            elif kind == "status":
                print(f"  · {obj.get('msg', '')}", flush=True)

    emit({"kind": "status", "msg": f"tailing {path.name}…"})
    # if no uuid, start in 'started' mode (pure tail from current pos).
    started = uuid is None
    while time.time() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            time.sleep(0.4)
            continue
        if size < pos:
            pos = 0  # truncated
        if size > pos:
            with path.open("rb") as f:
                f.seek(pos)
                chunk = f.read(size - pos).decode("utf-8", "replace")
                pos = size
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                t = rec.get("type")
                if not started:
                    if uuid and rec.get("uuid") == uuid and t == "user":
                        started = True
                        emit({"kind": "status", "msg": "turn queued"})
                    continue
                # after our turn: emit meaningful entries
                if t == "assistant":
                    msg = rec.get("message", {})
                    for c in msg.get("content", []):
                        if c.get("type") == "text" and c.get("text", "").strip():
                            final_text.append(c["text"])
                            emit({"kind": "text", "text": c["text"]})
                        elif c.get("type") == "tool_use":
                            inp = c.get("input", {})
                            summary = _tool_input_summary(inp)
                            emit(
                                {
                                    "kind": "tool_use",
                                    "name": c.get("name", "?"),
                                    "input_summary": summary,
                                }
                            )
                    if msg.get("stop_reason") == "end_turn":
                        saw_end = True
                elif t == "user":
                    msg = rec.get("message", {})
                    content = msg.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if c.get("type") == "tool_result":
                                is_err = c.get("is_error")
                                ctext = c.get("content", "")
                                if isinstance(ctext, list):
                                    ctext = " ".join(
                                        x.get("text", "")
                                        for x in ctext
                                        if isinstance(x, dict)
                                    )
                                emit(
                                    {
                                        "kind": "tool_result",
                                        "is_error": bool(is_err),
                                        "summary": str(ctext)[:200],
                                    }
                                )
            if saw_end:
                break
        time.sleep(0.3)
    return {
        "acked": saw_end or bool(final_text),
        "completed": saw_end,
        "text": "\n".join(final_text),
    }


def _tool_input_summary(inp) -> str:
    if not isinstance(inp, dict) or not inp:
        return ""
    # pick the most informative scalar field
    for k in (
        "command",
        "path",
        "file_path",
        "pattern",
        "query",
        "url",
        "prompt",
        "description",
    ):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v[:80]
    # fallback: first short value
    for k, v in inp.items():
        if isinstance(v, str) and len(v) < 80:
            return f"{k}={v}"
    return ""


def wait_ack_watch(
    cookie: str, uuid: str, ack_regex: str | None, timeout: float
) -> dict:
    """Watch the session for worker_status idle + optional status_detail match."""
    listing = http_list_sessions(cookie)
    token = listing["resume_token"]
    re_ack = re.compile(ack_regex) if ack_regex else None
    result = {"acked": False}
    deadline = [time.time() + timeout]
    saw_running = [False]

    def on_event(ev):
        if time.time() > deadline[0]:
            return True  # stop
        data = ev.get("data") or {}
        if not isinstance(data, dict):
            return False
        ws = data.get("worker_status")
        ext = (data.get("external_metadata") or {}).get("post_turn_summary") or {}
        summary = ext.get("status_detail", "")
        if ws == "running":
            saw_running[0] = True
        if ws == "idle" and saw_running[0]:
            matched = bool(re_ack and summary and re_ack.search(summary))
            result.update(
                {
                    "acked": True,
                    "workerIdle": True,
                    "summary": summary,
                    "ackMatched": matched or (not re_ack),
                }
            )
            return True
        return False

    try:
        http_watch(cookie, token, on_event, idle_timeout_s=timeout)
    except Exception as e:
        result["watchError"] = str(e)
    return result


def cmd_watch(args):
    cookie = load_cookie_header()
    token = http_list_sessions(cookie)["resume_token"]
    target_cse = None
    if args.target:
        s = resolve_target(args.target)
        target_cse = to_cse(s["bridgeSessionId"])

    def on_event(ev):
        data = ev.get("data") or {}
        if not isinstance(data, dict) and ev["event"] != "keepalive":
            return False
        # filter to target session if given
        if (
            target_cse
            and isinstance(data, dict)
            and data.get("id")
            and data["id"] != target_cse
        ):
            return False
        if ev["event"] == "keepalive":
            if not args.json:
                print(".", end="", flush=True)
            return False
        if args.json:
            print(json.dumps(ev))
        else:
            ws = data.get("worker_status", "") if isinstance(data, dict) else ""
            summ = ""
            if isinstance(data, dict):
                summ = (
                    (data.get("external_metadata") or {}).get("post_turn_summary") or {}
                ).get("status_detail", "")
            print(f"[{ev['event']}] worker={ws} {('— ' + summ) if summ else ''}")
        sys.stdout.flush()
        return False

    print("watching. Ctrl-C to stop.", file=sys.stderr)
    try:
        http_watch(cookie, token, on_event, idle_timeout_s=1e9)
    except KeyboardInterrupt:
        pass


def cmd_stream(args):
    """Tail a session's local transcript and print live activity (no send)."""
    s = resolve_target(args.target)
    if s.get("_url"):
        die("stream needs a local session; target resolved to a URL only")
    transcript = transcript_path(s)
    if not transcript or not Path(transcript).exists():
        die(f"no local transcript for target: {transcript}")
    print(f"tailing {transcript} (uuid=any recent). Ctrl-C to stop.", file=sys.stderr)
    res = stream_transcript(transcript, None, args.timeout, json_out=args.json)
    if not res["completed"]:
        print(f"\n[stream ended within {args.timeout}s]", flush=True)


def cmd_doctor(args):
    """Preflight: cookie freshness, CF status, daemon reachability."""

    report = {"checks": []}

    def check(name, ok, detail=""):
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        if args.json:
            return
        flag = "ok" if ok else "FAIL"
        line = f"  [{flag}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)

    if not args.json:
        print("claude-rc health check")
    # 1. cookie read
    try:
        cookie = load_cookie_header()
        check("cookie read", True, "sessionKey present")
    except SystemExit:
        check("cookie read", False, "no sessionKey cookie in Chrome profile")
        print(json.dumps(report, indent=2)) if args.json else None
        if args.json:
            return
        sys.exit(1)

    # 2. cookie expiry (days left on sessionKey / cf_clearance)
    try:
        raw = subprocess.run(
            ["node", str(COOKIE_BRIDGE), "raw", PROFILE],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        now = time.time()
        exps = {}
        for line in raw.stdout.splitlines():
            try:
                d = json.loads(line)
                exps[d["name"]] = d
            except Exception:
                continue
        sk = exps.get("sessionKey")
        cf = exps.get("cf_clearance")
        bits = []
        if sk:
            days = ((sk.get("expires") or 0) - now) / 86400
            bits.append(f"sessionKey {days:.0f}d")
        if cf:
            days = ((cf.get("expires") or 0) - now) / 86400
            bits.append(f"cf_clearance {days:.0f}d")
        check("cookie expiry", True, ", ".join(bits) if bits else "unknown")
    except Exception as e:
        check("cookie expiry", True, f"skip ({repr(e)[:60]})")

    # 3. CF / list reachability (the real health signal)
    try:
        listing = http_list_sessions(cookie)
        n = len(listing.get("data", []))
        check("cloudflare / list", True, f"{n} active sessions reachable")
    except CfChallenge as e:
        check("cloudflare / list", False, str(e))
    except Exception as e:
        check("cloudflare / list", False, repr(e)[:120])

    # 4. daemon reachability (informational; offline bg-job fallback)
    try:
        sock = next(Path("/tmp").glob("cc-daemon-*/*/control.sock"), None)
        if sock:
            check(
                "daemon socket",
                True,
                f"{sock.parent.parent.name}/{sock.parent.name} (bg-job reply available)",
            )
        else:
            check(
                "daemon socket",
                True,
                "not running (fine unless you use backgrounded sessions)",
            )
    except Exception as e:
        check("daemon socket", True, f"skip ({repr(e)[:60]})")

    # daemon is informational; overall health = cookie + CF only.
    critical = [
        c for c in report["checks"] if c["name"] in ("cookie read", "cloudflare / list")
    ]
    ok = all(c["ok"] for c in critical)
    if args.json:
        print(json.dumps({"ok": ok, **report}, indent=2))
    sys.exit(0 if ok else 1)


def cmd_last_reply(args):
    """Fetch the most recent assistant reply (full text) for a session."""
    s = resolve_target(args.target)
    sid = s["bridgeSessionId"]

    transcript = transcript_path(s) if not s.get("_url") else None
    if args.source in ("auto", "local") and transcript and Path(transcript).exists():
        reply = transcript_assistant_reply(transcript)
        if reply:
            if args.json:
                print(json.dumps(reply, ensure_ascii=False, indent=2))
            else:
                print(reply["text"])
            return
        if args.source == "local":
            die("no assistant text reply found in local transcript")
    elif args.source == "local":
        die(f"no local transcript for target: {transcript}")

    cookie = load_cookie_header()
    events = http_fetch_events(cookie, sid, limit=args.limit)
    # walk backward for the last assistant text event
    for e in reversed(events):
        role, text = extract_text(e)
        if role == "assistant" and text and not text.startswith("["):
            if args.json:
                print(
                    json.dumps(
                        {"uuid": e.get("uuid"), "text": text, "source": "api"},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(text)
            return
    die("no assistant text reply found in the last " + str(args.limit) + " events")


# ---- entry -----------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(prog="claude-rc", description="Headless RC client")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="JSON output")

    sub.add_parser("list", parents=[common], help="list RC sessions").set_defaults(
        func=cmd_list
    )
    rp = sub.add_parser("resolve", parents=[common], help="resolve a target")
    rp.add_argument("target")
    rp.set_defaults(func=cmd_resolve)

    sp = sub.add_parser("send", parents=[common], help="send a message")
    sp.add_argument("target")
    sp.add_argument("message")
    sp.add_argument("--wait-ack", default=None, nargs="?", const=".*")
    sp.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_SEND_TIMEOUT,
        help=f"seconds to wait for ack/reply/stream work (default: {DEFAULT_SEND_TIMEOUT:g}; env: CLAUDE_RC_TIMEOUT or CLAUDE_RC_SEND_TIMEOUT)",
    )
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument(
        "--stream",
        action="store_true",
        help="tail the local transcript: print tool calls + assistant text live",
    )
    sp.add_argument(
        "--reply",
        action="store_true",
        help="after ack, fetch + print the assistant reply text (any session)",
    )
    sp.set_defaults(func=lambda a: _send_and_maybe_watch(a, watch=False))

    sw = sub.add_parser(
        "send+watch", parents=[common], help="send then stream until turn completes"
    )
    sw.add_argument("target")
    sw.add_argument("message")
    sw.add_argument("--wait-ack", default=None, nargs="?", const=".*")
    sw.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_WATCH_TIMEOUT,
        help=f"seconds to watch for completion (default: {DEFAULT_WATCH_TIMEOUT:g}; env: CLAUDE_RC_TIMEOUT or CLAUDE_RC_WATCH_TIMEOUT)",
    )
    sw.add_argument("--dry-run", action="store_true")
    sw.set_defaults(func=lambda a: _send_and_maybe_watch(a, watch=True))

    wp = sub.add_parser("watch", parents=[common], help="stream session state changes")
    wp.add_argument("target", nargs="?", default=None)
    wp.set_defaults(func=cmd_watch)

    stp = sub.add_parser(
        "stream",
        parents=[common],
        help="tail a session's local transcript live (no send)",
    )
    stp.add_argument("target")
    stp.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_STREAM_TIMEOUT,
        help=f"seconds to tail the local transcript (default: {DEFAULT_STREAM_TIMEOUT:g}; env: CLAUDE_RC_STREAM_TIMEOUT)",
    )
    stp.set_defaults(func=cmd_stream)

    sub.add_parser(
        "doctor", parents=[common], help="preflight health check"
    ).set_defaults(func=cmd_doctor)

    lr = sub.add_parser(
        "last-reply",
        parents=[common],
        help="fetch the last assistant reply (full text, any session)",
    )
    lr.add_argument("target")
    lr.add_argument(
        "--limit", type=int, default=50, help="how many recent events to scan"
    )
    lr.add_argument(
        "--source",
        choices=("auto", "local", "api"),
        default="auto",
        help="reply source: local transcript when available, otherwise API",
    )
    lr.set_defaults(func=cmd_last_reply)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
