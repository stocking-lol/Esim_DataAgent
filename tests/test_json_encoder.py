"""
JSON 序列化兜底测试
-------------------
锁定两个真实线上故障的回归：

1. SSE 流式查询中断：MySQL DECIMAL 列返回 Decimal，裸 json.dumps 抛
   TypeError，整条事件流中断，前端"提问后一直转圈、无结果"。
   （SSE 的异常发生在 generator 内部，客户端甚至看不到错误提示）
2. Redis 缓存污染：用 default=str 序列化会把金额 19.90 变成字符串 "19.90"，
   回读后前端无法按数值排序、图表也无法渲染。
"""

import datetime
import json
import uuid
from decimal import Decimal

import pytest

from app.utils.json_encoder import dumps_json, json_default


class TestJsonDefault:
    """json_default 各类型分支"""

    def test_decimal_to_float(self):
        """DECIMAL 金额必须转成 float，保留数值语义"""
        assert json_default(Decimal("19.90")) == 19.9
        assert isinstance(json_default(Decimal("19.90")), float)

    def test_decimal_zero_and_negative(self):
        assert json_default(Decimal("0.00")) == 0.0
        assert json_default(Decimal("-3.25")) == -3.25

    def test_datetime_isoformat(self):
        dt = datetime.datetime(2026, 9, 6, 21, 30, 0)
        assert json_default(dt) == "2026-09-06T21:30:00"

    def test_date_isoformat(self):
        assert json_default(datetime.date(2026, 9, 6)) == "2026-09-06"

    def test_timedelta_to_seconds(self):
        """MySQL TIME 列经 pymysql 返回 timedelta"""
        assert json_default(datetime.timedelta(hours=2, minutes=30)) == 9000.0

    def test_bytes_decoded(self):
        assert json_default(b"hello") == "hello"
        assert json_default(bytearray(b"hi")) == "hi"

    def test_uuid_to_str(self):
        u = uuid.uuid4()
        assert json_default(u) == str(u)

    def test_unknown_type_falls_back_to_str(self):
        """未知类型降级为 str，不能抛错中断整条 SSE 流"""

        class Weird:
            def __str__(self):
                return "weird-obj"

        assert json_default(Weird()) == "weird-obj"


class TestDumpsJson:
    """dumps_json 端到端行为"""

    def test_nested_decimal_in_rows(self):
        """真实查询返回结构：列表套字典，值含 Decimal"""
        payload = {
            "type": "result",
            "columns": ["plan_name", "price"],
            "data": [
                {"plan_name": "欧洲漫游包", "price": Decimal("19.90")},
                {"plan_name": "全球通", "price": Decimal("99.00")},
            ],
        }
        out = json.loads(dumps_json(payload))
        assert out["data"][0]["price"] == 19.9
        assert isinstance(out["data"][0]["price"], float)
        assert out["data"][1]["price"] == 99.0

    def test_mixed_types(self):
        payload = {
            "created_at": datetime.datetime(2026, 9, 6, 12, 0),
            "amount": Decimal("128.50"),
            "usage_mb": Decimal("2048"),
            "note": b"raw",
        }
        out = json.loads(dumps_json(payload))
        assert out["created_at"] == "2026-09-06T12:00:00"
        assert out["amount"] == 128.5
        assert out["usage_mb"] == 2048.0
        assert out["note"] == "raw"

    def test_chinese_not_escaped(self):
        """ensure_ascii=False 默认开启，中文保持可读"""
        assert "欧洲漫游包" in dumps_json({"name": "欧洲漫游包"})

    def test_kwargs_passthrough(self):
        out = dumps_json({"a": 1}, indent=2)
        assert "\n" in out

    def test_regression_bare_dumps_would_fail(self):
        """回归护栏：证明裸 json.dumps 确实会抛错，dumps_json 不会"""
        payload = {"price": Decimal("19.90")}
        with pytest.raises(TypeError):
            json.dumps(payload)  # 裸调用必然失败——这正是线上故障的根因
        assert json.loads(dumps_json(payload))["price"] == 19.9


class TestSSEIntegration:
    """验证 SSE 路径确实使用了兜底编码器"""

    def test_query_module_uses_dumps_json(self):
        """query.py 的 _sse_data 必须序列化 Decimal 而不抛错"""
        from app.api.v1.query import _sse_data

        event = {
            "type": "result",
            "data": {"columns": ["price"], "rows": [[Decimal("29.90")]]},
        }
        out = json.loads(_sse_data(event))
        assert out["data"]["rows"][0][0] == 29.9

    def test_cache_serialize_keeps_numeric(self):
        """query_cache 序列化后 Decimal 仍是数值，不能退化成字符串"""
        from app.services.query_cache import RedisCacheBackend

        raw = RedisCacheBackend._serialize({"price": Decimal("19.90")})
        assert json.loads(raw)["price"] == 19.9
        # 关键：不能是 "19.90" 字符串
        assert not isinstance(json.loads(raw)["price"], str)
