"""
管理员 API
---------
提供审计日志查询、系统统计、用户管理等管理接口。
所有接口需要管理员权限。
"""

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_admin
from app.services.audit_service import audit_service

router = APIRouter()


@router.get("/audit/logs")
async def get_audit_logs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, description="按状态过滤: success/error/blocked"),
    admin: dict = Depends(require_admin),
):
    """获取查询审计日志"""
    logs = audit_service.get_recent_logs(limit=limit, offset=offset, status=status)
    return {
        "code": 200,
        "message": "success",
        "data": {
            "logs": logs,
            "total": len(logs),
            "limit": limit,
            "offset": offset,
        },
    }


@router.get("/audit/stats")
async def get_audit_stats(admin: dict = Depends(require_admin)):
    """获取审计统计信息"""
    stats = audit_service.get_stats()
    return {
        "code": 200,
        "message": "success",
        "data": stats,
    }


@router.get("/security/status")
async def get_security_status(admin: dict = Depends(require_admin)):
    """获取安全配置状态"""
    from app.core.sql_security import sql_gateway
    from app.services.masking_service import masking_service

    return {
        "code": 200,
        "message": "success",
        "data": {
            "sql_security": {
                "enabled": sql_gateway.sql_security_config.get("enabled", True),
                "input_filter": sql_gateway.sql_security_config.get("input_filter", {}).get("enabled", True),
                "schema_limiter": sql_gateway.sql_security_config.get("schema_limiter", {}).get("enabled", True),
                "sql_validator": sql_gateway.sql_security_config.get("sql_validator", {}).get("enabled", True),
                "post_checker": sql_gateway.sql_security_config.get("post_checker", {}).get("enabled", True),
                "allowed_tables": sql_gateway.sql_security_config.get("schema_limiter", {}).get("allowed_tables", []),
                "allowed_operations": sql_gateway.sql_security_config.get("schema_limiter", {}).get("allowed_operations", ["SELECT"]),
            },
            "masking": {
                "enabled": masking_service.enabled,
                "rules": masking_service._column_rules,
            },
        },
    }
