"""
训练管理 API - RAG 知识注入
--------------------------
管理 ChromaDB 中的训练数据（DDL、业务文档、SQL 示例），
提升 NL2SQL 查询的准确率。
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import require_admin

from app.services import train_service

router = APIRouter(dependencies=[Depends(require_admin)])


# ============================================================
# 请求模型
# ============================================================

from pydantic import BaseModel, Field


class TrainDdlRequest(BaseModel):
    """DDL 训练请求"""
    ddl: str = Field(..., description="完整的 CREATE TABLE 或 ALTER TABLE 语句", min_length=10)
    table_name: str = Field(default="", description="表名（可选）")


class TrainDocumentationRequest(BaseModel):
    """业务文档训练请求"""
    documentation: str = Field(..., description="业务知识文档或术语说明", min_length=5)
    topic: str = Field(default="", description="主题标签（如 ARPU、Profile激活）")


class TrainSqlRequest(BaseModel):
    """SQL 示例训练请求"""
    question: str = Field(..., description="自然语言问题", min_length=3)
    sql: str = Field(..., description="对应的 SQL 语句", min_length=5)


class TrainBatchDdlRequest(BaseModel):
    """批量 DDL 训练请求"""
    items: list[TrainDdlRequest] = Field(..., min_length=1, max_length=50)


class TrainBatchDocRequest(BaseModel):
    """批量文档训练请求"""
    items: list[TrainDocumentationRequest] = Field(..., min_length=1, max_length=50)


class TrainBatchSqlRequest(BaseModel):
    """批量 SQL 示例训练请求"""
    items: list[TrainSqlRequest] = Field(..., min_length=1, max_length=50)


# ============================================================
# DDL 训练
# ============================================================

@router.post("/ddl", summary="训练 DDL 语句")
async def add_ddl(request: TrainDdlRequest):
    """添加一条 DDL 语句到训练数据

    示例:
    ```json
    {
      "ddl": "CREATE TABLE users (id INT PRIMARY KEY, ...)",
      "table_name": "users"
    }
    ```
    """
    try:
        result = train_service.train_ddl(
            ddl=request.ddl,
            table_name=request.table_name,
        )
        return {"code": 200, "message": "DDL 训练成功", "data": result}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DDL 训练失败: {e}")


@router.post("/ddl/batch", summary="批量训练 DDL")
async def batch_add_ddl(request: TrainBatchDdlRequest):
    """批量添加 DDL 语句"""
    try:
        results = []
        for item in request.items:
            result = train_service.train_ddl(
                ddl=item.ddl,
                table_name=item.table_name,
            )
            results.append(result)
        return {
            "code": 200,
            "message": f"成功训练 {len(results)} 条 DDL",
            "data": results,
        }
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ============================================================
# 业务文档训练
# ============================================================

@router.post("/documentation", summary="训练业务文档")
async def add_documentation(request: TrainDocumentationRequest):
    """添加一条业务文档/术语说明

    示例:
    ```json
    {
      "documentation": "ARPU值 = 总收入 / 活跃用户数",
      "topic": "ARPU"
    }
    ```
    """
    try:
        result = train_service.train_documentation(
            documentation=request.documentation,
            topic=request.topic,
        )
        return {"code": 200, "message": "文档训练成功", "data": result}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文档训练失败: {e}")


@router.post("/documentation/batch", summary="批量训练业务文档")
async def batch_add_documentation(request: TrainBatchDocRequest):
    """批量添加业务文档"""
    try:
        results = []
        for item in request.items:
            result = train_service.train_documentation(
                documentation=item.documentation,
                topic=item.topic,
            )
            results.append(result)
        return {
            "code": 200,
            "message": f"成功训练 {len(results)} 条文档",
            "data": results,
        }
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ============================================================
# SQL 示例训练
# ============================================================

@router.post("/sql", summary="训练 SQL 示例")
async def add_sql_example(request: TrainSqlRequest):
    """添加一条 SQL 查询示例

    示例:
    ```json
    {
      "question": "本月新增多少eSIM用户",
      "sql": "SELECT COUNT(*) FROM users WHERE created_at >= '2026-08-01'"
    }
    ```
    """
    try:
        result = train_service.train_sql_example(
            question=request.question,
            sql=request.sql,
        )
        return {"code": 200, "message": "SQL 示例训练成功", "data": result}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SQL 示例训练失败: {e}")


@router.post("/sql/batch", summary="批量训练 SQL 示例")
async def batch_add_sql_examples(request: TrainBatchSqlRequest):
    """批量添加 SQL 查询示例"""
    try:
        results = []
        for item in request.items:
            result = train_service.train_sql_example(
                question=item.question,
                sql=item.sql,
            )
            results.append(result)
        return {
            "code": 200,
            "message": f"成功训练 {len(results)} 条 SQL 示例",
            "data": results,
        }
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ============================================================
# 查询训练数据
# ============================================================

@router.get("/data", summary="获取训练数据列表")
async def get_training_data(
    type: str | None = Query(
        default=None,
        alias="type",
        description="过滤类型: ddl | documentation | sql",
        pattern="^(ddl|documentation|sql)?$",
    ),
):
    """获取所有训练数据，支持按类型过滤"""
    try:
        data = train_service.get_all_training_data(record_type=type)
        return {
            "code": 200,
            "message": "success",
            "data": {
                "items": data,
                "total": len(data),
                "filter_type": type,
            },
        }
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/stats", summary="训练数据统计")
async def get_training_stats():
    """获取训练数据的统计信息"""
    stats = train_service.get_training_stats()
    return {"code": 200, "message": "success", "data": stats}


# ============================================================
# 删除训练数据
# ============================================================

@router.delete("/data/{record_type}/{record_id}", summary="删除训练数据")
async def delete_training_data(
    record_type: str,
    record_id: str,
):
    """删除指定类型的训练数据

    Args:
        record_type: ddl | documentation | sql
        record_id: 记录唯一 ID
    """
    if record_type not in ("ddl", "documentation", "sql"):
        raise HTTPException(
            status_code=400,
            detail=f"无效的记录类型: {record_type}，有效值为 ddl, documentation, sql",
        )

    try:
        success = train_service.remove_training_data(record_type, record_id)
        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"未找到记录: {record_type}/{record_id}",
            )
        return {
            "code": 200,
            "message": "删除成功",
            "data": {"id": record_id, "type": record_type},
        }
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.delete("/data", summary="清空所有训练数据")
async def clear_all_training_data():
    """清空所有训练数据（危险操作）"""
    try:
        result = train_service.clear_all_training_data()
        return {
            "code": 200,
            "message": "所有训练数据已清空",
            "data": result,
        }
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
