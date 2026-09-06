// 复刻修复后 frontend/app.js 的解析逻辑（CRLF 规整 + status 噪音过滤）
function parseSSEFrame(frame) {
  let event = "message", data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return null;
  try { return { event, data: JSON.parse(data) }; } catch (_) { return null; }
}
const isNoise = (t) => /id='vanna-|ComponentType|ComponentLifecycle|<TaskOperation|status='(working|idle)'/.test(t);

const BASE = "http://127.0.0.1:8000/api/v1";
const login = await (await fetch(BASE + "/auth/login", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ username: "admin", password: "esim_admin_2026" }),
})).json();
const token = login.data.access_token;

let shown = [];
for (const q of process.argv.slice(2)) {
  const conv = await (await fetch(BASE + "/conversation", {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
    body: JSON.stringify({ title: "probe2" }),
  })).json();
  const cid = conv.data.conversation.id;
  const res = await fetch(BASE + "/query/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
    body: JSON.stringify({ question: q, conversation_id: cid }),
  });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const got = { status: 0, sql: 0, data: 0, error: 0, done: 0 };
  let statusShown = 0, noiseSeen = 0, lastData = null, donePayload = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
      const evt = parseSSEFrame(frame);
      if (!evt) continue;
      if (got[evt.event] !== undefined) got[evt.event]++;
      if (evt.event === "status") {
        const txt = ((evt.data || {}).data || "").trim();
        if (isNoise(txt)) noiseSeen++; else statusShown++;
      }
      if (evt.event === "data") lastData = evt.data;
      if (evt.event === "done") donePayload = evt.data;
    }
  }
  const dataShown = got.data > 0, sqlShown = got.sql > 0;
  const terminal = got.error ? "⚠️ 查询失败" : (!sqlShown && !dataShown) ? "⚠️ 无结果" : "✅ 查询完成";
  console.log("问题:", q);
  console.log("  帧解析:", JSON.stringify(got), "| 干净status:", statusShown, "| 过滤噪音status:", noiseSeen);
  if (lastData) console.log("  数据行:", Array.isArray(lastData.data) ? lastData.data.length : "?", "| 列:", JSON.stringify(lastData.columns || []));
  if (donePayload) console.log("  done:", JSON.stringify(donePayload));
  console.log("  前端终态 =>", terminal);
  console.log();
}
