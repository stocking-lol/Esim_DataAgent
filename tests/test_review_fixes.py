"""审查修复回归测试：坑①-⑲ 逐项验收（与 docs/pitfalls.md 对应）。"""

import pytest

from tests.conftest import assert_success_response


# ============================================================
# 坑① 权限上下文断链：role / mvno_id 贯穿 HTTP 链路
# ============================================================

@pytest.mark.asyncio
async def test_jwt_payload_carries_mvno_id(client):
    """登录返回的 JWT 载荷必须携带 mvno_id 字段。"""
    from app.core.auth import JWTManager

    response = await client.post("/api/v1/auth/login", json={
        "username": "analyst",
        "password": "esim_analyst_2026",
    })
    data = assert_success_response(response)
    token = data["data"]["access_token"]
    payload = JWTManager.verify_token(token)
    assert "mvno_id" in payload, "JWT 载荷缺少 mvno_id"


@pytest.mark.asyncio
async def test_query_endpoint_passes_role_and_mvno(client, monkeypatch):
    """/query 必须把 role / mvno_id 透传给服务层。"""
    import app.api.v1.query as query_api
    from app.core.auth import JWTManager
    from app.services.query_service import QueryResult

    captured = {}

    async def fake_execute_query_with_retry(**kwargs):
        captured.update(kwargs)
        return QueryResult(question=kwargs["question"])

    monkeypatch.setattr(query_api, "execute_query_with_retry", fake_execute_query_with_retry)
    monkeypatch.setattr(query_api.vanna_manager, "_initialized", True)

    token = JWTManager.create_access_token({
        "sub": "42", "username": "analyst_b", "role": "analyst", "mvno_id": 7,
    })
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.post(
        "/api/v1/query",
        json={"question": "本月新增多少eSIM用户"},
        headers=headers,
    )
    assert response.status_code == 200
    assert captured["user_role"] == "analyst"
    assert captured["user_mvno_id"] == 7


@pytest.mark.asyncio
async def test_query_endpoint_anonymous_is_viewer(client, monkeypatch):
    """匿名请求不得以 admin 语义执行：降级为 viewer 且无租户。"""
    import app.api.v1.query as query_api
    from app.services.query_service import QueryResult

    captured = {}

    async def fake_execute_query_with_retry(**kwargs):
        captured.update(kwargs)
        return QueryResult(question=kwargs["question"])

    monkeypatch.setattr(query_api, "execute_query_with_retry", fake_execute_query_with_retry)
    monkeypatch.setattr(query_api.vanna_manager, "_initialized", True)

    response = await client.post(
        "/api/v1/query",
        json={"question": "本月新增多少eSIM用户"},
    )
    assert response.status_code == 200
    assert captured["user_role"] == "viewer"
    assert captured["user_mvno_id"] is None


