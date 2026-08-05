"""
管理员 API
---------
提供审计日志查询、系统统计、用户管理等管理接口。
所有接口需要管理员权限。
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, Field

from app.core.auth import require_admin
from app.services.audit_service import audit_service
from app.services.auth_service import auth_service
from app.utils.errors import BadRequestException, NotFoundException
router = APIRouter()


# ============================================================
# 请求模型（用户管理）
# ============================================================

class CreateUserRequest(BaseModel):
    """管理员创建用户请求"""
    username: str = Field(..., min_length=3, max_length=100, description="用户名")
    email: EmailStr = Field(..., description="邮箱")
    password: str = Field(..., min_length=6, max_length=128, description="密码")
    role: str = Field(default="analyst", description="角色: admin/analyst/viewer")
    mvno_id: int | None = Field(default=None, description="关联 MVNO ID（可选）")


class UpdateUserRoleRequest(BaseModel):
    """更新用户角色请求"""
    role: str = Field(..., description="新角色: admin/analyst/viewer")


# ============================================================
# 用户管理端点
# ============================================================

@router.get("/users", response_model=dict)
async def list_users(
    role: str | None = Query(None, description="按角色过滤: admin/analyst/viewer"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    admin: dict = Depends(require_admin),
):
    """获取用户列表（仅管理员）

    Args:
        role: 按角色过滤（可选）
        page: 页码
        page_size: 每页条数

    Returns:
        dict: 用户列表及分页信息
    """
    result = auth_service.list_users(role_filter=role, page=page, page_size=page_size)
    return {
        "code": 200,
        "message": "success",
        "data": result,
    }


@router.post("/users", response_model=dict)
async def create_user(
    req: CreateUserRequest,
    admin: dict = Depends(require_admin),
):
    """创建用户（仅管理员）

    Args:
        req: 创建用户请求

    Returns:
        dict: 创建成功的用户信息
    """
    try:
        user = auth_service.register_user(
            username=req.username,
            email=req.email,
            password=req.password,
            role=req.role,
            mvno_id=req.mvno_id,
        )
        return {
            "code": 200,
            "message": "用户创建成功",
            "data": user.to_dict(),
        }
    except ValueError as e:
        raise BadRequestException(str(e))


@router.put("/users/{user_id}", response_model=dict)
async def update_user_role(
    user_id: int,
    req: UpdateUserRoleRequest,
    admin: dict = Depends(require_admin),
):
    """更新用户角色（仅管理员）

    Args:
        user_id: 目标用户 ID
        req: 包含新角色的请求

    Returns:
        dict: 更新后的用户信息
    """
    try:
        user = auth_service.update_user_role(user_id, req.role)
        return {
            "code": 200,
            "message": "用户角色更新成功",
            "data": user.to_dict(),
        }
    except ValueError as e:
        if "不存在" in str(e):
            raise NotFoundException(str(e))
        raise BadRequestException(str(e))


@router.delete("/users/{user_id}", response_model=dict)
async def deactivate_user(
    user_id: int,
    admin: dict = Depends(require_admin),
):
    """停用用户（仅管理员，软删除）

    Args:
        user_id: 目标用户 ID

    Returns:
        dict: 停用后的用户信息
    """
    try:
        user = auth_service.deactivate_user(user_id)
        return {
            "code": 200,
            "message": "用户已停用",
            "data": user.to_dict(),
        }
    except ValueError as e:
        raise NotFoundException(str(e))


# ============================================================
# 审计日志端点（原有）
# ============================================================

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


# ============================================================
# 审计日志查询端点（增强）
# ============================================================

@router.get("/audit-logs")
async def get_audit_logs_filtered(
    user_id: int | None = Query(None, description="按用户ID过滤"),
    status: str | None = Query(
        None, description="按执行状态过滤: success/error/blocked"
    ),
    start_date: str | None = Query(
        None, description="起始日期 (YYYY-MM-DD 或 ISO datetime)"
    ),
    end_date: str | None = Query(
        None, description="截止日期 (YYYY-MM-DD 或 ISO datetime)"
    ),
    page: int = Query(1, ge=1, description="页码（从1开始）"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    admin: dict = Depends(require_admin),
):
    """分页查询审计日志（支持多条件过滤）

    支持按用户ID、执行状态、日期范围过滤，返回分页结果。
    """
    result = audit_service.get_logs_filtered(
        user_id=user_id,
        status=status,
        start_date=start_date,
        end_date=end_date,
        page=page,
        page_size=page_size,
    )
    return {
        "code": 200,
        "message": "success",
        "data": result,
    }


@router.get("/audit-logs/{log_id}")
async def get_audit_log_detail(
    log_id: int,
    admin: dict = Depends(require_admin),
):
    """获取单条审计日志详情

    Args:
        log_id: 审计日志ID
    """
    log = audit_service.get_log_by_id(log_id)
    if log is None:
        raise NotFoundException(f"审计日志 {log_id} 不存在")
    return {
        "code": 200,
        "message": "success",
        "data": log,
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
