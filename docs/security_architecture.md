# SQL 安全架构设计文档

## 1. 概述

eSIM NL2SQL 平台采用**四层防御体系**，对自然语言到 SQL 的全链路进行安全保护。从用户输入到 SQL 执行，每一层独立校验，形成纵深防御。

## 2. 架构总览

```
用户问题
    │
    ▼
┌───────────────────────────────────┐
│  第一层：输入过滤 (Input Filter)    │  ← 拦截危险关键词、Prompt 注入
│  app/core/sql_security.py          │
│  check_input(question)             │
└───────────┬───────────────────────┘
            │ 通过
            ▼
┌───────────────────────────────────┐
│  LLM 生成 SQL (DeepSeek-V3)        │
│  app/core/vanna_instance.py        │
│  agent.send_message()              │
└───────────┬───────────────────────┘
            │ SQL
            ▼
┌───────────────────────────────────┐
│  第二层：Schema 限制                │  ← 白名单表、仅 SELECT
│  validate_sql() → 关键词检查        │
│  + sqlglot AST 解析                │
└───────────┬───────────────────────┘
            │ 通过
            ▼
┌───────────────────────────────────┐
│  第三层：SQL 校验 (SQL Validator)   │  ← JOIN 限制、子查询深度、危险函数
│  validate_sql() → sqlglot 深度检查  │
│  + 系统变量检查                     │
└───────────┬───────────────────────┘
            │ 通过（可能自动添加 LIMIT）
            ▼
┌───────────────────────────────────┐
│  第四层：结果检查 (Post Checker)    │  ← 行数限制、脱敏
│  check_result(row_count)           │
│  + masking_service 脱敏            │
└───────────┬───────────────────────┘
            │
            ▼
        返回结果给用户
```

## 3. 各层职责详解

### 3.1 第一层：输入过滤 (Input Filter)

**文件**: `app/core/sql_security.py` → `check_input()`

**职责**: 在用户问题发送给 LLM 之前，拦截危险输入。

**检查项**:
- **长度限制**: 问题不超过 1000 字符
- **SQL 注入关键词**: 正则匹配 `DROP|DELETE|UPDATE|INSERT|TRUNCATE|ALTER|GRANT|REVOKE` 等关键词
- **Prompt 注入模式**: 检测 `ignore previous|disregard|system prompt|you are|act as` 等模式
- **可疑字符**: `; --`、`/* */`、`xp_` 等 SQL 注入特征

**配置**: `app/config/security.yaml` → `sql_security.input_filter`

### 3.2 第二层：Schema 限制 (Schema Limiter)

**文件**: `app/core/sql_security.py` → `validate_sql()` 前半部分

**职责**: 确保 LLM 生成的 SQL 只访问授权表和执行允许的操作。

**检查项**:
- **操作类型**: 只允许 `SELECT` 和 `WITH`（CTE），拦截 `INSERT/UPDATE/DELETE/DROP` 等
- **表白名单**: 7 张 eSIM 业务表 + 2 张对话表 + 1 张审计表
- **危险关键词**: `LOAD_FILE|INTO OUTFILE|INTO DUMPFILE|CALL|EXEC` 等

**配置**: `app/config/security.yaml` → `sql_security.schema_limiter`

### 3.3 第三层：SQL 校验 (SQL Validator)

**文件**: `app/core/sql_security.py` → `validate_sql()` 后半部分

**职责**: 使用 sqlglot 解析 SQL 为 AST，进行深度语法检查。

**检查项**:
- **SQL 长度**: 不超过 4000 字符
- **JOIN 数量**: 最多 5 个 JOIN（防笛卡尔积）
- **子查询深度**: 最多 3 层子查询
- **危险函数**: `SLEEP|BENCHMARK|LOAD_FILE|USER|DATABASE|CURRENT_USER|CONNECTION_ID`
- **系统变量**: 禁止 `@@version` 等系统变量访问
- **自动 LIMIT**: 如果 SQL 无 LIMIT，自动追加 `LIMIT 1000`

**技术**: `sqlglot` 库解析 SQL 为 AST，遍历 `exp.Join`、`exp.Subquery`、`exp.Func`、`exp.Parameter` 节点

### 3.4 第四层：结果检查 (Post Checker)

**文件**: `app/core/sql_security.py` → `check_result()` + `app/services/masking_service.py`

**职责**: 对查询结果进行行数限制和数据脱敏。

