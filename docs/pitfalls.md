# 平台通用踩坑记录（架构审查 · 2026-09-01）

> 背景：对平台进行资深 Agent 应用开发工程师视角的架构审查（低压力内部业务场景）后，将发现的问题与解决方案沉淀于此。
> 适用版本：v0.9.0（git `20ecadc`）。与专项文档的关系：ChromaDB 相关坑见 `docs/pitfalls_chromadb_server.md`，本文档记录其余通用问题。
> 审查产物：`G:\Esim_DataAgent\esim_review\`（架构图、审查重点清单、SSE 缓冲复现脚本）。
> **修复状态（2026-09-01）：全部 19 项已按顺序修复并验收，全量 pytest 327 项通过（修复前 297 项）。**

---

## 1. P0：必须修（安全 / 正确性）

### 坑① 权限上下文断链：role / mvno_id 未贯穿 HTTP 链路

**现象（危害）**：多租户隔离（RLS）、角色列权限、数据脱敏、缓存租户隔离在真实 HTTP 请求路径上全部未生效；无效/过期 token 会被静默降级为匿名，却以 admin 语义继续执行查询。

**根因（证据）**：JWT 载荷不含 `mvno_id`（`app/core/auth.py`）；`/api/v1/query` 手动解析 token 且只取 user_id/username（`app/api/v1/query.py`）；`except Exception: user_info = None` 静默降级。

**修复记录（✅ 2026-09-01）**：
- `app/core/auth.py`：JWT 载荷增加 `mvno_id`（AuthManager/DBAuthManager 登录、get_current_user/get_optional_user 均返回）。
- `app/api/v1/auth.py`：refresh_token 同步写入 mvno_id。
- `app/api/v1/query.py`：两个端点改用 `Depends(get_optional_user)`，无效 token 401；匿名请求以 `viewer` 且无租户执行（不再默认 admin）；role/mvno_id 透传给服务层。
- 验收：`tests/test_review_fixes.py` 中 JWT 含 mvno_id、/query 透传 role/mvno、匿名降级 viewer、无效 token 401 共 4 项通过。

---

### 坑② RLS 上下文挂在共享单例上：并发请求会串号

**现象（危害）**：异步并发下请求 A 的 RLS 上下文可能被请求 B 覆盖，A 的 SQL 以 B 的租户身份执行 → 跨租户数据泄露。

**根因（证据）**：`CapturingRunSqlTool._current_role/_current_mvno_id` 是全局单例 `vanna_manager._run_sql_tool` 上的可变字段，`execute_query()` 在含多次 await 的 `send_message` 前 set、finally 中 reset（`query_service.py`、`vanna_instance.py`）。

**修复记录（✅ 2026-09-01）**：
- `app/core/vanna_instance.py`：引入请求级 `contextvars.ContextVar`（rls_role/rls_mvno/last_sql/last_blocked/last_block_reason），`execute()` 读取 ContextVar；`set_user_context/reset_user_context` 操作 ContextVar；捕获状态属性优先读 ContextVar。
- 验收：`test_rls_context_task_isolation`（并发任务隔离）、`test_set_reset_user_context_roundtrip` 通过。

---

### 坑③ RLS「1=0」分支写错：该拒绝的请求被原样放行

**现象（危害）**：非 admin 用户无 mvno_id 时本应拒绝访问，实际 SQL 原样执行、可看到全部租户数据。

**根因（证据）**：`rls_service.py` `inject_rls()` 的 `1=0` 分支只返回 `rls_applied=False` 原 SQL，没有注入拒绝条件。

**修复记录（✅ 2026-09-01）**：
- `app/services/rls_service.py`：`1=0` 分支用 sqlglot 在每个 SELECT 注入 `WHERE 1=0`，返回 `rls_applied=True` + 拒绝标记。
- `app/core/vanna_instance.py`（CapturingRunSqlTool）与 `app/core/mini_agent/tools.py`（SqlTool）：检测拒绝标记后直接返回 `blocked`，不进入执行与自愈。
- 验收：`test_rls_deny_all_when_no_mvno`、`test_sql_tool_blocks_no_mvno` 通过。

---

### 坑④ SSE 流式被审计中间件整体缓冲（已实测）

**现象（危害）**：`/query/stream` 客户端直到流结束才收到第一个事件（实测 5×0.25s 事件首包 2.03s 到达），渐进展示失效。

**根因（证据）**：`app/middleware/audit.py` 的 `_extract_audit_from_response()` 用 `async for chunk in response.body_iterator` 完整消费 SSE 响应体再整体回写。

**修复记录（✅ 2026-09-01）**：
- `app/middleware/audit.py`：对 `Content-Type` 含 `text/event-stream` 的响应直接跳过 body 读取（审计改由 service 层负责）。
- **关键坑中坑**：BaseHTTPMiddleware 包装后的 `response.media_type` 为 `None`，必须用 `response.headers.get("content-type")` 判断，否则会漏判并继续缓冲（已通过调试探针确认并修正）。
- 验收：`test_audit_extract_skips_sse_body`（单元）与 `test_sse_streams_through_audit_middleware_real_server`（真实 uvicorn：首包 <0.35s、多 chunk）通过。

---

### 坑⑤ K8s 三探针指向不存在的 `/healthz`

**现象（危害）**：liveness/readiness/startup 探针全部请求 `/healthz`，应用只有 `/health` → Pod 永远不会 Ready。

**修复记录（✅ 2026-09-01）**：`k8s/app-deployment.yaml` 三处探针路径改为 `/health`。验收：`test_k8s_probes_point_to_health` 通过。

---

### 坑⑥ docker-compose 中 prometheus 依赖不存在的服务

**现象（危害）**：`docker-compose up` 报错，监控栈起不来。

**修复记录（✅ 2026-09-01）**：`docker-compose.yml` 移除 prometheus 的 `depends_on: fastapi`（fastapi 服务被注释，本地开发由宿主机运行）。验收：`test_compose_prometheus_dependency_exists` 通过。

---

## 2. P1：建议修（可靠性 / 一致性）

### 坑⑦ L4 结果检查与 RLS 二次校验定义了但从未接线

**修复记录（✅ 2026-09-01）**：
- `app/services/query_service.py`：`execute_query()` 结果返回前调用 `sql_gateway.check_result(len(result.data))`，超限即 blocked。
- `app/core/vanna_instance.py` / `app/core/mini_agent/tools.py`：RLS 注入后调用 `rls_service.verify_rls()`，失败即拦截。
- 验收：`test_execute_query_applies_post_check`、`test_mini_sql_tool_verifies_rls` 通过。

---

### 坑⑧ 训练管理接口完全无认证 → RAG 知识投毒

**修复记录（✅ 2026-09-01）**：`app/api/v1/train.py` 的 router 增加 `dependencies=[Depends(require_admin)]`，全部训练端点强制 admin。验收：`test_train_endpoints_require_admin`（匿名 401 / analyst 403 / admin 放行）通过。

---

### 坑⑨ 会话资源 IDOR：查看/删除无认证无归属校验

**修复记录（✅ 2026-09-01）**：
- `app/api/v1/conversation.py`：`get_conversation_detail` / `delete_conversation` 改为 `Depends(get_current_user)`；`send_message` 强制登录；新增 `_ensure_conversation_owner()`（非 admin 且非 owner → 403）。
- 验收：`test_conversation_idor_blocked`（匿名 401、非 owner 403、owner 200）通过。

---

### 坑⑩ 流式路径功能/安全不对等

**修复记录（✅ 2026-09-01）**：
- `app/services/query_service.py`：`execute_query_stream` 对齐普通路径——缓存短路（key 隔离）、RLS 上下文（ContextVar）、数据脱敏、审计、安全拦截事件、结果缓存写入、会话保存。
- `app/api/v1/query.py`：流式端点补 API 层输入过滤（置于 Agent 初始化检查之前）。
- 验收：`test_stream_endpoint_input_filter`、`test_execute_query_stream_applies_rls_and_masking` 通过。

---

### 坑⑪ 审计重复写入且静默失败

**修复记录（✅ 2026-09-01）**：
- `app/api/v1/query.py`：service 层审计后置位 `request.state._audit_logged_by_service = True`（普通/流式/API 层拦截三条路径），中间件不再重复写。
- `app/middleware/metrics.py`：新增 `nl2sql_audit_failed_total` 计数器与 `record_audit_failed()`；`query_service._audit_log` 与 audit 中间件失败分支记录错误日志 + 计数。
- 验收：`test_audit_not_duplicated`、`test_audit_failure_records_metric` 通过。

---

### 坑⑫ 准确率/活跃用户指标无写入点

**修复记录（✅ 2026-09-01）**：
- `app/api/v1/admin.py`：新增 `POST /api/v1/admin/metrics/accuracy`（require_admin），写入 accuracy/active_users Gauge。
- `scripts/eval/run_eval.py`：live 模式支持 `--accuracy-endpoint` + `--admin-token`，评估结束后自动上报执行准确率。
- 验收：`test_metrics_accuracy_endpoint`（403/200 + Gauge 更新）通过。

---

### 坑⑬ 限流中间件：XFF 伪造、无 per-user、无清理、配置死代码

**修复记录（✅ 2026-09-01）**：
- `app/middleware/rate_limit.py`：仅当对端 IP 在 `settings.TRUSTED_PROXY_IPS` 才采信 XFF/X-Real-IP；限流键升级为 `user_id:IP:path`（Bearer token 解析）；每 5 分钟清理空窗口。
- `app/config/settings.py`：新增 `RATE_LIMIT_QUERY_PER_MINUTE=30`、`TRUSTED_PROXY_IPS=[]`；`app/main.py` 从 settings 取值注册（去掉硬编码）。
- 验收：`test_rate_limit_ignores_untrusted_xff`、`test_rate_limit_middleware_uses_settings` 通过。

---

### 坑⑭ Mini Agent 用同步 pymysql 阻塞事件循环

**修复记录（✅ 2026-09-01）**：`app/core/mini_agent/tools.py`：`_real_execute` 改为 `await asyncio.to_thread(self._real_execute_sync, sql)`；连接改用 `threading.local` 每线程独立，避免跨线程共享。验收：`test_mini_sql_tool_executes_in_thread` 通过。

---

### 坑⑮ 缓存：多副本不一致 + 权限键隔离失效

**修复记录（✅ 2026-09-01）**：
- `app/config/settings.py`：`QUERY_CACHE_BACKEND` 默认改为 `auto`（优先 Redis、失败降级内存，多副本默认一致）。
- 坑① 修复后三维 key（role|mvno|question）在 HTTP 链路恢复租户隔离；K8s ConfigMap 已确认 `QUERY_CACHE_BACKEND=redis`。
- 验收：`test_query_cache_key_isolates_role_and_mvno`、`test_k8s_cache_backend_is_redis` 通过。

---

### 坑⑯ 多轮状态双入口不一致

**修复记录（✅ 2026-09-01）**：
- `app/api/v1/query.py`：`/query` 携带 conversation_id 时调用 `ConversationService.save_query_turn()` 保存。
- `app/services/query_service.py`：`execute_query_stream` 结束同样保存会话。
- 验收：`test_query_endpoint_saves_conversation_turn` 通过。

---

## 3. P2：一致性 / 体验

### 坑⑰ 前端未消费 SSE，且响应字段不一致

**修复记录（✅ 2026-09-01）**：
- `app/api/v1/conversation.py`：send_message 响应补充 `retry_count / corrections / chart`。
- `frontend/app.js`：新增 `streamQuestion()`（fetch + ReadableStream 解析 SSE）与 `parseSSEFrame()`；发送流程改为流式渐进展示 status/sql/data/error，完成后刷新会话列表（配合坑⑯ 服务端保存）。
- 验收：`test_conversation_response_includes_retry_fields`、`test_frontend_streaming_contract`（含 `node --check` 语法校验）通过。

---

### 坑⑱ 文档数字口径漂移

**修复记录（✅ 2026-09-01）**：
- README、docs/interview_project_intro.md、docs/vanna_architecture_resume.md、docs/agent_infra_apply.md、docs/pitfalls_chromadb_server.md：测试数统一为 **327 项**（`pytest --collect-only -q` 可复现，注明基线 297 项）；自愈率统一为 **16.7±2.9%**（`scripts/eval/compare_report3.json` --trials 3）。
- 验收：`test_docs_use_unified_test_count`、`test_docs_use_unified_self_healing_rate` 通过。

---

### 坑⑲ 延迟预算未明示：摘要串行 + 重试上限

**修复记录（✅ 2026-09-01）**：
- `app/config/settings.py`：新增 `SUMMARY_ENABLED`（默认 True）。
- `app/services/query_service.py`：`execute_query` 摘要生成受开关控制，关闭后不再产生串行摘要 LLM 调用（降低尾延迟）。
- 验收：`test_summary_can_be_disabled` 通过。

---

## 4. 修复后验证清单（2026-09-01 全部完成）

1. ✅ 全量 `pytest`：297 → **327 项通过**，新增 `tests/test_review_fixes.py`（30 项验收测试，覆盖坑①-⑲）。
2. ✅ `docker-compose config` 语义校验：prometheus 不再依赖不存在的 fastapi（静态测试覆盖）。
3. ✅ K8s 探针路径与 `/health` 契约一致（静态测试覆盖；实际 `kubectl apply` 需在有集群环境执行）。
4. ✅ 双路径（`/query` 与 `/query/stream`）RLS/脱敏/注入用例一致（服务层单元测试覆盖）。
5. ✅ SSE 真实服务器测试：首包 <0.35s、多 chunk。
6. ✅ 评估脚本支持 `--accuracy-endpoint` 上报；文档数字与当前测试收集数一致。

## 5. 附：实施过程中的环境事故记录

- 修复过程中发现 `app/services/query_service.py` 工作副本被外部进程系统性损坏（全部字母 `a` 被替换为 `s`，git 显示全文件 668 行变更）。已通过 `git show HEAD` 恢复，并重新叠加用户未提交的异步检索改动（`aretrieve_context`），随后继续修复。若再次出现类似整文件字符替换，请检查是否有文本替换类工具/输入法/同步软件在 G 盘后台运行，并尽快提交代码。