/* eSIM NL2SQL 前端 —— 纯静态 SPA，无构建步骤
 * 依赖：Plotly (CDN)
 * 后端契约见 app/api/v1/* —— 前缀 /api/v1
 */
(function () {
  "use strict";

  const LS = window.localStorage;
  // 默认 API 地址：若由后端同源托管（http://host:port/），直接用当前 origin 避免跨域；
  // 若以 file:// 方式直接打开，则回退到本地 8000 端口（此时仍需后端 CORS 放行）。
  const DEFAULT_BASE =
    location.protocol === "file:" ? "http://localhost:8000" : location.origin;
  const state = {
    base: LS.getItem("esim_api_base") || DEFAULT_BASE,
    token: LS.getItem("esim_jwt") || "",
    user: null,
    convId: null,
    convTitle: "",
    busy: false,
  };

  // ---------- 工具 ----------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  function el(tag, props = {}, children = []) {
    const n = document.createElement(tag);
    Object.entries(props).forEach(([k, v]) => {
      if (k === "class") n.className = v;
      else if (k === "html") n.innerHTML = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) n.setAttribute(k, v);
    });
    (Array.isArray(children) ? children : [children]).forEach((c) => {
      if (c == null) return;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return n;
  }
  function icon(name, cls) {
    const wrap = document.createElement("span");
    wrap.innerHTML = `<svg class="ic${cls ? " " + cls : ""}" aria-hidden="true"><use href="#${name}"/></svg>`;
    return wrap.firstChild;
  }
  function toast(msg, type, ms = 2600) {
    const t = $("#toast");
    t.className = "toast" + (type ? " " + type : "");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), ms);
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }
  function nowTime() {
    const d = new Date();
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  function isNum(v) {
    if (v == null || v === "") return false;
    return typeof v === "number" ? isFinite(v) : !isNaN(Number(v));
  }
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) { /* 降级到 execCommand */ }
    try {
      const ta = el("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;opacity:0;left:-9999px";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (_) { return false; }
  }

  /* ---------------- SQL 语法高亮 ---------------- */
  const SQL_KW = new Set(("SELECT FROM WHERE AND OR NOT NULL IS IN EXISTS BETWEEN LIKE ILIKE AS ON JOIN LEFT RIGHT INNER OUTER FULL CROSS " +
    "GROUP BY ORDER HAVING LIMIT OFFSET UNION ALL DISTINCT CASE WHEN THEN ELSE END WITH VALUES INSERT INTO UPDATE DELETE SET " +
    "DROP ALTER CREATE TRUNCATE TABLE VIEW INDEX GRANT REVOKE ASC DESC OVER PARTITION ROWS RANGE PRECEDING FOLLOWING CURRENT ROW").split(/\s+/));
  const SQL_FN = new Set(("COUNT SUM AVG MIN MAX ROUND FLOOR CEIL ABS COALESCE IFNULL NULLIF CONCAT CONCAT_WS CAST CONVERT " +
    "DATE_FORMAT STR_TO_DATE NOW CURDATE CURRENT_DATE DATE_ADD DATE_SUB DATEDIFF TIMESTAMPDIFF YEAR MONTH DAY HOUR MINUTE SECOND " +
    "SUBSTRING SUBSTR TRIM LTRIM RTRIM UPPER LOWER LENGTH CHAR_LENGTH REPLACE IF JSON_EXTRACT ROW_NUMBER").split(/\s+/));
  const SQL_RE = /(--[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)|((?:'[^']*'|"[^"]*"|`[^`]*`)+)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_$]*)|([+\-*/%=<>!&|^~,.()]+)/g;
  function highlightSQL(sql) {
    let out = "", last = 0, m;
    SQL_RE.lastIndex = 0;
    while ((m = SQL_RE.exec(sql)) !== null) {
      out += esc(sql.slice(last, m.index));
      last = m.index + m[0].length;
      if (m[1]) out += '<span class="c">' + esc(m[1]) + "</span>";
      else if (m[2]) out += '<span class="s">' + esc(m[2]) + "</span>";
      else if (m[3]) out += '<span class="n">' + esc(m[3]) + "</span>";
      else if (m[4]) {
        const up = m[4].toUpperCase();
        const cls = SQL_KW.has(up) ? "k" : SQL_FN.has(up) ? "f" : "";
        out += cls ? '<span class="' + cls + '">' + esc(m[4]) + "</span>" : esc(m[4]);
      } else out += '<span class="o">' + esc(m[5]) + "</span>";
    }
    out += esc(sql.slice(last));
    return out;
  }

  /* ---------------- Markdown-lite（AI 摘要） ---------------- */
  function renderMarkdown(text) {
    let s = esc(text);
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/^\s*[-*]\s+(.+)$/gm, "<li>$1</li>");
    s = s.replace(/(?:<li>.*<\/li>\n?)+/g, (block) => "<ul>" + block.replace(/\n/g, "") + "</ul>");
    return s.replace(/\n/g, "<br>");
  }

  // ---------- API ----------
  async function api(method, path, body, auth = true) {
    const headers = { "Content-Type": "application/json" };
    if (auth && state.token) headers["Authorization"] = "Bearer " + state.token;
    let res;
    try {
      res = await fetch(state.base + "/api/v1" + path, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (e) {
      throw new Error("无法连接后端：" + state.base + "（" + e.message + "）");
    }
    let json;
    try { json = await res.json(); } catch { json = { code: res.status, message: res.statusText }; }
    if (!res.ok) {
      // 401 = 未认证 / token 过期。必须立即回到登录页，
      // 否则用户会在无感知的情况下继续操作（数据写了但列表查不到）。
      if (res.status === 401 && auth) onUnauthorized();
      const detail = (json && (json.detail || json.message)) || ("HTTP " + res.status);
      throw new Error(detail);
    }
    return json;
  }
  // 坑⑰：SSE 流式查询（POST /api/v1/query/stream），逐帧解析并回调
  function parseSSEFrame(frame) {
    let event = "message", data = "";
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) return null;
    try { return { event, data: JSON.parse(data) }; } catch (_) { return null; }
  }
  async function streamQuestion(q, onEvent) {
    const headers = { "Content-Type": "application/json" };
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    const res = await fetch(state.base + "/api/v1/query/stream", {
      method: "POST",
      headers,
      body: JSON.stringify({ question: q, conversation_id: state.convId || null }),
    });
    if (res.status === 401) { onUnauthorized(); throw new Error("登录已过期"); }
    if (!res.ok) throw new Error("HTTP " + res.status);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const evt = parseSSEFrame(frame);
        if (evt) onEvent(evt);
      }
    }
  }

  // 登录态失效：清凭据 + 回到登录页 + 提示。幂等，重复触发只提示一次。
  let _authExpired = false;
  function onUnauthorized() {
    if (_authExpired) return;
    _authExpired = true;
    state.token = ""; state.user = null; state.convId = null;
    LS.removeItem("esim_jwt");
    $("#app").hidden = true;
    $("#login-overlay").hidden = false;
    toast("登录已过期，请重新登录", "err");
    const u = $("#login-username");
    if (u) u.focus();
  }

  // ---------- 认证 ----------
  async function doLogin(username, password) {
    const r = await api("POST", "/auth/login", { username, password }, false);
    state.token = r.data.access_token;
    LS.setItem("esim_jwt", state.token);
    await loadMe();
    // 重新登录成功，复位过期标记，允许后续请求再次触发 401 拦截
    _authExpired = false;
  }
  async function loadMe() {
    try {
      const r = await api("GET", "/auth/me");
      state.user = r.data;
      renderUser();
    } catch { state.user = null; }
  }
  function renderUser() {
    const box = $("#user-box");
    if (!state.user) { box.textContent = ""; return; }
    const role = (state.user.role || "?").toUpperCase();
    box.innerHTML = "";
    box.appendChild(el("div", {}, [el("b", { text: state.user.username })]));
    box.appendChild(el("div", { class: "role-chip", text: role }));
    if (state.user.mvno_id) {
      box.appendChild(el("div", { style: "opacity:.72;font-size:11.5px", text: "MVNO " + state.user.mvno_id }));
    }
    $("#admin-btn").hidden = state.user.role !== "admin";
  }
  function logout() {
    state.token = ""; state.user = null; state.convId = null;
    LS.removeItem("esim_jwt");
    $("#app").hidden = true;
    $("#login-overlay").hidden = false;
  }

  // ---------- 对话 ----------
  async function loadConversations() {
    try {
      const r = await api("GET", "/conversation?limit=50");
      const list = (r.data && (r.data.conversations || r.data)) || [];
      const ul = $("#conv-list");
      ul.innerHTML = "";
      if (!list.length) {
        ul.appendChild(el("li", { class: "conv-empty", html: "暂无对话记录<br />点击「新建对话」开始提问" }));
        return;
      }
      list.forEach((c) => {
        const li = el("li", { class: "conv-item" + (c.id === state.convId ? " active" : ""), "data-id": c.id });
        li.appendChild(icon("i-doc"));
        li.appendChild(el("span", { class: "title", text: c.title || "(未命名对话)", title: c.title || "" }));
        const del = el("button", { class: "del", title: "删除", onclick: async (e) => {
          e.stopPropagation();
          if (!confirm("删除该对话及其消息？")) return;
          try {
            await api("DELETE", "/conversation/" + c.id);
            toast("已删除", "ok");
            if (state.convId === c.id) newConversation();
            loadConversations();
          } catch (err) { toast("删除失败：" + err.message, "err"); }
        } }, [icon("i-trash")]);
        li.appendChild(del);
        li.addEventListener("click", () => { openConversation(c.id, c.title); closeSidebar(); });
        ul.appendChild(li);
      });
    } catch (err) { /* 忽略，未登录也可 */ }
  }
  async function newConversation() {
    state.convId = null; state.convTitle = "";
    $("#chat-title").textContent = "新建对话";
    $("#chat-meta").textContent = "";
    $("#messages").innerHTML = emptyStateHTML();
    bindChips();
    $$(".conv-item").forEach((n) => n.classList.remove("active"));
    $("#question-input").focus();
  }
  async function openConversation(id, title) {
    state.convId = id; state.convTitle = title || "";
    $("#chat-title").textContent = title || "对话";
    $$(".conv-item").forEach((n) => n.classList.toggle("active", n.dataset.id === id));
    const box = $("#messages"); box.innerHTML = "";
    try {
      const r = await api("GET", "/conversation/" + id);
      const d = r.data || {};
      const msgs = d.messages || [];
      let rows = 0, ms = 0;
      msgs.forEach((m) => {
        if (m.role === "user") addUserMsg(m.content);
        else {
          addBotMsg({
            sql: m.generated_sql || "", summary: m.content || "",
            data: [], columns: [], row_count: m.row_count || 0,
            execution_time_ms: m.execution_time_ms || 0,
            status: m.sql_status || "success", error: m.error_message || null,
          });
        }
        ms++; rows += (m.row_count || 0);
      });
      $("#chat-meta").textContent = `${ms} 轮 · ${rows} 行`;
    } catch (err) { toast("加载对话失败：" + err.message, "err"); }
  }

  // ---------- 示例问题（与 scripts/eval/demo_set.json 演示用例集对齐） ----------
  const DEMO_QUESTIONS = [
    { q: "查询所有 eSIM 套餐的名称和价格", icon: "i-db", tag: "基础查询" },
    { q: "统计总共有多少个 eSIM 套餐", icon: "i-chart", tag: "聚合统计" },
    { q: "查询本月新增的 eSIM 用户数量", icon: "i-chart", tag: "时间过滤" },
    { q: "查询销量最高的前 5 个套餐", icon: "i-chart", tag: "排序 TopN" },
    { q: "统计各地区的 eSIM 用户数量", icon: "i-chart", tag: "分组聚合" },
    { q: "对比漫游订单和普通订单的数量", icon: "i-code", tag: "条件对比" },
    { q: "查询用户的手机号和邮箱", icon: "i-shield", tag: "脱敏演示" },
    { q: "统计各套餐类型的订单数量并用柱状图展示", icon: "i-chart", tag: "可视化" },
  ];

  function demoChipsHTML() {
    return DEMO_QUESTIONS.map((item) => {
      const q = typeof item === "string" ? item : item.q;
      const ico = (item && item.icon) || "i-chart";
      const tag = (item && item.tag) || "示例";
      return (
        '<button class="chip" data-q="' + esc(q) + '">' +
        '<span class="chip-ico"><svg class="ic"><use href="#' + ico + '"/></svg></span>' +
        '<span class="chip-txt"><span class="chip-tag">' + esc(tag) + "</span>" + esc(q) + "</span>" +
        "</button>"
      );
    }).join("");
  }

  function emptyStateHTML() {
    return `<div class="empty-state">
      <div class="empty-badge"><svg class="ic"><use href="#i-spark"/></svg> AI 驱动 · 自然语言查数</div>
      <h2>用一句话查询 eSIM 业务数据</h2>
      <p>描述你想知道的业务问题，AI 自动生成 SQL，在安全网关（RLS + 列级脱敏）保护下执行，并返回表格与可视化图表。</p>
      <div class="suggest">${demoChipsHTML()}</div>
      <div class="empty-feats">
        <span><svg class="ic"><use href="#i-shield"/></svg> 四层 SQL 安全网关</span>
        <span><svg class="ic"><use href="#i-db"/></svg> 行级权限 + 列级脱敏</span>
        <span><svg class="ic"><use href="#i-refresh"/></svg> 失败自动纠错重试</span>
      </div>
    </div>`;
  }

  // ---------- 发送查询 ----------
  function bindChips() {
    $$(".chip").forEach((c) => c.addEventListener("click", () => {
      $("#question-input").value = c.dataset.q || c.textContent;
      autoGrow();
      sendQuestion();
    }));
  }

  // 首页静态 empty-state 中的 .suggest 也由示例问题动态填充（单一数据源）
  function renderDemoChips() {
    const box = $("#messages .suggest");
    if (!box) return;
    box.innerHTML = demoChipsHTML();
    bindChips();
  }
  function autoGrow() {
    const t = $("#question-input");
    if (!t) return;
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight, 168) + "px";
  }
  function setSending(on) {
    state.busy = on;
    const btn = $("#send-btn");
    btn.disabled = on;
    btn.innerHTML = on
      ? '<span class="spinner"></span>'
      : '<svg class="ic" aria-hidden="true"><use href="#i-send"/></svg>';
  }
  async function sendQuestion() {
    if (state.busy) return;
    const input = $("#question-input");
    const q = input.value.trim();
    if (!q) return;

    // 确保有对话
    if (!state.convId) {
      try {
        const r = await api("POST", "/conversation", { title: q.slice(0, 30) });
        state.convId = r.data.conversation.id;
        state.convTitle = r.data.conversation.title || q.slice(0, 30);
        $("#chat-title").textContent = state.convTitle;
        loadConversations();
      } catch (err) { toast("创建对话失败：" + err.message, "err"); return; }
    }

    // 清空空状态
    if ($("#messages .empty-state")) $("#messages").innerHTML = "";

    addUserMsg(q);
    input.value = "";
    autoGrow();
    setSending(true);

    const head = el("div", { class: "md-head" }, [icon("i-spark"), "正在思考 · " + nowTime()]);
    const thinking = el("div", { class: "thinking" }, [
      el("span"), el("span"), el("span"),
      el("span", { class: "thinking-label", text: "生成 SQL 并执行查询…" }),
    ]);
    const tWrap = el("div", { class: "msg bot" }, [
      el("div", { class: "avatar", text: "AI" }),
      el("div", { class: "bubble" }, [head, thinking]),
    ]);
    $("#messages").appendChild(tWrap);
    scrollBottom();

    try {
      const bubble = tWrap.querySelector(".bubble");
      const thinking = bubble.querySelector(".thinking");
      const setStatus = (txt) => {
        const label = bubble.querySelector(".thinking-label");
        if (label) label.textContent = txt;
        else bubble.appendChild(stateCard("🧠", "状态", txt));
      };
      let sqlShown = false, dataShown = false, streamError = null;

      // 坑⑰：接入 SSE 流式查询，渐进展示 SQL / 数据 / 状态
      await streamQuestion(q, (ev) => {
        const d = ev.data || {};
        if (ev.event === "status") {
          setStatus(d.data || "处理中…");
        } else if (ev.event === "sql") {
          if (thinking) thinking.remove();
          bubble.appendChild(sqlCard(d.data || ""));
          sqlShown = true;
        } else if (ev.event === "data") {
          if (thinking) thinking.remove();
          bubble.appendChild(renderTable(d.columns || [], d.data || [], []));
          const chart = renderChart(d.columns || [], d.data || []);
          if (chart) bubble.appendChild(chart);
          dataShown = true;
        } else if (ev.event === "error") {
          streamError = d.data || "查询失败";
        }
        scrollBottom();
      });

      if (thinking) thinking.remove();
      if (streamError) {
        bubble.appendChild(stateCard("⚠️", "查询失败", streamError));
      } else if (!sqlShown && !dataShown) {
        bubble.appendChild(stateCard("ℹ️", "无结果", "未生成 SQL 或返回数据。"));
      } else {
        const meta = el("div", { class: "meta-row" });
        meta.appendChild(el("span", { class: "badge ok", html: "✅ 查询完成" }));
        bubble.appendChild(meta);
      }
      refreshMeta();
      loadConversations(); // 会话已在服务端保存（坑⑯）
    } catch (err) {
      tWrap.remove();
      if (!/登录已过期/.test(err.message)) addBotMsg({ status: "error", error: err.message });
    } finally {
      setSending(false);
      scrollBottom();
    }
  }
  function refreshMeta() {
    const n = $("#messages").querySelectorAll(".msg.bot").length;
    $("#chat-meta").textContent = `对话进行中 · ${n} 轮`;
  }

  // ---------- 渲染消息 ----------
  function addUserMsg(text) {
    const m = el("div", { class: "msg user" }, [
      el("div", { class: "avatar", text: "我" }),
      el("div", { class: "bubble" }, [el("p", { text })]),
    ]);
    $("#messages").appendChild(m); scrollBottom();
  }

  function stateCard(ico, title, text) {
    return el("div", { class: "state-card" }, [
      el("i", { text: ico }),
      el("div", {}, [el("b", { text: title }), el("div", { text })]),
    ]);
  }

  function sqlCard(sql) {
    const copyBtn = el("span", { class: "copy", text: "复制", onclick: async () => {
      const ok = await copyText(sql);
      if (!ok) { toast("复制失败，请手动选择", "err"); return; }
      copyBtn.textContent = "已复制 ✓";
      copyBtn.classList.add("done");
      toast("SQL 已复制", "ok", 1600);
      setTimeout(() => { copyBtn.textContent = "复制"; copyBtn.classList.remove("done"); }, 1800);
    } });
    const head = el("div", { class: "sql-head" }, [
      el("span", { class: "sql-title", text: "生成 SQL" }),
      copyBtn,
    ]);
    return el("div", { class: "sql-card" }, [
      head,
      el("pre", { class: "sql", html: highlightSQL(sql) }),
    ]);
  }

  function addBotMsg(res) {
    const bubble = el("div", { class: "bubble" });
    if (res.status === "blocked") {
      bubble.appendChild(stateCard("🚫", "安全拦截", res.error || "该查询被 SQL 安全网关拦截，未执行任何语句。"));
    } else if (res.status === "error") {
      bubble.appendChild(stateCard("⚠️", "查询失败", res.error || "未知错误"));
    } else {
      bubble.appendChild(el("div", { class: "md-head" }, [icon("i-spark"), "AI 回答 · " + nowTime()]));
      if (res.summary) bubble.appendChild(el("div", { class: "md", html: renderMarkdown(res.summary) }));
      if (res.sql) bubble.appendChild(sqlCard(res.sql));
      if (res.data && res.data.length) {
        bubble.appendChild(renderTable(res.columns, res.data, res.masked_columns));
        const chart = renderChart(res.columns, res.data);
        if (chart) bubble.appendChild(chart);
      } else if (res.row_count === 0 && res.status === "success") {
        bubble.appendChild(el("p", { class: "md", style: "color:var(--muted)", text: "查询成功，但没有匹配的数据。" }));
      }
    }
    const meta = el("div", { class: "meta-row" });
    if (res.execution_time_ms) meta.appendChild(el("span", { class: "badge ok", html: "⏱&nbsp;" + res.execution_time_ms + " ms" }));
    if (res.row_count != null && res.status !== "blocked" && res.status !== "error")
      meta.appendChild(el("span", { class: "badge", html: "📊&nbsp;" + res.row_count + " 行" }));
    if (res.masked_columns && res.masked_columns.length)
      meta.appendChild(el("span", { class: "badge warn", html: "🔒&nbsp;脱敏：" + esc(res.masked_columns.join(", ")) }));
    if (res.truncated) meta.appendChild(el("span", { class: "badge", text: "结果已截断(前100行)" }));
    if (meta.children.length) bubble.appendChild(meta);

    const m = el("div", { class: "msg bot" }, [el("div", { class: "avatar", text: "AI" }), bubble]);
    $("#messages").appendChild(m); scrollBottom();
  }

  function renderTable(columns, data, masked) {
    const maskSet = new Set(masked || []);
    const rows = data.slice(0, 100);
    const numCols = new Set(columns.filter((c) => rows.length && rows.every((r) => isNum(r[c]))));

    const thead = el("thead", {}, [
      el("tr", {}, columns.map((c) =>
        el("th", { class: numCols.has(c) ? "num-head" : "", title: "点击排序：" + c, text: c })
      )),
    ]);
    const tbody = el("tbody", {}, rows.map((row) =>
      el("tr", {}, columns.map((c) => {
        const v = row[c];
        const cell = el("td", {
          class: (numCols.has(c) ? "num " : "") + (maskSet.has(c) ? "masked" : ""),
          text: v == null ? "—" : String(v),
          title: v == null ? "" : String(v),
        });
        cell.dataset.v = v == null ? "" : String(v);
        return cell;
      }))
    ));
    const tbl = el("table", { class: "result" }, [thead, tbody]);

    // 点击表头排序（数值 / 中文字符串分别比较）
    const sort = { idx: -1, dir: 1 };
    thead.querySelectorAll("th").forEach((th, idx) => {
      th.addEventListener("click", () => {
        sort.dir = sort.idx === idx ? -sort.dir : 1;
        sort.idx = idx;
        thead.querySelectorAll("th").forEach((o) => o.classList.remove("sorted-asc", "sorted-desc"));
        th.classList.add(sort.dir === 1 ? "sorted-asc" : "sorted-desc");
        const list = Array.from(tbody.children);
        list.sort((a, b) => {
          const av = a.children[idx].dataset.v, bv = b.children[idx].dataset.v;
          if (isNum(av) && isNum(bv)) return sort.dir * (Number(av) - Number(bv));
          return sort.dir * String(av).localeCompare(String(bv), "zh-CN");
        });
        list.forEach((r) => tbody.appendChild(r));
      });
    });

    const head = el("div", { class: "table-head" }, [
      icon("i-db"),
      el("b", { text: "查询结果" }),
      el("span", { text: `${rows.length} 行 × ${columns.length} 列` }),
      el("span", { class: "spacer" }),
      el("span", { text: "点击表头可排序" }),
    ]);
    return el("div", { class: "table-card" }, [head, el("div", { class: "scroll-x" }, [tbl])]);
  }

  function recommendChart(columns, data) {
    if (!data.length || columns.length < 2) return "table";
    const numCols = columns.filter((c) => data.every((r) => r[c] == null || isNum(r[c])));
    const dateCols = columns.filter((c) => /date|time|月份|日期|day|month|周/i.test(c) || (data[0][c] && /\d{4}[-/]\d{1,2}/.test(String(data[0][c]))));
    if (dateCols.length) return "line";
    const cat = columns.find((c) => !numCols.includes(c));
    const val = numCols[0];
    if (cat && val) {
      const cats = new Set(data.map((r) => r[cat]));
      if (cats.size <= 8) return cats.size <= 4 ? "pie" : "bar";
      return "bar";
    }
    return "table";
  }

  function chartColors() {
    return {
      primary: cssVar("--brand-500") || "#6366f1",
      text: cssVar("--text-2") || "#3c4657",
      muted: cssVar("--muted") || "#6b7689",
      grid: cssVar("--border") || "#e6e9f0",
      palette: [
        cssVar("--brand-500") || "#6366f1",
        cssVar("--teal-500") || "#14b8a6",
        cssVar("--brand-300") || "#a5b4fc",
        cssVar("--teal-200") || "#99f6e4",
        cssVar("--warn-500") || "#f59e0b",
        cssVar("--brand-700") || "#4338ca",
        cssVar("--ok-500") || "#10b981",
        cssVar("--danger-500") || "#ef4444",
      ],
    };
  }

  function drawChart(box, columns, data, type) {
    const C = chartColors();
    const numCols = columns.filter((c) => data.every((r) => r[c] == null || isNum(r[c])));
    const cat = columns.find((c) => !numCols.includes(c)) || columns[0];
    const val = numCols[0] || columns[1];
    const x = data.slice(0, 50).map((r) => String(r[cat] == null ? "" : r[cat]));
    const y = data.slice(0, 50).map((r) => Number(r[val]) || 0);

    const base = {
      marker: { color: type === "pie" ? C.palette : C.primary, line: { color: C.primary, width: type === "line" ? 2.5 : 0 } },
      hovertemplate: "%{x}: %{y}<extra></extra>",
    };
    let trace;
    if (type === "pie") {
      trace = Object.assign({ type: "pie", labels: x, values: y, hole: 0.45, textinfo: "label+percent", textfont: { color: C.text }, hovertemplate: "%{label}: %{value}<extra></extra>" }, { marker: { colors: C.palette } });
    } else if (type === "line") {
      trace = Object.assign({ type: "scatter", mode: "lines+markers", x, y, name: val, fill: "tozeroy", fillcolor: "rgba(99,102,241,.12)" }, base, { marker: { color: C.primary, size: 6 } });
    } else {
      trace = Object.assign({ type: "bar", x, y, name: val }, base);
    }

    const layout = {
      margin: { t: 16, r: 16, b: 44, l: 52 },
      height: 320,
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: { family: "inherit", size: 12, color: C.text },
      xaxis: { gridcolor: C.grid, zerolinecolor: C.grid, linecolor: C.grid, tickfont: { color: C.muted }, automargin: true },
      yaxis: { gridcolor: C.grid, zerolinecolor: C.grid, linecolor: C.grid, tickfont: { color: C.muted }, automargin: true },
      showlegend: false,
    };
    box._chart = { columns, data, type };
    if (window.Plotly) {
      Plotly.newPlot(box, [trace], layout, { responsive: true, displayModeBar: false });
    }
  }

  function renderChart(columns, data) {
    const type = recommendChart(columns, data);
    if (type === "table") return null;

    const box = el("div", { class: "chart-box" });
    const sw = el("div", { class: "chart-switch" });
    const card = el("div", { class: "chart-card" }, [
      el("div", { class: "chart-head" }, [icon("i-chart"), el("b", { text: "可视化" }), sw]),
      box,
    ]);

    let cur = type;
    [["bar", "柱状"], ["line", "折线"], ["pie", "饼图"], ["table", "隐藏"]].forEach(([t, label]) => {
      const b = el("button", {
        text: label,
        class: t === cur ? "on" : "",
        title: t === "table" ? "隐藏图表" : "切换为" + label + "图",
        onclick: () => {
          cur = t;
          sw.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
          if (t === "table") {
            card.hidden = true;
            if (window.Plotly && box.data) Plotly.purge(box);
          } else {
            card.hidden = false;
            drawChart(box, columns, data, t);
          }
        },
      });
      sw.appendChild(b);
    });

    setTimeout(() => { if (box.isConnected) drawChart(box, columns, data, cur); }, 0);
    return card;
  }

  function redrawCharts() {
    $$(".chart-box").forEach((box) => {
      const c = box._chart;
      if (c && box.isConnected && box.offsetParent !== null) drawChart(box, c.columns, c.data, c.type);
    });
  }

  function scrollBottom() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }

  // ---------- 管理面板 ----------
  function statCard(k, v, ico) {
    return el("div", { class: "stat-card" }, [
      el("div", { class: "ico" }, [icon(ico || "i-chart", "stat-ico")]),
      el("div", {}, [el("div", { class: "v", text: v ?? 0 }), el("div", { class: "k", text: k })]),
    ]);
  }
  function openAdmin() { loadTab("stats"); $("#admin-overlay").hidden = false; }
  async function loadTab(name) {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
    try {
      if (name === "stats") {
        const r = await api("GET", "/train/stats");
        const s = r.data || {};
        const grid = el("div", { class: "stat-grid" });
        grid.appendChild(statCard("DDL 表结构", s.ddl, "i-db"));
        grid.appendChild(statCard("业务文档", s.documentation, "i-doc"));
        grid.appendChild(statCard("SQL 示例", s.sql_examples, "i-code"));
        grid.appendChild(statCard("训练总数", s.total, "i-spark"));
        const panel = $("#tab-stats");
        panel.innerHTML = "";
        panel.appendChild(el("div", { class: "panel-section" }, [grid]));
      } else if (name === "audit") {
        const r = await api("GET", "/admin/audit-logs?page=1&page_size=30");
        const logs = (r.data && r.data.logs) || [];
        const tbl = el("table", { class: "result" });
        tbl.appendChild(el("thead", {}, [el("tr", {}, ["时间", "用户", "问题", "状态", "行数", "耗时(ms)"].map((h) => el("th", { text: h })))]));
        const tb = el("tbody");
        logs.slice(0, 30).forEach((l) => {
          const st = String(l.execution_status || "-").toLowerCase();
          const dot = st === "success" ? "" : st === "blocked" ? " off" : " wait";
          tb.appendChild(el("tr", {}, [
            el("td", { text: (l.created_at || "").replace("T", " ").slice(0, 19) }),
            el("td", { text: l.username || "-" }),
            el("td", { text: (l.question || "").slice(0, 40), title: l.question || "" }),
            el("td", { html: '<span class="status-dot' + dot + '"></span>' + esc(l.execution_status || "-") }),
            el("td", { class: "num", text: l.row_count ?? "-" }),
            el("td", { class: "num", text: l.execution_time_ms ?? "-" }),
          ]));
        });
        if (!logs.length) {
          tb.appendChild(el("tr", {}, [el("td", { colspan: 6, text: "暂无审计日志", style: "text-align:center;color:var(--muted);padding:22px" })]));
        }
        tbl.appendChild(tb);
        const panel = $("#tab-audit");
        panel.innerHTML = "";
        panel.appendChild(el("div", { class: "admin-table-wrap" }, [el("div", { class: "scroll-x" }, [tbl])]));
      } else if (name === "security") {
        const r = await api("GET", "/admin/security/status");
        const d = (r.data || {});
        const sql = (d.sql_security || {});
        const mask = (d.masking || {});
        const panel = $("#tab-security");
        panel.innerHTML = "";

        const s1 = el("div", { class: "panel-section" }, [el("h3", { text: "SQL 安全网关" })]);
        const g = el("div", { class: "stat-grid" });
        [["输入过滤", sql.input_filter, "i-shield"], ["Schema 限制", sql.schema_limiter, "i-db"],
         ["SQL 校验", sql.sql_validator, "i-code"], ["结果检查", sql.post_checker, "i-chart"]].forEach(([k, v, ico]) =>
          g.appendChild(statCard(k, v ? "✅" : "❌", ico)));
        s1.appendChild(g);
        panel.appendChild(s1);

        const s2 = el("div", { class: "panel-section" }, [el("h3", { text: "允许访问的表" })]);
        const tables = sql.allowed_tables || [];
        const wrap2 = el("div", { style: "display:flex;flex-wrap:wrap;gap:6px" });
        (tables.length ? tables : ["（未配置白名单）"]).forEach((t) => wrap2.appendChild(el("span", { class: "badge", text: t })));
        s2.appendChild(wrap2);
        panel.appendChild(s2);

        const s3 = el("div", { class: "panel-section" }, [el("h3", { text: "数据脱敏" })]);
        const g3 = el("div", { class: "stat-grid" });
        g3.appendChild(statCard("脱敏状态", mask.enabled ? "✅" : "❌", "i-shield"));
        g3.appendChild(statCard("生效规则", Object.keys(mask.rules || {}).length, "i-lock"));
        s3.appendChild(g3);
        panel.appendChild(s3);
      } else if (name === "users") {
        const r = await api("GET", "/admin/users?page=1&page_size=50");
        const items = ((r.data && r.data.users) || (r.data && r.data.items) || []);
        const tbl = el("table", { class: "result" });
        tbl.appendChild(el("thead", {}, [el("tr", {}, ["ID", "用户名", "角色", "MVNO", "状态"].map((h) => el("th", { text: h })))]));
        const tb = el("tbody");
        items.forEach((u) => tb.appendChild(el("tr", {}, [
          el("td", { class: "num", text: u.id }),
          el("td", { text: u.username }),
          el("td", { html: '<span class="role-pill ' + esc(u.role || "") + '">' + esc(u.role || "-") + "</span>" }),
          el("td", { class: "num", text: u.mvno_id ?? "-" }),
          el("td", { html: '<span class="status-dot' + (u.is_active ? "" : " off") + '"></span>' + (u.is_active ? "正常" : "停用") }),
        ])));
        if (!items.length) {
          tb.appendChild(el("tr", {}, [el("td", { colspan: 5, text: "暂无用户", style: "text-align:center;color:var(--muted);padding:22px" })]));
        }
        tbl.appendChild(tb);
        const panel = $("#tab-users");
        panel.innerHTML = "";
        panel.appendChild(el("div", { class: "admin-table-wrap" }, [el("div", { class: "scroll-x" }, [tbl])]));
      }
    } catch (err) { toast("加载失败：" + err.message, "err"); }
  }

  // ---------- 设置 / 主题 ----------
  function openSettings() {
    $("#api-base-input").value = state.base;
    $("#theme-select").value = document.documentElement.getAttribute("data-theme") || "light";
    $("#settings-overlay").hidden = false;
  }
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    LS.setItem("esim_theme", t);
    setTimeout(redrawCharts, 60);
  }

  // ---------- 移动端抽屉 ----------
  function openSidebar() {
    $("#sidebar").classList.add("open");
    $("#sidebar-scrim").hidden = false;
  }
  function closeSidebar() {
    $("#sidebar").classList.remove("open");
    $("#sidebar-scrim").hidden = true;
  }

  // ---------- 事件绑定 ----------
  function bind() {
    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("#login-btn"); btn.disabled = true; $("#login-error").hidden = true;
      const old = btn.textContent; btn.textContent = "登录中…";
      try {
        await doLogin($("#login-username").value.trim(), $("#login-password").value);
        $("#login-overlay").hidden = true; $("#app").hidden = false;
        loadConversations(); newConversation();
      } catch (err) {
        const eb = $("#login-error");
        eb.innerHTML = '<svg class="ic"><use href="#i-shield"/></svg><span>' + esc(err.message) + "</span>";
        eb.hidden = false;
      } finally { btn.disabled = false; btn.textContent = old; }
    });
    $("#new-chat-btn").addEventListener("click", () => { newConversation(); closeSidebar(); });
    $("#send-btn").addEventListener("click", sendQuestion);
    $("#question-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendQuestion(); }
    });
    $("#question-input").addEventListener("input", autoGrow);
    $("#admin-btn").addEventListener("click", () => { closeSidebar(); openAdmin(); });
    $("#settings-btn").addEventListener("click", () => { closeSidebar(); openSettings(); });
    $("#logout-btn").addEventListener("click", logout);
    $("#theme-toggle").addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      applyTheme(next);
      $("#theme-select").value = next;
    });
    $("#save-settings-btn").addEventListener("click", () => {
      state.base = $("#api-base-input").value.trim().replace(/\/+$/, "");
      LS.setItem("esim_api_base", state.base);
      applyTheme($("#theme-select").value);
      $("#settings-overlay").hidden = true;
      toast("设置已保存", "ok");
    });
    $$(".tab").forEach((t) => t.addEventListener("click", () => loadTab(t.dataset.tab)));
    $$("[data-close]").forEach((b) => b.addEventListener("click", () => ($("#" + b.dataset.close).hidden = true)));
    $$(".overlay").forEach((o) => o.addEventListener("click", (e) => { if (e.target === o && o.id !== "login-overlay") o.hidden = true; }));
    $("#menu-btn").addEventListener("click", openSidebar);
    $("#sidebar-close").addEventListener("click", closeSidebar);
    $("#sidebar-scrim").addEventListener("click", closeSidebar);
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      $$(".overlay").forEach((o) => { if (!o.hidden && o.id !== "login-overlay") o.hidden = true; });
      closeSidebar();
    });
  }

  // ---------- 启动 ----------
  function init() {
    applyTheme(LS.getItem("esim_theme") || "light");
    bind();
    renderDemoChips();
    autoGrow();
    if (state.token) {
      loadMe().then(() => {
        // loadMe 内部吞掉了异常，这里必须再确认一次用户已拿到，
        // 否则 token 过期时会带着 state.user=null 进入主界面，
        // 之后创建的对话都会变成 user_id=NULL 的孤儿记录。
        if (!state.user) return;
        if (_authExpired) return; // 已被 401 拦截送回登录页
        $("#login-overlay").hidden = true; $("#app").hidden = false;
        loadConversations(); newConversation();
      }).catch(() => { /* token 失效，留在登录页 */ });
    }
  }
  document.addEventListener("DOMContentLoaded", init);
})();
