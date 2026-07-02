# claude-rc

Claude Code already has Remote Control. You can open a link on your phone, reply from the browser, and keep a session moving without sitting in the original terminal.

That is useful for humans. It is also useful for agents.

In honor of Fable coming back, I wanted Codex to be able to do the boring handoff work itself. I did not want to burn Claude tokens on menial coordination, and I did not want to route everything through tmux just to type into another terminal. `claude-rc` is a small local bridge that lets one agent send a normal user message into an already-running Claude Code Remote Control session.

The default path is browser-free: it uses your existing Claude web session from Chrome, reads cookies through `@steipete/sweet-cookie`, and sends through Claude's Remote Control web endpoints with Chrome-style TLS impersonation.

No tmux. No terminal scraping. No OAuth replay.

## What It Does

`claude-rc` lets you list local RC-enabled Claude Code sessions, resolve a target by PID/session/name/URL, send a user message, wait for acknowledgement, and fetch the assistant's reply text.

The main use case is simple: Codex can tell Claude what to do without you manually copying prompts between agent windows.

## Requirements

Required:

- macOS
- Chrome logged in to `https://claude.ai`
- Claude Code sessions with Remote Control enabled by default
- `python3`
- `node` and `npm`

Before using this, open Claude Code, run `/config`, and set:

```text
Enable Remote Control for all sessions    true
```

Claude Code defaults this to `false`. `claude-rc` assumes sessions are RC-enabled, so this is the difference between "works everywhere" and "why is this one session invisible?"

Installed dependencies:

- Python: `curl_cffi`
- Node: `@steipete/sweet-cookie`

`@steipete/sweet-cookie` is the cookie extraction layer. `cookie.mjs` uses it to read Claude cookies from your Chrome profile and emit a `Cookie:` header for the Python client. `claude-rc` does not store cookies or write tokens to disk.

Optional:

- [`surf-cli`](https://github.com/nicobailon/surf-cli), only for `--via-surf`, the browser UI fallback for send/resolve. The primary backend does not require surf.

## Install

```bash
git clone https://github.com/zm2231/claude-rc.git
cd claude-rc
./install.sh
claude-rc-send doctor
```

The installer creates `.venv`, installs Python requirements, runs `npm install`, and symlinks these commands into `~/.local/bin`:

```bash
claude-rc-send
claude-rc-list
```

If `~/.local/bin` is not on `PATH`, the installer adds it to your shell rc file with a `claude-rc` marker.

## Quick Start

List active RC-enabled Claude Code sessions:

```bash
claude-rc-list
```

Resolve a target before sending:

```bash
claude-rc-send resolve <target> --json
```

Send a message:

```bash
claude-rc-send <target> "run the tests and tell me what failed"
```

Send, wait for acknowledgement, then fetch the response:

```bash
claude-rc-send <target> "what are you working on?" --wait-ack --reply
```

Run a health check:

```bash
claude-rc-send doctor --json
```

## Targets

Targets can be:

- process ID, for example `33730`
- local Claude session id or prefix
- RC session id, for example `session_...`
- RC URL, for example `https://claude.ai/code/session_...`
- a unique fragment of the session name or cwd

If a target is ambiguous, the command fails and prints the matching sessions.

`resolve --json` also reports routing metadata:

- `hasLocalTranscript`: whether this machine has the Claude JSONL transcript.
- `replySource`: `transcript` for local-first replies, otherwise `api`.
- `streamSource`: `transcript` for local streaming, otherwise `watch`.
- `remoteOnly`: true when this client cannot read a local transcript for the target.

## Commands

```bash
claude-rc-list [--json]
claude-rc-send list [--json]
claude-rc-send resolve <target> [--json]
claude-rc-send send <target> "message" [--wait-ack [regex]] [--reply] [--stream] [--json]
claude-rc-send send+watch <target> "message" [--wait-ack [regex]]
claude-rc-send last-reply <target> [--source auto|local|api] [--limit N] [--json]
claude-rc-send stream <target>
claude-rc-send watch [target]
claude-rc-send doctor [--json]
```

The short form also works:

```bash
claude-rc-send <target> "message"
claude-rc-send --resolve <target>
```

## Backends

The Python backend is the default and is the one you probably want.

```bash
claude-rc-send --via-python <target> "message"
```

It supports send, list, resolve, acknowledgement, watch, transcript streaming, `doctor`, and reply fetching.

The Node backend is kept as a legacy sender:

```bash
claude-rc-send --via-node <target> "message"
```

It supports list, resolve, and send. It is less reliable for Cloudflare-protected GET endpoints.

The surf backend is an optional browser UI fallback:

```bash
claude-rc-send --via-surf <target> "message"
```

It supports send and resolve only. It exists for the case where Cloudflare makes the direct Python path unhappy and you still want to drive the real browser UI.

## What Works

- List local RC-enabled Claude Code sessions
- Resolve a session by PID, local session id, RC id, RC URL, cwd, or name
- Send a user message into an already-running RC session
- Confirm the sent user turn by UUID through the local transcript when available
- Watch worker state through Remote Control SSE
- Fetch the latest assistant reply text
- Send and fetch the reply in one command with `--reply`
- Use `doctor` to catch expired cookies, account mismatch, and Cloudflare problems before a real send

## Boundaries

`claude-rc` talks to Claude Code Remote Control. It does not inject into arbitrary terminals, and it does not control sessions that are not RC-enabled.

`--stream` tails the local Claude Code transcript file when this machine has one. If no local transcript is available, it falls back to Remote Control watch instead of failing the send.

`last-reply` defaults to `--source auto`: local transcript first when available, then the RC events API. Use `--source api` to force the cloud events path, or `--source local` when you specifically want the local transcript and would rather fail than fall back.

`--timeout` is a ceiling, not a sleep. Blocking commands return as soon as the expected acknowledgement, reply, or stream completion arrives; if it does not arrive before the timeout, the command exits nonzero. The default send/watch timeout is 120 seconds. Override per command with `--timeout N`, or set `CLAUDE_RC_TIMEOUT`, `CLAUDE_RC_SEND_TIMEOUT`, `CLAUDE_RC_WATCH_TIMEOUT`, or `CLAUDE_RC_STREAM_TIMEOUT`.

Watch streams state and summaries, not token-by-token assistant text. For full response text, use `--reply` or `last-reply`.

Chrome account mismatches matter. If Claude Code created the session under one Claude account but Chrome is logged into another, the local registry can still show a session while the cloud API returns `404`. `doctor` verifies the current Chrome cookie, but the fix is to switch Chrome back to the account that owns the session.

## Security Model

`claude-rc` uses your existing Claude web login from Chrome. Cookies are read fresh for each command through `@steipete/sweet-cookie`; they are not cached by this project.

The repo does not include your cookies, transcripts, Claude config, `.venv`, or `node_modules`.

Do not publish logs that include real `sessionKey` or `cf_clearance` values.

## Project Layout

```text
claude-rc/
  rc.py                 Python client
  cookie.mjs            Chrome cookie bridge via @steipete/sweet-cookie
  send.mjs              Legacy Node sender
  requirements.txt      Python dependencies
  package.json          Node dependency for sweet-cookie
  install.sh            Installer and PATH setup
  bin/
    claude-rc-send
    claude-rc-list
    claude-rc-send-surf
```
