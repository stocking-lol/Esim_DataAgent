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
  function toast(msg, ms = 2600) {
    const t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
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
      const detail = (json && (json.detail || json.message)) || ("HTTP " + res.status);
      throw new Error(detail);
    }
    return json;
  }

  // ---------- 认证 ----------
  async function doLogin(username, password) {
    const r = await api("POST", "/auth/login", { username, password }, false);
    state.token = r.data.access_token;
    LS.setItem("esim_jwt", state.token);
    await loadMe();
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
    box.innerHTML = `<div><b>${esc(state.user.username)}</b></div><div style="opacity:.7">${role}${state.user.mvno_id ? " · MVNO " + state.user.mvno_id : ""}</div>`;
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
      const ul = $("#conv-list"); ul.innerHTML = "";
      list.forEach((c) => {
        const li = el("li", { class: "conv-item" + (c.id === state.convId ? " active" : ""), "data-id": c.id });
        li.appendChild(el("span", { class: "title", text: c.title || "(未命名对话)", title: c.title || "" }));
        const del = el("button", { class: "del", title: "删除", text: "🗑", onclick: async (e) => {
          e.stopPropagation();
          if (!confirm("删除该对话及其消息？")) return;
          try { await api("DELETE", "/conversation/" + c.id); toast("已删除"); loadConversations(); }
          catch (err) { toast("删除失败：" + err.message); }
        } });
        li.appendChild(del);
        li.addEventListener("click", () => openConversation(c.id, c.title));
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
    } catch (err) { toast("加载对话失败：" + err.message); }
  }
  // ---------- 示例问题（与 scripts/eval/demo_set.json 演示用例集对齐） ----------
  const DEMO_QUESTIONS = [
    "查询所有 eSIM 套餐的名称和价格",
    "统计总共有多少个 eSIM 套餐",
    "查询本月新增的 eSIM 用户数量",
    "查询销量最高的前 5 个套餐",
    "统计各地区的 eSIM 用户数量",
    "对比漫游订单和普通订单的数量",
    "查询用户的手机号和邮箱",
    "统计各套餐类型的订单数量并用柱状图展示",
  ];

  function demoChipsHTML() {
    return DEMO_QUESTIONS.map((q) => `<button class="chip">${esc(q)}</button>`).join("");
  }

  function emptyStateHTML() {
    return `<div class="empty-state">
      <h2>👋 欢迎使用 eSIM NL2SQL 平台</h2>
      <p>直接输入业务问题，或点击下面的示例：</p>
      <div class="suggest">${demoChipsHTML()}</div>
      <p class="foot">查询由 AI 生成 SQL 并在安全网关（RLS + 列级脱敏）保护下执行。示例取自演示用例集（共 ${DEMO_QUESTIONS.length} 条）。</p>
    </div>`;
  }

  // ---------- 发送查询 ----------
  function bindChips() {
    $$(".chip").forEach((c) => c.addEventListener("click", () => {
      $("#question-input").value = c.textContent; sendQuestion();
    }));
  }

  // 首页静态 empty-state 中的 .suggest 也由示例问题动态填充（单一数据源）
  function renderDemoChips() {
    const box = $("#messages .suggest");
    if (!box) return;
    box.innerHTML = demoChipsHTML();
    bindChips();
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
      } catch (err) { toast("创建对话失败：" + err.message); return; }
    }

    // 清空空状态
    if ($("#messages .empty-state")) $("#messages").innerHTML = "";

    addUserMsg(q);
    input.value = "";
    state.busy = true; $("#send-btn").disabled = true;

    const thinking = el("div", { class: "thinking" }, [el("span"), el("span"), el("span")]);
    const tWrap = el("div", { class: "msg bot" }, [
      el("div", { class: "avatar", text: "AI" }),
      el("div", { class: "bubble" }, [thinking]),
    ]);
    $("#messages").appendChild(tWrap);
    scrollBottom();

    try {
      const r = await api("POST", "/conversation/" + state.convId + "/messages", { question: q });
      tWrap.remove();
      if (r.code && r.code !== 200) throw new Error(r.message || "查询失败");
      const d = r.data || {};
      addBotMsg({
        sql: d.sql || "", summary: d.summary || "",
        data: d.data || [], columns: d.columns || [],
        row_count: d.row_count || 0, execution_time_ms: d.execution_time_ms || 0,
        masked_columns: d.masked_columns || [], truncated: d.truncated || false,
        status: d.blocked ? "blocked" : (d.error ? "error" : "success"),
        error: d.error || (d.blocked ? d.block_reason : null),
      });
      refreshMeta();
    } catch (err) {
      tWrap.remove();
      addBotMsg({ status: "error", error: err.message });
    } finally {
      state.busy = false; $("#send-btn").disabled = false; scrollBottom();
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
  function addBotMsg(res) {
    const bubble = el("div", { class: "bubble" });
    if (res.status === "blocked") {
      bubble.appendChild(el("p", { class: "badge danger", text: "🚫 安全拦截：" + (res.error || "") }));
    } else if (res.status === "error") {
      bubble.appendChild(el("p", { class: "badge danger", text: "⚠️ 查询失败：" + (res.error || "未知错误") }));
    } else {
      if (res.summary) bubble.appendChild(el("p", { html: "<b>回答：</b>" + esc(res.summary).replace(/\n/g, "<br>") }));
      if (res.sql) {
        const head = el("div", { class: "sql-head" }, [
          el("span", { text: "生成 SQL" }),
          el("span", { class: "copy", text: "复制", onclick: () => { navigator.clipboard.writeText(res.sql); toast("SQL 已复制"); } }),
        ]);
        bubble.appendChild(el("div", { class: "sql-card" }, [head, el("pre", { class: "sql", text: res.sql })]));
      }
      if (res.data && res.data.length) {
        bubble.appendChild(renderTable(res.columns, res.data, res.masked_columns));
        bubble.appendChild(renderChart(res.columns, res.data));
      }
    }
    const meta = el("div", { class: "meta-row" });
    if (res.execution_time_ms) meta.appendChild(el("span", { class: "badge ok", text: "⏱ " + res.execution_time_ms + " ms" }));
    if (res.row_count != null) meta.appendChild(el("span", { class: "badge", text: "📊 " + res.row_count + " 行" }));
    if (res.masked_columns && res.masked_columns.length)
      meta.appendChild(el("span", { class: "badge warn", text: "🔒 脱敏：" + res.masked_columns.join(", ") }));
    if (res.truncated) meta.appendChild(el("span", { class: "badge", text: "结果已截断(前100行)" }));
    if (meta.children.length) bubble.appendChild(meta);

    const m = el("div", { class: "msg bot" }, [el("div", { class: "avatar", text: "AI" }), bubble]);
    $("#messages").appendChild(m); scrollBottom();
  }
  function renderTable(columns, data, masked) {
    const maskSet = new Set(masked || []);
    const thead = el("thead", {}, [el("tr", {}, columns.map((c) => el("th", { text: c })))]);
    const tbody = el("tbody", {}, data.slice(0, 100).map((row) =>
      el("tr", {}, columns.map((c) => {
        const v = row[c];
        const cell = el("td", { text: v == null ? "" : String(v) });
        if (maskSet.has(c)) cell.className = "masked";
        return cell;
      }))
    ));
    return el("div", { class: "scroll-x" }, [el("table", { class: "result" }, [thead, tbody])]);
  }
  function recommendChart(columns, data) {
    if (!data.length || columns.length < 2) return "table";
    const numCols = columns.filter((c) => data.every((r) => r[c] == null || typeof r[c] === "number" || !isNaN(Number(r[c]))));
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
  function renderChart(columns, data) {
    const type = recommendChart(columns, data);
    if (type === "table") return el("div");
    const box = el("div", { class: "chart-box" });
    const numCols = columns.filter((c) => data.every((r) => r[c] == null || typeof r[c] === "number" || !isNaN(Number(r[c]))));
    const cat = columns.find((c) => !numCols.includes(c)) || columns[0];
    const val = numCols[0] || columns[1];
    const x = data.map((r) => String(r[cat]));
    const y = data.map((r) => Number(r[val]) || 0);
    const trace = type === "pie"
      ? { type: "pie", labels: x, values: y, textinfo: "label+percent" }
      : { type, x, y, name: val, marker: { color: "var(--primary)" }, line: { color: "#4f46e5" } };
    const layout = { title: { text: val + " by " + cat, font: { size: 14 } }, margin: { t: 40, r: 20, b: 40, l: 50 }, height: 340, paper_bgcolor: "transparent", plot_bgcolor: "transparent", font: { color: getComputedStyle(document.body).color } };
    box.dataset.chart = JSON.stringify({ trace, layout });
    // Plotly 异步渲染
    setTimeout(() => {
      if (window.Plotly && box.isConnected) Plotly.newPlot(box, [trace], layout, { responsive: true, displayModeBar: false });
    }, 0);
    return box;
  }
  function scrollBottom() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }

  // ---------- 管理面板 ----------
  function openAdmin() { loadTab("stats"); $("#admin-overlay").hidden = false; }
  async function loadTab(name) {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
    try {
      if (name === "stats") {
        const r = await api("GET", "/train/stats");
        const s = r.data || {};
        $("#tab-stats").innerHTML = "";
        const cards = [
          ["DDL 表结构", s.ddl_count], ["业务文档", s.documentation_count],
          ["SQL 示例", s.sql_count], ["训练总数", s.total],
        ];
        const grid = el("div", { class: "stat-grid" });
        cards.forEach(([k, v]) => grid.appendChild(el("div", { class: "stat-card" }, [el("div", { class: "v", text: v ?? 0 }), el("div", { class: "k", text: k })])));
        $("#tab-stats").appendChild(grid);
      } else if (name === "audit") {
        const r = await api("GET", "/admin/audit-logs?page=1&page_size=30");
        const logs = (r.data && r.data.logs) || [];
        const tbl = el("table", { class: "result" });
        tbl.appendChild(el("thead", {}, [el("tr", {}, ["时间", "用户", "问题", "状态", "行数", "耗时(ms)"].map((h) => el("th", { text: h })))]));
        const tb = el("tbody");
        logs.slice(0, 30).forEach((l) => tb.appendChild(el("tr", {}, [
          el("td", { text: (l.created_at || "").replace("T", " ").slice(0, 19) }),
          el("td", { text: l.username || "-" }),
          el("td", { text: (l.question || "").slice(0, 40) }),
          el("td", { text: l.execution_status || "-" }),
          el("td", { text: l.row_count ?? "-" }),
          el("td", { text: l.execution_time_ms ?? "-" }),
        ])));
        tbl.appendChild(tb);
        $("#tab-audit").innerHTML = "";
        $("#tab-audit").appendChild(el("div", { class: "scroll-x" }, [tbl]));
      } else if (name === "security") {
        const r = await api("GET", "/admin/security/status");
        const d = (r.data || {});
        const sql = (d.sql_security || {});
        const mask = (d.masking || {});
        const wrap = el("div");
        wrap.appendChild(el("h3", { text: "SQL 安全网关" }));
        const g = el("div", { class: "stat-grid" });
        [["输入过滤", sql.input_filter], ["Schema 限制", sql.schema_limiter], ["SQL 校验", sql.sql_validator], ["结果检查", sql.post_checker]].forEach(([k, v]) =>
          g.appendChild(el("div", { class: "stat-card" }, [el("div", { class: "v", text: v ? "✅" : "❌" }), el("div", { class: "k", text: k })])));
        wrap.appendChild(g);
        wrap.appendChild(el("p", { html: "允许表：<code>" + esc((sql.allowed_tables || []).join(", ") || "全部白名单") + "</code>" }));
        wrap.appendChild(el("h3", { text: "数据脱敏" }));
        wrap.appendChild(el("p", { html: "状态：" + (mask.enabled ? "✅ 启用" : "❌ 关闭") + " · 规则数：" + Object.keys(mask.rules || {}).length }));
        $("#tab-security").innerHTML = ""; $("#tab-security").appendChild(wrap);
      } else if (name === "users") {
        const r = await api("GET", "/admin/users?page=1&page_size=50");
        const items = ((r.data && r.data.users) || (r.data && r.data.items) || []);
        const tbl = el("table", { class: "result" });
        tbl.appendChild(el("thead", {}, [el("tr", {}, ["ID", "用户名", "角色", "MVNO", "状态"].map((h) => el("th", { text: h })))]));
        const tb = el("tbody");
        items.forEach((u) => tb.appendChild(el("tr", {}, [
          el("td", { text: u.id }), el("td", { text: u.username }),
          el("td", { text: u.role }), el("td", { text: u.mvno_id ?? "-" }),
          el("td", { text: u.is_active ? "正常" : "停用" }),
        ])));
        tbl.appendChild(tb);
        $("#tab-users").innerHTML = ""; $("#tab-users").appendChild(el("div", { class: "scroll-x" }, [tbl]));
      }
    } catch (err) { toast("加载失败：" + err.message); }
  }

  // ---------- 设置 ----------
  function openSettings() {
    $("#api-base-input").value = state.base;
    $("#theme-select").value = LS.getItem("esim_theme") || "light";
    $("#settings-overlay").hidden = false;
  }
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    LS.setItem("esim_theme", t);
  }

  // ---------- 事件绑定 ----------
  function bind() {
    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("#login-btn"); btn.disabled = true; $("#login-error").hidden = true;
      try {
        await doLogin($("#login-username").value.trim(), $("#login-password").value);
        $("#login-overlay").hidden = true; $("#app").hidden = false;
        loadConversations(); newConversation();
      } catch (err) {
        const eb = $("#login-error"); eb.textContent = err.message; eb.hidden = false;
      } finally { btn.disabled = false; }
    });
    $("#new-chat-btn").addEventListener("click", newConversation);
    $("#send-btn").addEventListener("click", sendQuestion);
    $("#question-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendQuestion(); }
    });
    $("#admin-btn").addEventListener("click", openAdmin);
    $("#settings-btn").addEventListener("click", openSettings);
    $("#logout-btn").addEventListener("click", logout);
    $("#save-settings-btn").addEventListener("click", () => {
      state.base = $("#api-base-input").value.trim().replace(/\/+$/, "");
      LS.setItem("esim_api_base", state.base);
      applyTheme($("#theme-select").value);
      $("#settings-overlay").hidden = true;
      toast("设置已保存");
    });
    $$(".tab").forEach((t) => t.addEventListener("click", () => loadTab(t.dataset.tab)));
    $$("[data-close]").forEach((b) => b.addEventListener("click", () => ($("#" + b.dataset.close).hidden = true)));
    $$(".overlay").forEach((o) => o.addEventListener("click", (e) => { if (e.target === o) o.hidden = true; }));
  }

  // ---------- 启动 ----------
  function init() {
    applyTheme(LS.getItem("esim_theme") || "light");
    bind();
    renderDemoChips();
    if (state.token) {
      loadMe().then(() => {
        $("#login-overlay").hidden = true; $("#app").hidden = false;
        loadConversations(); newConversation();
      }).catch(() => { /* token 失效，留在登录页 */ });
    }
  }
  document.addEventListener("DOMContentLoaded", init);
})();
