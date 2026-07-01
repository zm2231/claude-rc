#!/usr/bin/env node
// Headless Claude Code Remote-Control message sender.
//
// Sends a user message into a live RC-enabled Claude Code session by POSTing
// to the official /v1/code/sessions/cse_<id>/events route, authenticated with
// the claude.ai web session cookies (read via @steipete/sweet-cookie from the
// Chrome profile). No browser, no surf, no DOM clicking, no tmux.
//
// Cookies are read from the browser profile on every call (never persisted).
// Only the standard web request headers + the documented anthropic-* client
// headers are sent. No Authorization token or OAuth flow is used.

import { getCookies, toCookieHeader } from "@steipete/sweet-cookie";
import { randomUUID } from "node:crypto";
import { readFile, readdir } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";

const EVENTS_URL = (cseId) => `https://claude.ai/v1/code/sessions/${cseId}/events`;
const BROWSER_HEADERS = {
  "user-agent":
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
  accept: "*/*",
  "accept-language": "en-US,en;q=0.9",
  origin: "https://claude.ai",
  "sec-ch-ua":
    '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
  "sec-ch-ua-mobile": "?0",
  "sec-ch-ua-platform": '"macOS"',
  "sec-fetch-dest": "empty",
  "sec-fetch-mode": "cors",
  "sec-fetch-site": "same-origin",
};
const ANTHROPIC_HEADERS = {
  "anthropic-version": "2023-06-01",
  "anthropic-beta": "ccr-byoc-2025-07-29",
  "anthropic-client-platform": "web_claude_ai",
  "anthropic-client-feature": "ccr",
  "anthropic-client-version": "1.0.0",
};

const SESSIONS_DIR =
  process.env.CLAUDE_SESSIONS_DIR || join(homedir(), ".claude", "sessions");

function die(msg, code = 1) {
  process.stderr.write(`error: ${msg}\n`);
  process.exit(code);
}

function usage() {
  process.stdout.write(`Usage:
  claude-rc-send <target> <message> [options]
  claude-rc-send --resolve <target>
  claude-rc-send --list

Target (one required unless --list):
  PID, local session id/prefix, RC session_* id, RC URL,
  or a case-insensitive fragment of cwd/name.

Options:
  --json              Emit JSON result (resolved target, uuid, status, transcript)
  --dry-run           Resolve target + load cookies, do not send
  --resolve <target>  Print resolved RC URL + local session, exit 0
  --list              List RC-enabled sessions (like claude-rc-list, JSON with --json)
  --wait-ack <regex>  After send, tail the local transcript until a user turn with
                      our uuid appears (and, if given, an assistant line matching <regex>)
  --timeout <sec>     Send + wait-ack timeout (default 30)
  --profile <name>    Chrome profile (default "Default")
  --help, -h          Show this help

Examples:
  claude-rc-send 33730 "hello by pid"
  claude-rc-send <local-session-prefix> "hello by local session prefix"
  claude-rc-send session_016u... "hello by RC id"
  claude-rc-send repo-name "hello by name fragment"
  claude-rc-send 33730 "do X" --wait-ack 'DONE_[0-9]+' --json
`);
}

function parseArgs(argv) {
  const opts = {
    json: false,
    dryRun: false,
    resolveOnly: false,
    list: false,
    waitAck: null,
    timeout: 30,
    profile: process.env.CLAUDE_COOKIE_PROFILE || "Default",
    target: null,
    message: null,
  };
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    switch (a) {
      case "-h":
      case "--help":
        usage();
        process.exit(0);
      case "--json":
        opts.json = true;
        break;
      case "--dry-run":
        opts.dryRun = true;
        break;
      case "--list":
        opts.list = true;
        break;
      case "--resolve":
        opts.resolveOnly = true;
        opts.target = argv[++i];
        break;
      case "--wait-ack":
        opts.waitAck = argv[++i];
        break;
      case "--timeout":
        opts.timeout = Number(argv[++i]);
        break;
      case "--profile":
        opts.profile = argv[++i];
        break;
      default:
        positional.push(a);
    }
  }
  if (!opts.resolveOnly && !opts.list) {
    if (positional.length < 2) {
      usage();
      process.exit(0);
    }
    opts.target = positional[0];
    opts.message = positional.slice(1).join(" ");
  }
  return opts;
}

// ---- session registry resolution --------------------------------------------

async function loadSessions() {
  let files;
  try {
    files = await readdir(SESSIONS_DIR);
  } catch {
    die(`no Claude sessions dir found: ${SESSIONS_DIR}`);
  }
  const out = [];
  for (const f of files) {
    if (!f.endsWith(".json")) continue;
    try {
      const raw = JSON.parse(await readFile(join(SESSIONS_DIR, f), "utf8"));
      if (raw.bridgeSessionId) out.push(raw);
    } catch {}
  }
  return out;
}