**检查项**:
- **行数限制**: 返回行数不超过 1000 行
- **数据脱敏**: 对手机号、ICCID、IMSI、邮箱等敏感字段自动脱敏
  - 手机号: `138****5678`
  - ICCID: `************1234`（仅显示后 4 位）
  - IMSI: `****`
  - 邮箱: `z***@example.com`
- **管理员豁免**: admin 角色不脱敏

## 4. 集成点

### 4.1 查询流程集成

```
query_service.execute_query()
    │
    ├─ 1. sql_gateway.check_input(question)     ← 第一层
    │     └─ 不通过 → 返回 blocked 结果
    │
    ├─ 2. agent.send_message(augmented_question) ← LLM 生成 SQL
    │
    ├─ 3. CapturingRunSqlTool.execute()          ← 捕获 SQL
    │     └─ sql_gateway.validate_sql(sql)       ← 第二+三层
    │           ├─ 不通过 → 返回 ToolResult(success=False)
    │           └─ 通过 → 执行 SQL（可能被修改：加 LIMIT）
    │
    ├─ 4. masking_service.mask_query_result()    ← 第四层（脱敏）
    │
    └─ 5. audit_service.log_query()              ← 审计日志
```

### 4.2 CapturingRunSqlTool

`app/core/vanna_instance.py` 中的 `CapturingRunSqlTool` 继承 Vanna 的 `RunSqlTool`，在 `execute()` 方法中：
1. 捕获 LLM 生成的 SQL 语句
2. 调用 `sql_gateway.validate_sql()` 校验
3. 被拦截的 SQL 不执行，返回错误给 Agent
4. 通过校验的 SQL（可能被修改）传递给父类执行

### 4.3 安全配置

所有安全策略通过 `app/config/security.yaml` 管理，支持热加载：

```yaml
sql_security:
  input_filter:
    enabled: true
    max_question_length: 1000
    blocked_patterns: ["DROP", "DELETE", "UPDATE", ...]
  schema_limiter:
    allowed_operations: ["SELECT"]
    allowed_tables: ["users", "plans", "orders", ...]
    max_joins: 5
    max_subqueries: 3
  sql_validator:
    max_query_length: 4000
    required_keyword: "SELECT"
  post_checker:
    enabled: true
    result_size_limit: 1000
```

## 5. 附加安全机制

### 5.1 JWT 认证

- **文件**: `app/core/auth.py`
- 演示账号: admin / analyst
- Token 有效期: 60 分钟
- 所有 API 端点支持可选/必需认证

### 5.2 限流中间件

- **文件**: `app/middleware/rate_limit.py`
- 查询接口: 30 次/分钟
- 其他接口: 60 次/分钟
- 滑动窗口算法，内存计数器

### 5.3 审计日志

- **文件**: `app/services/audit_service.py`
- 记录: 用户、问题、SQL、执行状态、耗时、IP
- 写入 `query_audit_log` 表
- 管理员可通过 `/api/v1/admin/audit/logs` 查询

## 6. 安全测试结果

### 6.1 测试覆盖

| 测试场景 | 测试方法 | 结果 |
|---------|---------|------|
| DROP TABLE 注入 | `test_sql_injection_blocked` | 通过 |
| Prompt 注入 | `test_prompt_injection_blocked` | 通过 |
| 空问题拒绝 | `test_invalid_question_empty` | 通过 |
| 超长问题拒绝 | `test_invalid_question_too_long` | 通过 |

### 6.2 集成测试

17 项集成测试全部通过，覆盖：
- 认证流程（登录、me、错误密码）
- 对话 CRUD（创建、列表、详情、删除、404）
- 安全拦截（SQL 注入、Prompt 注入）
- 参数校验（空问题、超长问题）
- 多轮对话（创建→发送消息→验证历史）
- 流式查询端点
- 训练管理 API
- 管理员 API

## 7. 已知限制和待改进项

1. **ONNX Embedding 模型**: ChromaDB 向量检索依赖 all-MiniLM-L6-v2 模型，当前使用关键词检索 fallback
2. **行级安全 (RLS)**: 未实现基于 mvno_id 的行级数据隔离（计划在 Day 16 实现）
3. **只读视图**: 未创建数据库只读视图和专用只读用户
4. **Redis 限流**: 当前使用内存计数器，生产环境应切换 Redis
5. **列级权限**: 未实现基于角色的列级访问控制
6. **SQL 纠错回路**: 未实现自动纠错重试机制
