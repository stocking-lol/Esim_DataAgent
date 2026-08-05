"""
查询结果可视化服务
------------------

根据 NL2SQL 返回的表格数据，自动推荐合适的图表类型并生成图表配置（JSON）。
前端可基于该配置使用 ECharts / Plotly / AntV 等任意图表库渲染。

支持的图表类型：
- bar      : 柱状图（单/多类别对比）
- line     : 折线图（时间序列趋势）
- pie      : 饼图（占比构成）
- table    : 表格（多维明细 / 不适合图形化）

设计原则：
- 完全确定性、无外部依赖、无 LLM 调用，保证低延迟
- 输出为前端无关的 JSON 配置，渲染交给前端
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 识别日期/时间列的关键字
_DATE_KEYWORDS = ("date", "time", "month", "day", "year", "week", "日期", "时间", "月份", "天", "年", "周")
# 识别可视为类别（维度）的列
_DIMENSION_KEYWORDS = ("name", "region", "type", "status", "operator", "plan", "名称", "地区", "类型", "状态", "运营商", "套餐")


def _detect_date_column(columns: list[str]) -> Optional[str]:
    """检测数据中是否为时间/日期列"""
    for col in columns:
        cl = col.lower()
        if any(k in cl for k in _DATE_KEYWORDS):
            return col
    return None


def _is_numeric_column(values: list[Any]) -> bool:
    """判断某列是否为数值列"""
    numeric_count = 0
    non_null = 0
    for v in values:
        if v is None:
            continue
        non_null += 1
        if isinstance(v, (int, float)):
            numeric_count += 1
        elif isinstance(v, str):
            try:
                float(v)
                numeric_count += 1
            except ValueError:
                pass
    return non_null > 0 and numeric_count / non_null >= 0.8


def _numeric_columns(data: list[dict], columns: list[str]) -> list[str]:
    """返回数据中的数值列名列表"""
    if not data:
        return []
    numeric = []
    for col in columns:
        values = [row.get(col) for row in data]
        if _is_numeric_column(values):
            numeric.append(col)
    return numeric


def recommend_chart_type(
    data: list[dict[str, Any]],
    columns: list[str],
) -> str:
    """根据数据形状推荐图表类型

    决策逻辑：
    1. 数据为空 / 列数不足               -> table
    2. 存在日期列 + 数值列               -> line（趋势）
    3. 1 个维度列 + 1 个数值列，类别少   -> pie（占比）/ bar
    4. 1 个维度列 + 数值列               -> bar（对比）
    5. 多维度或多数值                    -> table（明细更安全）

    Args:
        data: 查询结果行列表
        columns: 列名列表

    Returns:
        str: "bar" | "line" | "pie" | "table"
    """
    if not data or len(columns) < 2:
        return "table"

    numeric = _numeric_columns(data, columns)
    date_col = _detect_date_column(columns)

    # 时间序列 -> 折线图
    if date_col and numeric:
        return "line"

    # 维度列（非数值列）
    dimension_cols = [c for c in columns if c not in numeric]

    if len(dimension_cols) == 1 and numeric:
        dim = dimension_cols[0]
        n_categories = len({row.get(dim) for row in data})
        # 类别数 <= 8 适合饼图展示占比，否则柱状图更易读
        if n_categories <= 8 and len(numeric) == 1:
            return "pie"
        return "bar"

    if len(dimension_cols) >= 1 and numeric:
        return "bar"

    # 多维度或无明确度量 -> 表格
    return "table"


def generate_chart_config(
    data: list[dict[str, Any]],
    columns: list[str],
    question: str = "",
    chart_type: Optional[str] = None,
) -> dict[str, Any]:
    """生成图表配置（前端无关 JSON）

    Args:
        data: 查询结果行列表
        columns: 列名列表
        question: 原始问题（用于生成标题）
        chart_type: 强制指定图表类型，None 则自动推荐

    Returns:
        dict: 图表配置，包含 type / title / labels / datasets 等
    """
    if not data or not columns:
        return {"type": "table", "title": "无数据", "data": [], "columns": []}

    numeric = _numeric_columns(data, columns)
    dimension_cols = [c for c in columns if c not in numeric]

    ctype = chart_type or recommend_chart_type(data, columns)

    # 维度列选择：优先日期列，否则取第一个非数值列
    date_col = _detect_date_column(columns)
    primary_dim = date_col or (dimension_cols[0] if dimension_cols else columns[0])

    title = question[:40] if question else "查询结果"

    config: dict[str, Any] = {
        "type": ctype,
        "title": title,
        "category_column": primary_dim,
        "value_columns": numeric or columns[1:],
        "labels": [str(row.get(primary_dim, "")) for row in data],
    }

    if ctype == "table":
        config["data"] = data
        config["columns"] = columns
    elif ctype == "line":
        config["series"] = {
            col: [row.get(col) for row in data] for col in (numeric or columns[1:])
        }
    elif ctype == "bar":
        config["series"] = {
            col: [row.get(col) for row in data] for col in (numeric or columns[1:])
        }
    elif ctype == "pie":
        value_col = numeric[0] if numeric else columns[-1]
        config["values"] = [row.get(value_col) for row in data]
        config["value_column"] = value_col

    return config


def generate_chart_html(config: dict[str, Any]) -> str:
    """根据图表配置生成可嵌入的 Plotly HTML 片段

    使用 plotly 生成自包含 HTML（含 plotly.js CDN 引用）。
    适用于需要服务端直接渲染的场景；若前端自行渲染则可忽略本函数。

    Args:
        config: generate_chart_config 返回的配置

    Returns:
        str: HTML 字符串（含 <div> 与 <script>）
    """
    try:
        import plotly.graph_objects as go
        import plotly.io as pio
    except ImportError:
        logger.warning("plotly 未安装，无法生成 chart HTML")
        return ""

    ctype = config.get("type", "table")
    title = config.get("title", "查询结果")

    if ctype == "table":
        fig = go.Figure(
            data=[go.Table(
                header=dict(values=config.get("columns", [])),
                cells=dict(values=[
                    [row.get(c) for row in config.get("data", [])]
                    for c in config.get("columns", [])
                ]),
            )],
            layout=dict(title=title),
        )
    elif ctype == "pie":
        fig = go.Figure(
            data=[go.Pie(
                labels=config.get("labels", []),
                values=config.get("values", []),
            )],
            layout=dict(title=title),
        )
    elif ctype == "line":
        fig = go.Figure(layout=dict(title=title, xaxis_title=config.get("category_column", "")))
        for col, vals in config.get("series", {}).items():
            fig.add_trace(go.Scatter(x=config.get("labels", []), y=vals, mode="lines+markers", name=col))
    else:  # bar
        fig = go.Figure(layout=dict(title=title, xaxis_title=config.get("category_column", ""), barmode="group"))
        for col, vals in config.get("series", {}).items():
            fig.add_trace(go.Bar(x=config.get("labels", []), y=vals, name=col))

    return pio.to_html(fig, include_plotlyjs="cdn", full_html=False)