function projectDirFromCwd(cwd) {
  if (!cwd) return null;
  // Claude encodes project cwd paths by replacing every '/' with '-',
  // including the leading one).
  return join(homedir(), ".claude", "projects", cwd.replace(/\//g, "-"));
}

function transcriptPath(session) {
  const dir = projectDirFromCwd(session.cwd);
  return dir ? join(dir, `${session.sessionId}.jsonl`) : null;
}

function toCse(bridgeId) {
  const bare = (bridgeId || "")
    .replace(/^https:\/\/claude\.ai\/code\//, "")
    .replace(/^session_/, "")
    .replace(/^cse_/, "");
  return `cse_${bare}`;
}

// Resolve a target spec to a single session record. Throws on 0/N matches.
async function resolveTarget(raw) {
  // Direct RC URL / session_ / cse_ forms (no registry needed).
  const urlMatch = raw.match(/^https:\/\/claude\.ai\/code\/(session_[A-Za-z0-9]+)/);
  if (urlMatch) {
    return { bridgeSessionId: urlMatch[1], sessionId: null, cwd: null, _viaUrl: true };
  }
  if (/^cse_/.test(raw) || /^session_/.test(raw)) {
    const sessions = await loadSessions();
    const hit = sessions.find((s) => s.bridgeSessionId === raw || toCse(s.bridgeSessionId) === raw);
    if (hit) return hit;
    return { bridgeSessionId: raw.replace(/^cse_/, "session_"), sessionId: null, cwd: null, _viaUrl: true };
  }

  const sessions = await loadSessions();
  const q = raw.toLowerCase();
  const matches = sessions.filter((s) => {
    return (
      String(s.pid) === raw ||
      (s.sessionId || "").startsWith(raw) ||
      (s.bridgeSessionId || "").startsWith(raw) ||
      (s.cwd || "").toLowerCase().includes(q) ||
      (s.name || "").toLowerCase().includes(q)
    );
  });
  if (matches.length === 0) {
    die(`no RC-enabled Claude session matched '${raw}'\nhint: run claude-rc-send --list`);
  }
  if (matches.length > 1) {
    const rows = matches
      .map((m) => `  pid=${m.pid} local=${m.sessionId} rc=${m.bridgeSessionId} cwd=${m.cwd || ""} name=${(m.name || "").slice(0, 40)}`)
      .join("\n");
    die(`target '${raw}' matched ${matches.length} RC sessions:\n${rows}\nhint: use PID, local session prefix, or full session_* id`);
  }
  return matches[0];
}

// ---- cookie loading ----------------------------------------------------------

async function loadCookieHeader(profile) {
  const r = await getCookies({ url: "https://claude.ai/", profile });
  const hasSession = r.cookies.some((c) => c.name === "sessionKey");
  if (!hasSession) {
    die(
      `no claude.ai sessionKey cookie in Chrome profile '${profile}'. ` +
        `Is Chrome logged in? (warnings: ${JSON.stringify(r.warnings)})`
    );
  }
  return toCookieHeader(r.cookies);
}

// ---- send --------------------------------------------------------------------

async function send({ cseId, sessionId, message, cookie, refererSid }) {
  const uuid = randomUUID();
  const body = {
    events: [
      {
        payload: {
          type: "user",
          uuid,
          session_id: refererSid || `session_${cseId.replace(/^cse_/, "")}`,
          parent_tool_use_id: null,
          message: { role: "user", content: message },
        },
      },
    ],
  };
  const headers = {
    "content-type": "application/json",
    cookie,
    referer: `https://claude.ai/code/${refererSid || `session_${cseId.replace(/^cse_/, "")}`}`,
    ...BROWSER_HEADERS,
    ...ANTHROPIC_HEADERS,
  };
  const resp = await fetch(EVENTS_URL(cseId), {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  let respBody = "";
  try {
    respBody = await resp.text();
  } catch {}
  return { ok: resp.ok, status: resp.status, uuid, respBody: respBody.slice(0, 400) };
}

// ---- ACK verification (transcript tail) --------------------------------------

async function waitForAck({ transcript, uuid, ackRegex, timeoutSec }) {
  if (!transcript) return { acked: false, reason: "no transcript path" };
  const deadline = Date.now() + timeoutSec * 1000;
  const ackRe = ackRegex ? new RegExp(ackRegex) : null;
  let lastSize = 0;
  while (Date.now() < deadline) {
    let txt = "";
    try {
      txt = await readFile(transcript, "utf8");
    } catch {
      await sleep(400);
      continue;
    }
    // Look for our uuid as a user-turn uuid, then optionally an assistant line matching ackRe.
    const uuidSeen = txt.includes(`"uuid":"${uuid}"`);
    let ackLine = null;
    if (uuidSeen && ackRe) {
      for (const line of txt.slice(lastSize).split("\n").reverse()) {
        if (line.includes('"type":"assistant"') && ackRe.test(line)) {
          ackLine = line.slice(0, 200);
          break;
        }
      }
    }
    lastSize = txt.length;
    if (uuidSeen && (!ackRe || ackLine)) {
      return { acked: true, uuidConfirmed: true, ackMatched: !!ackLine, ackLine };
    }
    await sleep(500);
  }
  return { acked: false, reason: "timeout" };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- main --------------------------------------------------------------------

async function main() {
  const opts = parseArgs(process.argv.slice(2));

  if (opts.list) {
    const sessions = await loadSessions();
    if (opts.json) {
      process.stdout.write(JSON.stringify(sessions, null, 2) + "\n");
    } else {
      const rows = [
        ["PID", "STATUS", "KIND", "RC_SESSION", "LOCAL_SESSION", "CWD", "NAME"].join("\t"),
      ];
      for (const s of sessions
        .slice()
        .sort((a, b) => Number(a.pid) - Number(b.pid))) {
        rows.push(
          [s.pid, s.status || "", s.kind || "", s.bridgeSessionId, s.sessionId, s.cwd || "", (s.name || "").slice(0, 50)].join("\t")
        );
      }
      process.stdout.write(rows.join("\n") + "\n");
    }
    return;
  }

  const session = await resolveTarget(opts.target);
  const cseId = toCse(session.bridgeSessionId);
  const transcript = session.sessionId ? transcriptPath(session) : null;

  if (opts.resolveOnly) {
    const out = {
      rcUrl: `https://claude.ai/code/${session.bridgeSessionId}`,
      cseId,
      localSession: session.sessionId || null,
      pid: session.pid || null,
      cwd: session.cwd || null,
      transcript,
    };
    process.stdout.write((opts.json ? JSON.stringify(out, null, 2) : out.rcUrl) + "\n");
    return;
  }

  if (!opts.message) die("message is empty", 2);

  const cookie = await loadCookieHeader(opts.profile);

  if (opts.dryRun) {
    const out = {
      resolved: {
        rcUrl: `https://claude.ai/code/${session.bridgeSessionId}`,
        cseId,
        localSession: session.sessionId || null,
        pid: session.pid || null,
        cwd: session.cwd || null,
        transcript,
      },
      cookieLoaded: true,
      cookieNames: 0,
      wouldSend: opts.message.slice(0, 80),
    };
    process.stdout.write(JSON.stringify(out, null, 2) + "\n");
    return;
  }

  const sendDeadline = Date.now() + opts.timeout * 1000;
  let result;
  try {
    result = await Promise.race([
      send({ cseId, sessionId: session.sessionId, message: opts.message, cookie, refererSid: session.bridgeSessionId }),
      sleep(opts.timeout * 1000).then(() => null),
    ]);
  } catch (e) {
    die(`send failed: ${e.message}`);
  }
  if (!result) die(`send timed out after ${opts.timeout}s`);

  let ack = null;
  if (result.ok && opts.waitAck) {
    ack = await waitForAck({
      transcript,
      uuid: result.uuid,
      ackRegex: opts.waitAck === "any" ? null : opts.waitAck,
      timeoutSec: Math.max(1, Math.floor((sendDeadline - Date.now()) / 1000)),
    });
  } else if (result.ok && transcript && opts.json) {
    // Even without --wait-ack, cheaply confirm uuid landed (one quick read).
    ack = await waitForAck({ transcript, uuid: result.uuid, ackRegex: null, timeoutSec: 5 });
  }

  if (opts.json) {
    const out = {
      ok: result.ok,
      status: result.status,
      sentUuid: result.uuid,
      rcUrl: `https://claude.ai/code/${session.bridgeSessionId}`,
      cseId,
      localSession: session.sessionId || null,
      transcript,
      eventResponse: safeJson(result.respBody),
      ack: ack,
    };
    process.stdout.write(JSON.stringify(out, null, 2) + "\n");
  } else {
    if (!result.ok) {
      die(`send failed (HTTP ${result.status}): ${result.respBody.slice(0, 200)}`);
    }
    let line = `sent via RC events API: https://claude.ai/code/${session.bridgeSessionId} (uuid ${result.uuid})`;
    if (ack) line += ` ack=${ack.acked}${ack.ackMatched ? " (pattern matched)" : ""}`;
    process.stdout.write(line + "\n");
  }
  process.exit(result.ok ? 0 : 1);
}

function safeJson(s) {
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

main().catch((e) => die(e.message));