@pytest.mark.asyncio
async def test_query_endpoint_invalid_token_fails_closed(client):
    """无效 token 一律 401，不得静默降级为匿名执行。"""
    response = await client.post(
        "/api/v1/query",
        json={"question": "本月新增多少eSIM用户"},
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert response.status_code == 401

# ============================================================
# 坑② RLS 上下文挂在共享单例上：改为请求级 ContextVar 隔离
# ============================================================

@pytest.mark.asyncio
async def test_rls_context_task_isolation():
    """并发任务的 RLS 上下文互不串号。"""
    import asyncio

    from app.core import vanna_instance as vi

    async def probe(role, mvno, results, key):
        vi._rls_role_var.set(role)
        vi._rls_mvno_var.set(mvno)
        await asyncio.sleep(0.05)
        results[key] = (vi._rls_role_var.get(), vi._rls_mvno_var.get())

    results = {}
    await asyncio.gather(
        probe("analyst", 1, results, "a"),
        probe("viewer", 2, results, "b"),
    )
    assert results["a"] == ("analyst", 1)
    assert results["b"] == ("viewer", 2)


def test_set_reset_user_context_roundtrip():
    """set/reset 用户上下文按请求隔离且可复位。"""
    from app.core import vanna_instance as vi

    tool = vi.CapturingRunSqlTool.__new__(vi.CapturingRunSqlTool)
    tool._last_sql = ""
    tool._last_blocked = False
    tool._last_block_reason = ""
    tool._current_role = "admin"
    tool._current_mvno_id = None

    tool.set_user_context("analyst", 5)
    assert vi._rls_role_var.get() == "analyst"
    assert vi._rls_mvno_var.get() == 5

    tool.reset_user_context()
    assert vi._rls_role_var.get() == "admin"
    assert vi._rls_mvno_var.get() is None
    assert tool.last_sql == ""
    assert tool.last_blocked is False

# ============================================================
# 坑③ RLS「1=0」分支：非 admin 无 mvno_id 必须拒绝
# ============================================================

@pytest.mark.asyncio
async def test_rls_deny_all_when_no_mvno():
    """inject_rls 对非 admin 且无 mvno_id 的用户必须注入 1=0 而非放行。"""
    from app.services.rls_service import rls_service

    result = rls_service.inject_rls(
        sql="SELECT * FROM users WHERE status = 'active'",
        role="analyst",
        mvno_id=None,
    )
    assert result.rls_applied is True
    assert "1 = 0" in result.sql.lower() or "1=0" in result.sql.lower()


@pytest.mark.asyncio
async def test_sql_tool_blocks_no_mvno():
    """Mini Agent 工具层对无 mvno_id 的非 admin 用户返回 blocked。"""
    from app.core.mini_agent.tools import SqlTool

    tool = SqlTool(dry_run=True)
    result = await tool.execute(
        sql="SELECT * FROM users",
        role="analyst",
        mvno_id=None,
    )
    assert result.blocked is True
    assert "mvno_id" in result.block_reason
    assert result.retryable is False

# ============================================================
# 坑④ SSE 流式被审计中间件整体缓冲
# ============================================================

def test_audit_extract_skips_sse_body():
    """审计中间件对 SSE 响应不得读取 body_iterator（否则会整体缓冲）。"""
    import pytest

    from app.middleware.audit import AuditMiddleware

    class FakeSSEResponse:
        media_type = "text/event-stream"
        headers = {"content-type": "text/event-stream; charset=utf-8"}
        body_iterator = None  # 一旦被迭代会抛 TypeError

    mw = AuditMiddleware.__new__(AuditMiddleware)

    async def run():
        result = await mw._extract_audit_from_response(FakeSSEResponse())
        assert result["execution_status"] == "success"
        return result

    result = asyncio_run(run())
    assert result["generated_sql"] is None


def asyncio_run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.asyncio
async def test_sse_streams_through_audit_middleware_real_server():
    """真实 HTTP 服务器下，经过审计中间件的 SSE 首包应在流结束前到达。"""
    import asyncio
    import json
    import socket
    import threading
    import time

    import httpx
    import uvicorn
    from fastapi import FastAPI
    from sse_starlette.sse import EventSourceResponse

    from app.middleware.audit import AuditMiddleware

    # 选一个空闲端口
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    mini = FastAPI()
    mini.add_middleware(AuditMiddleware)

    async def gen():
        for i in range(3):
            await asyncio.sleep(0.15)
            yield {"event": "status", "data": json.dumps({"type": "status", "data": f"s{i}"})}

    @mini.post("/api/v1/query/stream")
    async def ep():
        return EventSourceResponse(gen(), media_type="text/event-stream")

    server = uvicorn.Server(uvicorn.Config(mini, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)
    assert server.started, "uvicorn 未启动"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            start = time.perf_counter()
            first_at = None
            chunks = 0
            async with client.stream(
                "POST", f"http://127.0.0.1:{port}/api/v1/query/stream",
                json={"question": "x"},
            ) as resp:
                async for _ in resp.aiter_bytes():
                    if first_at is None:
                        first_at = time.perf_counter() - start
                    chunks += 1
            assert first_at is not None and first_at < 0.35, (
                f"SSE 首包被缓冲：first_at={first_at}"
            )
            assert chunks > 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ============================================================
# 坑⑤ K8s 探针指向 /healthz（不存在） 与 坑⑥ compose 依赖
# ============================================================

def _project_root():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


def test_k8s_probes_point_to_health():
    """K8s 三探针必须指向应用真实存在的 /health 端点。"""
    import yaml

    deploy = _project_root() / "k8s" / "app-deployment.yaml"
    with open(deploy, encoding="utf-8") as f:
        docs = list(yaml.safe_load_all(f))
    doc = docs[0]  # 第一个文档是 Deployment
    container = doc["spec"]["template"]["spec"]["containers"][0]
    for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
        assert container[probe]["httpGet"]["path"] == "/health"


def test_compose_prometheus_dependency_exists():
    """prometheus 不得依赖被注释的 fastapi 服务。"""
    import yaml

    compose = _project_root() / "docker-compose.yml"
    with open(compose, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    services = doc["services"]
    deps = services["prometheus"].get("depends_on", [])
    assert "fastapi" not in deps, "prometheus 依赖了不存在的 fastapi 服务"

# ============================================================
# 坑⑦ L4 结果检查与 RLS 二次校验接线
# ============================================================

@pytest.mark.asyncio
async def test_mini_sql_tool_verifies_rls(monkeypatch):
    """工具层 RLS 注入后执行 verify_rls，失败必须拦截。"""
    from app.core.mini_agent import tools as mt

    def fake_verify(sql, role, mvno_id):
        return False, "missing RLS"

    monkeypatch.setattr(mt.rls_service, "verify_rls", fake_verify)
    tool = mt.SqlTool(dry_run=True)
    result = await tool.execute(
        sql="SELECT * FROM users",
        role="analyst",
        mvno_id=5,
    )
    assert result.blocked is True
    assert "RLS 校验失败" in result.block_reason


@pytest.mark.asyncio
async def test_execute_query_applies_post_check(monkeypatch):
    """execute_query 结果返回前调用 check_result，超限即 blocked。"""
    import app.services.query_service as qs
    from app.core import vanna_instance as vi
    from app.core.sql_security import SecurityCheckResult

    async def fake_retrieve(question, max_items=5):
        return ""

    async def fake_summary(**kwargs):
        return "摘要"

    class RichData:
        rows = [{"a": 1}]
        columns = ["a"]

    class Comp:
        def __init__(self, rich=None, simple=None):
            self.rich_component = rich
            self.simple_component = simple

    async def fake_send_message(request_context, message, conversation_id):
        yield Comp(rich=RichData())
        yield Comp(simple=type("S", (), {"text": "完成"})())

    from types import SimpleNamespace

    fake_agent = SimpleNamespace(send_message=fake_send_message)

    monkeypatch.setattr(vi.vanna_manager, "_initialized", True)
    monkeypatch.setattr(vi.VannaAgentManager, "agent", property(lambda self: fake_agent))
    monkeypatch.setattr(vi.vanna_manager, "_run_sql_tool", None)
    monkeypatch.setattr(qs.vanna_manager, "create_request_context", lambda: {})
    monkeypatch.setattr(qs.vanna_manager, "aretrieve_context", fake_retrieve)
    monkeypatch.setattr(qs.vanna_manager, "get_last_sql", lambda: "SELECT 1")
    from app.core import llm as llm_mod

    monkeypatch.setattr(llm_mod.llm_service, "generate_summary", fake_summary)

    calls = []

    def fake_check(row_count):
        calls.append(row_count)
        return SecurityCheckResult(
            passed=False, layer="post_checker", reason="结果行数超限"
        )

    import app.core.sql_security as ss

    monkeypatch.setattr(ss.sql_gateway, "check_result", fake_check)

    result = await qs.execute_query(
        "测试问题", user_role="admin", user_mvno_id=None
    )
    assert calls == [1], f"check_result 未被调用: {calls}"
    assert result.blocked is True
    assert "结果行数超限" in result.block_reason

# ============================================================
# 坑⑧ 训练管理接口认证 与 坑⑨ 会话 IDOR
# ============================================================

@pytest.mark.asyncio
async def test_train_endpoints_require_admin(client, analyst_headers, admin_headers):
    """训练接口：匿名 401、非 admin 403、admin 放行。"""
    payload = {"ddl": "CREATE TABLE demo_t (id INT)", "table_name": "demo_t"}

    resp = await client.post("/api/v1/train/ddl", json=payload)
    assert resp.status_code == 401

    resp = await client.post(
        "/api/v1/train/ddl", json=payload, headers=analyst_headers
    )
    assert resp.status_code == 403

    resp = await client.post(
        "/api/v1/train/ddl", json=payload, headers=admin_headers
    )
    assert resp.status_code not in (401, 403)


@pytest.mark.asyncio
async def test_conversation_idor_blocked(client, admin_headers):
    """会话详情/删除：匿名 401，非 owner 403，owner 正常。"""
    from app.core.auth import JWTManager

    resp = await client.post(
        "/api/v1/conversation", json={"title": "IDOR 测试"}, headers=admin_headers
    )
    conv_id = resp.json()["data"]["conversation"]["id"]

    # 匿名访问他人会话 → 401
    resp = await client.get(f"/api/v1/conversation/{conv_id}")
    assert resp.status_code == 401

    # 非 owner 用户（user_id=99）→ 403
    other_token = JWTManager.create_access_token({
        "sub": "99", "username": "other", "role": "analyst", "mvno_id": 2,
    })
    other_headers = {"Authorization": f"Bearer {other_token}"}
    resp = await client.get(
        f"/api/v1/conversation/{conv_id}", headers=other_headers
    )
    assert resp.status_code == 403

    resp = await client.delete(
        f"/api/v1/conversation/{conv_id}", headers=other_headers
    )
    assert resp.status_code == 403

    # owner（admin）可读可删
    resp = await client.get(f"/api/v1/conversation/{conv_id}", headers=admin_headers)
    assert resp.status_code == 200
    resp = await client.delete(f"/api/v1/conversation/{conv_id}", headers=admin_headers)
    assert resp.status_code == 200

# ============================================================
# 坑⑩ 流式路径与普通查询对等（RLS/脱敏/审计/缓存/输入过滤）
# ============================================================

@pytest.mark.asyncio
async def test_stream_endpoint_input_filter(client):
    """流式端点：Agent 不可用时注入问题也必须被 API 层拦截。"""
    import app.api.v1.query as query_api

    query_api.vanna_manager._initialized = False
    try:
        response = await client.post(
            "/api/v1/query/stream",
            json={"question": "DROP TABLE users"},
        )
        assert response.status_code == 200
        assert "安全拦截" in response.text or "1001" in response.text
    finally:
        query_api.vanna_manager._initialized = False


@pytest.mark.asyncio
async def test_execute_query_stream_applies_rls_and_masking(monkeypatch):
    """流式执行：设置 RLS 上下文、数据脱敏、done 事件齐全。"""
    import app.services.query_service as qs
    from app.core import vanna_instance as vi
    from app.services.query_cache import query_cache
    from types import SimpleNamespace

    query_cache.clear()

    calls = []

    class FakeTool:
        last_blocked = False
        last_block_reason = ""

        def set_user_context(self, role, mvno):
            calls.append(("set", role, mvno))

        def reset_user_context(self):
            calls.append(("reset",))

    class RichData:
        rows = [{"phone_number": "13800005678"}]
        columns = ["phone_number"]

    class Comp:
        def __init__(self, rich=None, simple=None):
            self.rich_component = rich
            self.simple_component = simple

    async def fake_send(request_context, message, conversation_id):
        yield Comp(rich=RichData())

    async def fake_retrieve(question, max_items=5):
        return ""

    fake_agent = SimpleNamespace(send_message=fake_send)
    fake_tool = FakeTool()

    monkeypatch.setattr(vi.vanna_manager, "_initialized", True)
    monkeypatch.setattr(vi.VannaAgentManager, "agent", property(lambda self: fake_agent))
    monkeypatch.setattr(qs.vanna_manager, "create_request_context", lambda: {})
    monkeypatch.setattr(qs.vanna_manager, "aretrieve_context", fake_retrieve)
    monkeypatch.setattr(qs.vanna_manager, "_run_sql_tool", fake_tool)
    monkeypatch.setattr(qs.vanna_manager, "get_last_sql", lambda: "SELECT 1")

    events = []
    async for ev in qs.execute_query_stream(
        "查询手机号", user_role="analyst", user_mvno_id=3
    ):
        events.append(ev)

    assert ("set", "analyst", 3) in calls
    assert ("reset",) in calls
    data_ev = [e for e in events if e["type"] == "data"][0]
    phone = data_ev["data"][0]["phone_number"]
    assert phone.startswith("138")
    assert "*" in phone  # analyst 脱敏生效
    assert events[-1]["type"] == "done"

# ============================================================
# 坑⑪ 审计重复写入且静默失败
# ============================================================

@pytest.mark.asyncio
async def test_audit_not_duplicated(client, admin_headers, monkeypatch):
    """service 层审计后，中间件不得再写一条（去重标记生效）。"""
    import app.api.v1.query as query_api
    from app.services import audit_service as audit_mod
    from app.services.query_service import QueryResult

    calls = []
    orig = audit_mod.audit_service.log_query

    def fake_log(**kwargs):
        calls.append(kwargs)
        return orig(**kwargs)

    monkeypatch.setattr(audit_mod.audit_service, "log_query", fake_log)

    async def fake_execute(**kwargs):
        return QueryResult(
            question=kwargs["question"], sql="SELECT 1",
            data=[{"a": 1}], columns=["a"],
        )

    monkeypatch.setattr(query_api, "execute_query_with_retry", fake_execute)
    monkeypatch.setattr(query_api.vanna_manager, "_initialized", True)

    resp = await client.post(
        "/api/v1/query",
        json={"question": "本月新增多少eSIM用户"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    # 端点已置位 _audit_logged_by_service，中间件必须跳过写入（此前会多写一条）
    assert calls == [], f"审计被中间件重复写入: {len(calls)} 次"


def test_audit_failure_records_metric(monkeypatch):
    """审计写入失败时记录错误指标，不再完全静默。"""
    from app.middleware import metrics as metrics_mod
    from app.services import audit_service as audit_mod
    from app.services.query_service import QueryResult, _audit_log

    calls = []
    monkeypatch.setattr(
        metrics_mod.metrics, "record_audit_failed", lambda: calls.append(1)
    )

    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_mod.audit_service, "log_query", boom)

    _audit_log(QueryResult(question="q"), "success")  # 不应抛异常
    assert calls == [1]

# ============================================================
# 坑⑫ 准确率/活跃用户指标写入点
# ============================================================

@pytest.mark.asyncio
async def test_metrics_accuracy_endpoint(client, admin_headers, analyst_headers):
    """质量指标端点：非 admin 403，admin 可写且 Gauge 更新。"""
    from app.middleware import metrics as metrics_mod

    resp = await client.post(
        "/api/v1/admin/metrics/accuracy",
        json={"accuracy": 0.95, "active_users": 3},
        headers=analyst_headers,
    )
    assert resp.status_code == 403

    resp = await client.post(
        "/api/v1/admin/metrics/accuracy",
        json={"accuracy": 0.95, "active_users": 3},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert float(metrics_mod.nl2sql_query_accuracy._value.get()) == pytest.approx(0.95)
    assert float(metrics_mod.nl2sql_active_users._value.get()) == pytest.approx(3)

# ============================================================
# 坑⑬ 限流中间件：XFF 信任、per-user、配置接入
# ============================================================

def _make_request(forwarded=None):
    from starlette.requests import Request

    headers = []
    if forwarded:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "client": ("9.9.9.9", 123),
        "method": "POST",
        "path": "/api/v1/query",
        "query_string": b"",
        "server": ("127.0.0.1", 80),
        "scheme": "http",
    }
    return Request(scope)


def test_rate_limit_ignores_untrusted_xff(monkeypatch):
    """未配置可信代理时，X-Forwarded-For 不得被采信。"""
    from app.config.settings import settings
    from app.middleware.rate_limit import RateLimitMiddleware

    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", [])

    def dummy_app(scope, receive, send):
        raise AssertionError("unused")

    mw = RateLimitMiddleware(dummy_app)
    req = _make_request(forwarded="1.2.3.4")
    assert mw._get_client_ip(req) == "9.9.9.9"

    # 配置可信代理后采信转发头
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", ["9.9.9.9"])
    assert mw._get_client_ip(req) == "1.2.3.4"


def test_rate_limit_middleware_uses_settings():
    """限流阈值必须来自 settings，而非硬编码。"""
    from app.config.settings import settings
    from app.main import app

    rate_mw = [
        m for m in app.user_middleware
        if m.cls.__name__ == "RateLimitMiddleware"
    ]
    assert rate_mw, "未找到 RateLimitMiddleware"
    kwargs = rate_mw[0].kwargs
    assert kwargs["query_limit"] == settings.RATE_LIMIT_QUERY_PER_MINUTE
    assert kwargs["default_limit"] == settings.RATE_LIMIT_PER_MINUTE

# ============================================================
# 坑⑭ Mini Agent 同步 pymysql 阻塞事件循环
# ============================================================

@pytest.mark.asyncio
async def test_mini_sql_tool_executes_in_thread(monkeypatch):
    """_real_execute 必须经 asyncio.to_thread 执行同步 DB 逻辑。"""
    import app.core.mini_agent.tools as mt

    tool = mt.SqlTool(dry_run=False)
    called = []

    async def fake_sync(sql):
        return mt.ToolResult(success=True, sql=sql)

    monkeypatch.setattr(tool, "_real_execute_sync", fake_sync)

    def fake_to_thread(fn, *args):
        called.append(fn.__name__)
        return fn(*args)  # 返回协程，由 _real_execute await

    monkeypatch.setattr(mt.asyncio, "to_thread", fake_to_thread)

    res = await tool._real_execute("SELECT 1")
    assert called == ["fake_sync"]  # to_thread 收到的是同步执行函数
    assert res.success is True

# ============================================================
# 坑⑮ 缓存：多副本一致性 + 权限键隔离
# ============================================================

def test_query_cache_key_isolates_role_and_mvno():
    """缓存 key 必须按 role|mvno|question 隔离，防止越权命中。"""
    from app.services.query_cache import QueryCache

    k1 = QueryCache._make_key("本月新增", "analyst", 1)
    k2 = QueryCache._make_key("本月新增", "analyst", 2)
    k3 = QueryCache._make_key("本月新增", "viewer", 1)
    assert len({k1, k2, k3}) == 3
    assert QueryCache._make_key(" 本月新增 ", "admin", None).startswith(
        "admin|None|本月新增"
    )


def test_k8s_cache_backend_is_redis():
    """多副本部署的 ConfigMap 必须启用 Redis 后端（避免各副本缓存分叉）。"""
    import yaml

    cfg = _project_root() / "k8s" / "configmap.yaml"
    doc = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert doc["data"].get("QUERY_CACHE_BACKEND") == "redis"

# ============================================================
# 坑⑯ 多轮状态双入口不一致
# ============================================================

@pytest.mark.asyncio
async def test_query_endpoint_saves_conversation_turn(client, admin_headers, monkeypatch):
    """/query 携带 conversation_id 时必须保存会话历史。"""
    import app.api.v1.query as query_api
    from app.services.query_service import QueryResult

    resp = await client.post(
        "/api/v1/conversation", json={"title": "双入口"}, headers=admin_headers
    )
    conv_id = resp.json()["data"]["conversation"]["id"]

    async def fake_execute(**kwargs):
        return QueryResult(
            question=kwargs["question"], sql="SELECT 1",
            data=[{"a": 1}], columns=["a"], summary="1 行",
        )

    monkeypatch.setattr(query_api, "execute_query_with_retry", fake_execute)
    monkeypatch.setattr(query_api.vanna_manager, "_initialized", True)

    resp = await client.post(
        "/api/v1/query",
        json={"question": "本月新增", "conversation_id": conv_id},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    resp = await client.get(
        f"/api/v1/conversation/{conv_id}", headers=admin_headers
    )
    messages = resp.json()["data"]["messages"]
    roles = [m["role"] for m in messages]
    assert "user" in roles and "assistant" in roles, (
        f"会话未保存: {roles}"
    )

# ============================================================
# 坑⑰ 前端未消费 SSE 且响应字段不一致
# ============================================================

@pytest.mark.asyncio
async def test_conversation_response_includes_retry_fields(
    client, admin_headers, monkeypatch
):
    """对话消息端点必须返回 retry_count / corrections / chart。"""
    import app.api.v1.conversation as conv_api
    from app.services.query_service import QueryResult

    resp = await client.post(
        "/api/v1/conversation", json={"title": "字段契约"}, headers=admin_headers
    )
    conv_id = resp.json()["data"]["conversation"]["id"]

    async def fake_execute(**kwargs):
        return QueryResult(
            question=kwargs["question"], sql="SELECT 1",
            data=[{"a": 1}], columns=["a"], summary="1 行",
            retry_count=1, corrections=["列不存在"], chart={"type": "bar"},
        )

    monkeypatch.setattr(conv_api, "execute_query", fake_execute)
    from app.core import vanna_instance as vi

    monkeypatch.setattr(vi.vanna_manager, "_initialized", True)

    resp = await client.post(
        f"/api/v1/conversation/{conv_id}/messages",
        json={"question": "本月新增"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    d = resp.json()["data"]
    assert d["retry_count"] == 1
    assert d["corrections"] == ["列不存在"]
    assert d["chart"] == {"type": "bar"}


def test_frontend_streaming_contract():
    """前端必须消费 /query/stream，且不再把 /conversation/{id}/messages 当查询主通道。"""
    js = (_project_root() / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "query/stream" in js
    assert "streamQuestion" in js
    assert '"/conversation/" + state.convId + "/messages"' not in js

# ============================================================
# 坑⑲ 延迟预算：摘要串行调用可配置关闭
# ============================================================

@pytest.mark.asyncio
async def test_summary_can_be_disabled(monkeypatch):
    """SUMMARY_ENABLED=False 时不再调用摘要 LLM。"""
    import app.services.query_service as qs
    from app.config.settings import settings
    from app.core import vanna_instance as vi
    from app.core import llm as llm_mod
    from types import SimpleNamespace

    async def fake_retrieve(question, max_items=5):
        return ""

    class RichData:
        rows = [{"a": 1}]
        columns = ["a"]

    class Comp:
        def __init__(self, rich=None):
            self.rich_component = rich
            self.simple_component = None

    async def fake_send(request_context, message, conversation_id):
        yield Comp(rich=RichData())

    fake_agent = SimpleNamespace(send_message=fake_send)
    summary_calls = []

    async def fake_summary(**kwargs):
        summary_calls.append(1)
        return "摘要"

    monkeypatch.setattr(vi.vanna_manager, "_initialized", True)
    monkeypatch.setattr(vi.VannaAgentManager, "agent", property(lambda self: fake_agent))
    monkeypatch.setattr(vi.vanna_manager, "_run_sql_tool", None)
    monkeypatch.setattr(qs.vanna_manager, "create_request_context", lambda: {})
    monkeypatch.setattr(qs.vanna_manager, "aretrieve_context", fake_retrieve)
    monkeypatch.setattr(qs.vanna_manager, "get_last_sql", lambda: "SELECT 1")
    monkeypatch.setattr(llm_mod.llm_service, "generate_summary", fake_summary)
    monkeypatch.setattr(settings, "SUMMARY_ENABLED", False)

    result = await qs.execute_query(
        "测试问题", user_role="admin", user_mvno_id=None
    )
    assert result.data == [{"a": 1}]
    assert summary_calls == [], "摘要被关闭后仍调用了 LLM"

# ============================================================
# 坑⑱ 文档数字口径漂移
# ============================================================

def test_docs_use_unified_test_count():
    """README/面试文档中的测试数必须统一为当前可复现值 327。"""
    for name in ("README.md", "docs/interview_project_intro.md",
                 "docs/vanna_architecture_resume.md", "docs/agent_infra_apply.md"):
        p = _project_root() / name
        text = p.read_text(encoding="utf-8")
        assert "269 项" not in text, f"{name} 仍含旧口径 269 项"
        assert "278 项" not in text, f"{name} 仍含旧口径 278 项"
        assert "327 项" in text, f"{name} 缺少当前口径 327 项"


def test_docs_use_unified_self_healing_rate():
    """自愈率口径统一为 16.7±2.9%（compare_report3.json --trials 3）。"""
    for name in ("README.md", "docs/interview_project_intro.md",
                 "docs/vanna_architecture_resume.md"):
        text = (_project_root() / name).read_text(encoding="utf-8")
        assert "16.7±2.9%" in text, f"{name} 缺少统一自愈率口径"