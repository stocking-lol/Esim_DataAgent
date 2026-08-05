# eSIM NL2SQL Platform

> 企业级自然语言数据查询平台 — 基于 FastAPI + Vanna 2.0 + ChromaDB + DeepSeek-V3

## 技术栈

- **后端**: FastAPI + Vanna 2.0 + SQLAlchemy
- **向量存储**: ChromaDB
- **数据库**: MySQL 8.0
- **LLM**: DeepSeek-V3 (OpenAI 兼容接口)
- **认证**: JWT
- **监控**: Prometheus
- **容器化**: Docker + docker-compose

## 快速开始

### 1. 克隆项目

```bash
git clone <repo-url>
cd esim-nl2sql-platform
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，填写 LLM_API_KEY 等配置
```

### 3. 创建 Conda 环境

```bash
conda create -n esim-nl2sql python=3.12 -y
conda activate esim-nl2sql
pip install -r requirements.txt
```

### 4. 启动基础设施

```bash
docker-compose up -d mysql chromadb
```

### 5. 启动应用

```bash
python -m uvicorn app.main:app --reload
```

### 6. 访问

- API 文档: http://localhost:8000/docs
- 健康检查: http://localhost:8000/health

## 项目结构

```
esim-nl2sql-platform/
├── app/
│   ├── main.py                 # FastAPI 应用入口
│   ├── config/                 # 配置管理
│   │   ├── settings.py         # pydantic-settings 配置
│   │   └── security.yaml       # 安全策略
│   ├── api/v1/                 # API 路由
│   ├── core/                   # 核心模块（Vanna, LLM, Security）
│   ├── models/                 # 数据库模型
│   ├── services/               # 业务服务
│   ├── middleware/             # 中间件（审计、限流、脱敏）
│   └── utils/                  # 工具函数
├── frontend/                   # 前端项目
├── tests/                      # 测试
├── scripts/                    # 脚本（建表、种子数据、评估）
├── docker-compose.yml          # Docker 编排
└── requirements.txt            # Python 依赖
```

## API 模块

| 模块 | 路径 | 说明 |
|------|------|------|
| Auth | `/api/v1/auth` | 认证与授权 |
| Query | `/api/v1/query` | NL2SQL 查询 |
| Train | `/api/v1/train` | RAG 训练管理 |
| Conversation | `/api/v1/conversation` | 多轮对话 |
| Admin | `/api/v1/admin` | 用户管理 |

## 错误码

| 错误码 | 说明 |
|--------|------|
| 200 | 成功 |
| 400 | 请求参数错误 |
| 401 | 未认证 |
| 403 | 无权限 |
| 404 | 资源不存在 |
| 429 | 请求过于频繁 |
| 500 | 服务器内部错误 |
| 1001 | SQL安全拦截 |
| 1002 | SQL执行错误 |
| 1003 | LLM服务异常 |
| 1004 | 数据库连接异常 |

## License

MIT
