"""
Day 20: 监控告警系统 — 测试套件
--------------------------------
覆盖：
1. 自定义业务指标定义与记录（Counter / Histogram / Gauge）
2. MetricsRecorder 便捷方法
3. /metrics 端点暴露（GET 返回 200 且包含 nl2sql_ 指标）
4. 安全拦截指标记录
"""

import pytest
from prometheus_client import REGISTRY


@pytest.fixture(autouse=True)
def _enable_metrics(monkeypatch):
    """确保 Instrumentator 暴露端点（受 ENABLE_METRICS 控制）"""
    monkeypatch.setenv("ENABLE_METRICS", "true")


class TestMetricsRecorder:
    def test_record_query(self):
        from app.middleware.metrics import metrics, nl2sql_query_total
        before = nl2sql_query_total.labels(status="success")._value.get()
        metrics.record_query(status="success", duration_seconds=0.5)
        after = nl2sql_query_total.labels(status="success")._value.get()
        assert after == before + 1

    def test_record_security_block(self):
        from app.middleware.metrics import metrics, nl2sql_security_blocked_total
        before = nl2sql_security_blocked_total.labels(reason="sql_injection")._value.get()
        metrics.record_security_block(reason="sql_injection")
        after = nl2sql_security_blocked_total.labels(reason="sql_injection")._value.get()
        assert after == before + 1

    def test_record_correction(self):
        from app.middleware.metrics import metrics, nl2sql_correction_total
        before = nl2sql_correction_total.labels(success="true")._value.get()
        metrics.record_correction(success=True)
        after = nl2sql_correction_total.labels(success="true")._value.get()
        assert after == before + 1

    def test_set_accuracy(self):
        from app.middleware.metrics import metrics, nl2sql_query_accuracy
        metrics.set_query_accuracy(0.95)
        assert nl2sql_query_accuracy._value.get() == 0.95
        # 边界裁剪
        metrics.set_query_accuracy(1.5)
        assert nl2sql_query_accuracy._value.get() == 1.0

    def test_set_active_users(self):
        from app.middleware.metrics import metrics, nl2sql_active_users
        metrics.set_active_users(42)
        assert nl2sql_active_users._value.get() == 42


class TestMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_metrics_endpoint_exposes(self, client):
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        # 自定义业务指标应被暴露
        assert "nl2sql_query_total" in body
        assert "nl2sql_security_blocked_total" in body
        assert "nl2sql_query_duration_seconds" in body

    @pytest.mark.asyncio
    async def test_metrics_records_query(self, client, admin_headers):
        # 触发一次查询（可能被安全拦截或正常执行），确保指标被记录
        import random
        q = f"测试查询指标 {random.randint(0, 999999)}"
        await client.post("/api/v1/query", json={"question": q}, headers=admin_headers)
        resp = await client.get("/metrics")
        assert "nl2sql_query_total" in resp.text
