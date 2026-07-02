---
name: "claude-rc-operations"
description: "Use when the user wants Codex to communicate with Claude Code sessions through claude-rc, send a message to an already-running Claude agent, ask Claude for output, fetch Claude's reply, list or resolve RC-enabled Claude sessions, check Remote Control health, or coordinate work between Codex and Claude without tmux or manual copy-paste."
---

# Claude RC Operations

Use `claude-rc` when Codex should talk to an already-running Claude Code Remote Control session.

Do not say Codex cannot message Claude until you have checked the installed client:

```bash
command -v claude-rc-send
claude-rc-send doctor --json
```

`doctor` should show cookie read and Cloudflare/list reachability as `ok: true`. If it fails with a 404 or session-not-found after local resolution succeeds, Chrome is often logged into a different Claude account than the target session.

## Resolve First

List and resolve sessions before sending when the target is not exact:

```bash
claude-rc-list
claude-rc-list --json
claude-rc-send resolve <target> --json
```

Targets can be PID, local session id or prefix, `session_...`, RC URL, cwd fragment, or session-name fragment. If resolution is ambiguous, use the PID, local session id, or full RC session id.

Use `resolve --json` routing fields:

- `hasLocalTranscript: true`: transcript-backed `--reply`, `--stream`, and `last-reply --source local` are available.
- `replySource: transcript|api`: what auto reply mode will prefer.
- `streamSource: transcript|watch`: whether `--stream` will tail local transcript or fall back to RC watch.
- `remoteOnly: true`: this client cannot read a local transcript for the target.

Local sessions can wait on the JSONL transcript and return fresh full text. Remote-only/API sessions can send and watch RC state, but may not have fresh full text until cloud events catch up.

## Send Defaults

Default to blocking mode so Codex waits for Claude instead of sending and moving on:

```bash
claude-rc-send <target> "message" --wait-ack --reply
```

Use transcript streaming for local sessions when tool calls or live progress matter:

```bash
claude-rc-send <target> "message" --wait-ack --stream
```

Use plain send only when the user explicitly wants fire-and-forget:

```bash
claude-rc-send <target> "message" --wait-ack
```

For unclear or risky targets, dry-run first:

```bash
claude-rc-send <target> "message" --dry-run --json
```

## Reply And Timeout Behavior

`--timeout` is a ceiling, not a sleep. Commands return as soon as the expected acknowledgement, reply, or `end_turn` arrives. If the expected event does not arrive before the timeout, the command exits nonzero.

Use the right primitive:

- Need to send a message and wait for the answer: `claude-rc-send <target> "message" --wait-ack --reply --timeout 300`.
- Need to send a message and watch local tool activity live: `claude-rc-send <target> "message" --wait-ack --stream --timeout 300`.
- Message was already sent and the target is local: `claude-rc-send stream <target> --timeout 300`.
- Message was already sent and the target is remote-only: `claude-rc-send watch <target>` for state, then `claude-rc-send last-reply <target>` to fetch whatever text cloud events currently expose.

`last-reply` is not a wait primitive. It fetches the latest available assistant text immediately and does not take `--timeout`.

Defaults:

- send/watch timeout: 120 seconds
- transcript tail timeout: 600 seconds

Override with `--timeout N` or environment variables:

- `CLAUDE_RC_TIMEOUT`
- `CLAUDE_RC_SEND_TIMEOUT`
- `CLAUDE_RC_WATCH_TIMEOUT`
- `CLAUDE_RC_STREAM_TIMEOUT`

For long implementation/review tasks, use `--timeout 300` or `--timeout 600`.

Streaming shows transcript-visible events only: queued status, tool calls, tool results, assistant text, and end turn. It does not show hidden thinking tokens, so silence can be normal while Claude is thinking.

## Fetch Existing Replies

Use:

```bash
claude-rc-send last-reply <target>
```

`last-reply` defaults to `--source auto`: local transcript first when available, then RC events API. It returns immediately. If freshness matters for a local session, force:

```bash
claude-rc-send last-reply <target> --source local
```

Use `--source api` only to inspect cloud events behavior or for targets without local transcripts.

## Fallbacks And Failures

- `--stream` falls back to RC watch when no local transcript is available. Watch gives worker state and summaries, not full token text.
- `--reply` uses local transcript after the sent UUID when possible, then API fallback for no-transcript targets.
- Cloud events can be stale compared with local transcript; prefer transcript-backed paths for local sessions.
- Do not use tmux, AppleScript, keyboard typing, or terminal injection when `claude-rc` can address the session directly.
- Do not read or print raw cookies. `doctor` is safe; cookie values are not needed.

If Cloudflare blocks the Python backend, ask the user to open `https://claude.ai` in Chrome and let Cloudflare resolve. Use `--via-surf` only as an explicit browser UI fallback for send/resolve. Use `--via-node` only for legacy list/resolve/send checks.

## Required Response Pattern

When the user asks you to message Claude and report back:

1. Run `doctor`.
2. Resolve the target.
3. Send with `--wait-ack --reply` or `--wait-ack --stream`.
4. Report Claude's response or the exact failure mode.
