"""
JSON 序列化工具
---------------
统一处理数据库驱动返回的、标准 json 模块无法序列化的类型。

为什么需要它
------------
MySQL 的 DECIMAL 列（plans.price、orders.amount、data_usage.usage_mb 等）经
pymysql 返回 ``decimal.Decimal``，标准 ``json.dumps`` 不认：

    TypeError: Object of type Decimal is not JSON serializable

这个异常在两处会造成真实故障：

1. **SSE 流式查询**（app/api/v1/query.py）—— 手写 ``json.dumps`` 序列化事件，
   抛错会中断整条事件流，前端表现为"提问后一直转圈、始终没有结果"，
   且因为异常发生在 generator 内部，客户端只看到流断开，看不到任何错误提示。
2. **Redis 查询缓存**（app/services/query_cache.py）—— 序列化 QueryResult 时
   若用 ``default=str``，Decimal 会变成字符串 ``"19.90"``，反序列化回来仍是
   字符串，前端的数值列右对齐、表格排序、图表渲染全部失效。

非流式接口（/query、/conversation/{id}/messages）不受影响，是因为 FastAPI 的
``jsonable_encoder`` 已内置同类处理；凡是绕过它手写 dumps 的地方都要用这里。

设计取舍
--------
- **Decimal → float 而非 str**：保住数值语义，让前端能直接参与计算与绘图。
  DECIMAL 用于金额时 float 的精度损失（19.90 → 19.9）对分析展示场景可接受；
  若未来需要精确金额运算，应另行引入 decimal 感知的序列化分支。
- **兜底转 str 而非抛错**：SSE 流中任何一个字段序列化失败都会毁掉整条流，
  降级为字符串让客户端仍能拿到其余数据，可用性优先于严格性。
"""

import datetime
import json
import uuid
from decimal import Decimal
from typing import Any

__all__ = ["json_default", "dumps_json"]


def json_default(obj: Any) -> Any:
    """json.dumps 的 default 回调：把数据库原生类型转为 JSON 可表示类型

    Args:
        obj: 标准 json 无法序列化的对象

    Returns:
        JSON 可表示的值（float / str / int）

    Examples:
        >>> json_default(Decimal("19.90"))
        19.9
        >>> json_default(datetime.date(2026, 9, 6))
        '2026-09-06'
    """
    # 金额/用量等 DECIMAL 列：转 float 保住数值语义，不能转 str
    if isinstance(obj, Decimal):
        return float(obj)
    # 时间戳类：转 ISO 8601，前端 new Date() 可直接解析
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    # MySQL TIME 列经 pymysql 返回 timedelta
    if isinstance(obj, datetime.timedelta):
        return obj.total_seconds()
    # BLOB / BINARY 列
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, uuid.UUID):
        return str(obj)
    # 其余未知类型降级为字符串，避免中断整条 SSE 流
    return str(obj)


def dumps_json(data: Any, **kwargs: Any) -> str:
    """带数据库类型兜底的 json.dumps

    与 ``json.dumps`` 参数一致，默认 ``ensure_ascii=False``（保留中文可读性）
    并强制 ``default=json_default``。

    Args:
        data: 待序列化对象
        **kwargs: 透传给 json.dumps

    Returns:
        JSON 字符串
    """
    kwargs.setdefault("ensure_ascii", False)
    kwargs["default"] = json_default
    return json.dumps(data, **kwargs)
