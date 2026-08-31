# eSIM NL2SQL Platform - Kubernetes 部署说明

将 Docker Compose 部署迁移为 Kubernetes 清单，支持多副本水平扩展。

## 快速开始

```bash
# 1. 构建应用镜像
docker build -t esim-nl2sql-platform:latest .

# 2. 创建命名空间与配置
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml

# 3. 创建敏感配置（使用真实值！）
kubectl -n esim-nl2sql create secret generic esim-app-secret \
  --from-literal=LLM_API_KEY='<真实 Key>' \
  --from-literal=JWT_SECRET_KEY='<随机 32+ 字符>' \
  --from-literal=DATABASE_PASSWORD='<数据库密码>'
# 或 kubectl apply -f k8s/secret.example.yaml（仅演示结构，勿用占位值）

# 4. 部署基础设施（MySQL + Redis + Chroma Server）
kubectl apply -f k8s/mysql-deployment.yaml
kubectl apply -f k8s/redis-deployment.yaml
kubectl apply -f k8s/chroma-deployment.yaml

# 5. 部署应用（2 副本，NodePort 30800）
kubectl apply -f k8s/app-deployment.yaml

# 6. 验证
kubectl -n esim-nl2sql get pods
kubectl -n esim-nl2sql get svc
# 访问: http://<节点IP>:30800
```

## 应用配置说明（ConfigMap / Secret）

| 配置 | 来源 | 说明 |
|---|---|---|
| LLM_MODEL / LLM_BASE_URL | ConfigMap | 模型与地址 |
| LLM_API_KEY | **Secret** | 大模型密钥，勿入镜像/ConfigMap |
| JWT_SECRET_KEY | **Secret** | 签名密钥 |
| DATABASE_PASSWORD | **Secret** | 数据库密码 |
| QUERY_CACHE_BACKEND=redis | ConfigMap | 缓存后端切换为 Redis（跨副本共享） |
| REDIS_URL=redis://esim-redis:6379/0 | ConfigMap | 走 K8s Service 域名 |
| CHROMA_CLIENT_MODE=http | ConfigMap | 连独立 Chroma Server，多副本下必选（默认 persistent 仅供本地单副本） |
| CHROMADB_HOST=esim-chroma | ConfigMap | Chroma Server 的 Service 域名，集群内端口 8000 |

## 高可用设计（与项目特性呼应）

- **多副本 + 就绪探针**：replicas=2，readinessProbe 保证流量只打到可服务副本；
  startupProbe 为应用启动（LLM 客户端建连等）留启动时间
- **跨副本缓存一致性**：QUERY_CACHE_BACKEND=redis，多个 Pod 共享同一 Redis，
  避免内存缓存不一致——这正是 Redis 后端存在的意义
- **训练知识库共享**：CHROMA_CLIENT_MODE=http，两个 Pod 通过 HttpClient 连同一个
  Chroma Server（1 副本 + PVC），消除数据分叉。Server 本身**不能**跟着扩副本
  （内部是 SQLite + 本地 HNSW 索引文件），它的高可用靠 PVC 持久化 + 快速重启
- **配置与密钥分离**：非敏感进 ConfigMap、敏感进 Secret（RBAC 可精细控制读取权限）
- **水平扩展**：`kubectl scale deploy esim-app -n esim-nl2sql --replicas=4`

## 依赖说明

- ChromaDB：已提供独立部署 `k8s/chroma-deployment.yaml`（1 副本 + 2Gi PVC + 心跳探针）。
  必须单副本，无法像 app 那样水平扩展。数据备份靠定期快照 PVC；HNSW 索引常驻内存，
  训练数据量增长后需上调 memory limit。详见 `docs/pitfalls_chromadb_server.md`。
- 本地开发：默认 `CHROMA_CLIENT_MODE=persistent`（进程内本地目录），无需起 Server；
  如需验证 http 模式，`docker-compose up -d chromadb` 后设 `CHROMA_CLIENT_MODE=http`。
- 生产建议：MySQL 使用 StatefulSet + Headless Service 以保障稳定网络标识。
