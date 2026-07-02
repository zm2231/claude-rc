---
name: "claude-rc-operations"
description: "Use when the user wants Codex to communicate with Claude Code sessions through claude-rc, send a message to an already-running Claude agent, ask Claude for output, fetch Claude's reply, list or resolve RC-enabled Claude sessions, check Remote Control health, or coordinate work between Codex and Claude without tmux or manual copy-paste."
---

# Claude RC Operations

Use `claude-rc` when the user wants Codex to talk to a running Claude Code session.

The important thing to remember: you can send a normal user turn to Claude and get Claude's reply from the terminal. Do not say you cannot communicate with Claude until you have checked whether `claude-rc-send` is installed and healthy.

## Prerequisite Check

Start with:

```bash
command -v claude-rc-send
claude-rc-send doctor --json
```

`doctor` should show:

- cookie read: true
- cloudflare / list: true
- daemon socket: true or informational

If `doctor` fails because Chrome is logged into the wrong Claude account, say that directly. Account mismatch is a common failure mode: a local Claude session can appear in the registry while the Claude web API returns `404` because Chrome is authenticated as a different Claude account.

## List Sessions

List the sessions Codex can target:

```bash
claude-rc-list
```

For structured parsing:

```bash
claude-rc-list --json
```

Use this before sending if the user gives a vague target like a repo name, session name, or cwd fragment.

## Resolve a Target

Resolve before sending when there is any ambiguity:

```bash
claude-rc-send resolve <target> --json
```

Targets can be:

- PID
- local Claude session id or prefix
- RC `session_...` id
- RC URL
- unique cwd or session-name fragment

If resolution returns multiple matches, ask the user which one to use or choose the exact PID/session id if the user already supplied it.

## Send a Message and Get Claude's Reply

Default pattern:

```bash
claude-rc-send <target> "message" --wait-ack --reply
```

This sends a user turn, waits for transcript acknowledgement when possible, and fetches the assistant reply text.

If the user only wants to send a message and does not need the reply:

```bash
claude-rc-send <target> "message" --wait-ack
```

If the user asks "what did Claude say?" after a send:

```bash
claude-rc-send last-reply <target>
```

## Streaming and Watching

For local sessions on this machine, `--stream` tails the Claude transcript:

```bash
claude-rc-send <target> "message" --stream --timeout 120
```

Use this when the user wants to see tool calls, tool results, or final text as they appear in the local transcript.

For remote/no-transcript sessions, use watch or reply fetching:

```bash
claude-rc-send send+watch <target> "message" --timeout 120
claude-rc-send <target> "message" --wait-ack --reply
```

Watch streams worker state and summaries, not token-by-token assistant text. Use `--reply` or `last-reply` for full text.

## Dry Run Before Risky Sends

Use dry-run when the target is unclear or the message has side effects:

```bash
claude-rc-send <target> "message" --dry-run --json
```

Dry-run resolves the target and loads cookies but does not send.

## Operational Defaults

- Prefer the default Python backend.
- Use `--via-surf` only if the Python backend is blocked by Cloudflare and the user wants browser UI fallback.
- Use `--via-node` only for legacy list/resolve/send behavior.
- Do not use tmux, AppleScript, keyboard typing, or terminal injection when `claude-rc` can target the session directly.
- Do not read or print raw cookies. `doctor` is safe; cookie values are not needed.

## Common Workflows

### Ask Claude a question

```bash
claude-rc-send <target> "Question for Claude..." --wait-ack --reply
```

Then summarize the reply to the user.

### Delegate work to Claude

```bash
claude-rc-send <target> "Please investigate X and report findings. Do not modify files." --wait-ack
```

If the user asks for the result later:

```bash
claude-rc-send last-reply <target>
```

### Check whether a session can be addressed

```bash
claude-rc-send resolve <target> --json
claude-rc-send <target> "ping" --dry-run --json
```

Use a real send only after the user confirms the target or the target is unambiguous.

## Failure Modes

### `session not found` or HTTP 404

Most likely causes:

- Chrome is signed into a different Claude account than the target Claude Code session.
- Remote Control is not enabled for that session.
- The local session registry has stale RC metadata.

Run:

```bash
claude-rc-send doctor --json
claude-rc-send resolve <target> --json
claude-rc-list
```

Then tell the user what differs: account, target, registry, or cloud reachability.

### `--stream requires a local transcript`

The target is not local to this machine, or the transcript path is missing. Use:

```bash
claude-rc-send <target> "message" --wait-ack --reply
claude-rc-send last-reply <target>
```

### Cloudflare challenge

Ask the user to open `https://claude.ai` in Chrome and let Cloudflare resolve. Then retry `doctor`.

If direct Python remains blocked and `surf-cli` is installed:

```bash
claude-rc-send --via-surf <target> "message"
```

## Response Handling

When the user asks you to "message Claude and tell me what it says," do all of it:

1. Run `doctor`.
2. Resolve the target.
3. Send with `--wait-ack --reply`.
4. Report Claude's response in your final answer.

Do not make the user manually run `last-reply` unless a command fails or the session is still working.
