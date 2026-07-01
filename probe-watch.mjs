import { getCookies, toCookieHeader } from "@steipete/sweet-cookie";

const sid = process.env.PROBE_SESSION_ID || "session_REPLACE_ME";
if (sid === "session_REPLACE_ME") {
  throw new Error("set PROBE_SESSION_ID=session_... before running this probe");
}
const r = await getCookies({ url: "https://claude.ai/", profile: "Default" });
const cookie = toCookieHeader(r.cookies);
const H = {
  cookie,
  "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
  origin: "https://claude.ai", referer: "https://claude.ai/code/" + sid,
  "anthropic-version": "2023-06-01", "anthropic-beta": "ccr-byoc-2025-07-29",
  "anthropic-client-platform": "web_claude_ai", "anthropic-client-feature": "ccr", "anthropic-client-version": "1.0.0",
};

// 1. list -> resume_token
const listResp = await fetch("https://claude.ai/v1/code/sessions?statuses=active&statuses=paused&limit=5", { headers: { ...H, accept: "application/json" } });
const token = (await listResp.json()).resume_token;
console.log("token:", token);

// 2. optionally send a message to create activity
const cse = "cse_" + sid.replace(/^session_/, "");
const uuid = crypto.randomUUID();
if (process.env.PROBE_ALLOW_SEND === "1") {
  const sresp = await fetch("https://claude.ai/v1/code/sessions/" + cse + "/events", {
    method: "POST", headers: { ...H, "content-type": "application/json" },
    body: JSON.stringify({ events: [{ payload: { type: "user", uuid, session_id: sid, parent_tool_use_id: null, message: { role: "user", content: "watch-probe: reply exactly ACK_CDX_WATCH only." } } }] }),
  });
  console.log("send:", sresp.status, uuid);
} else {
  console.log("send: skipped (set PROBE_ALLOW_SEND=1 to post a probe message)");
}

// 3. open watch SSE, collect for 6s
const watchResp = await fetch("https://claude.ai/v1/code/sessions/watch?exclude_tags=-&resume_token=" + encodeURIComponent(token), { headers: { ...H, accept: "text/event-stream" } });
console.log("watch:", watchResp.status, watchResp.headers.get("content-type"));
const reader = watchResp.body.getReader();
const dec = new TextDecoder();
let got = ""; const start = Date.now();
while (Date.now() - start < 6000) {
  const { value, done } = await reader.read();
  if (done) break;
  got += dec.decode(value, { stream: true });
  if (got.length > 2500) break;
}
console.log("=== SSE events ===");
console.log(got.slice(0, 2200));
process.exit(0);
