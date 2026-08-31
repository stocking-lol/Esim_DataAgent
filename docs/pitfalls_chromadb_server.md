# ChromaDB Server 模式造造踩坑记录

> 背景：为「训练数据定时同步」方案做前置造造时，发现 K8s 多副本部署下 RAG 训练知识库存在数据分叉与丢失风险。
> 适用版本：v0.9.0 之后。相关设计文档：`docs/auto_sync_design.md`。

---

## 0. 先纠正一个概念错误：丢的不是对话记录

排查时最容易搞混的一点，先钉死：

| 数据 | 存储位置 | Pod 重启是否受影响 |
|---|---|---|
| 对话记录 `conversations` / `conversation_messages` | **MySQL**（`app/models/conversation.py`，SQLAlchemy ORM） | ❌ 不受影响，MySQL 有独立 Deployment + PVC |
| 审计日志 `query_audit_log` | **MySQL** | ❌ 不受影响 |
| **RAG 训练知识库**（7 DDL + 15 业务文档 + 24 SQL 示例及向量） | **ChromaDB** | ✅ **会丢** |
| 查询缓存 | Redis（已造造为共享） | ❌ 不受影响 |

**结论：会丢的只有 ChromaDB 里的训练知识库。** 一开始把"对话记录会丢"当动因去排查是错的，方向会偏。

---

## 1. 问题：三层，按严重度排序

`k8s/app-deployment.yaml` 里 `replicas: 2` 但**没有任何 volumeMounts / volumes**，每个 Pod 都在往容器可写层写 `chromadb_data/`。

### ① 数据分叉（最严重，运行时每秒都在发生）

两个 Pod 各持一份独立 ChromaDB。通过管理面板新增一条业务文档，只有当时命中的那个 Pod 知道；下一次请求被 Service 负载均衡到另一个 Pod，就检索不到。

**这比"重启丢数据"隐蔽得多**——不需要等重启，服务正常运行期间结果就已经不一致了。表现为「明明刚训练过，问同样的问题有时生效有时不生效」，极难排查。

### ② 重启即丢

Pod 可写层是临时的。重启后 `chromadb_data/` 重置为空，只剩 `AUTO_INIT_TRAINING` 重建的 46 条基础数据，之后人工加的文档、自动回写的 SQL 示例全部蒸发。

### ③ 反直觉陷阱：挂共享 PVC 也救不了

第一反应通常是"给 app 挂个共享卷不就行了"。**不行。**

PersistentClient 用 **SQLite** 做元数据后端，SQLite 是**单写者模型**。两个进程跨节点写同一个文件时，NFS 的 advisory lock 不可靠，会导致锁冲突甚至文件损坏。而且 ChromaDB 的元数据段（SQLite）和向量段（HNSW + WAL）必须保持一致，跨进程没有任何协调机制。

**所以解法只能是「把写入者收敛成一个进程」，而不是「把文件共享出去」。**

---

## 2. 机制：Server 模式做了什么

核心一句话：**把"只有一个进程能写"这个物理约束，从"靠部署方式碰巧满足"变成"由架构强制保证"。**

```
app Pod（无状态，HttpClient）
        │
        │  HTTP / REST
        ▼
Chroma Server 进程（唯一写入者）
   ├── REST API 层
   ├── 写串行化：单写者锁 + 队列
   ├── 元数据段 chroma.sqlite3
   └── 向量段 HNSW 索引 + WAL
        │
        ▼
     PVC（唯一真实数据副本）
```

- **客户端无状态化**：app Pod 里不再有本地向量数据，可以随意扩缩容、滚动更新、被 OOM kill 后重建。
- **写入串行化上移**：PersistentClient 模式下"只有一个进程写"靠物理隔离（各写各的文件）；Server 模式把这个约束收进 Server 内部，所有请求先排队加锁再落盘，对客户端透明。
- **存储分两段**：元数据在 `chroma.sqlite3`，向量和 HNSW 图在 collection 目录（含 `data_level0.bin`），写前先落 WAL。这两段必须一致——这正是不能共享文件的原因。
- **PVC 用 RWO 即可**：只有 1 个 Chroma Pod 挂载它，不需要 ReadWriteMany（贵，且不是所有 StorageClass 都支持）。

---

## 3. 意外发现：Server 早就部署了，一直在空跑

查 `docker-compose.yml` 时发现：

```yaml
chromadb:
  image: chromadb/chroma:latest
  ports: ["8001:8000"]
  volumes: [chromadb_data:/chroma/chroma]
  environment:
    - IS_PERSISTENT=TRUE
    - PERSIST_DIRECTORY=/chroma/chroma
```

`app/config/settings.py` 也预留了 `CHROMADB_PORT: int = 8001`。但代码走的是 `chromadb.PersistentClient(path=本地目录)`，**从来没连过它**——那个容器目前零数据。

**这个造造不是"新增组件"，而是"把已存在但没接上的组件接上"。** 排查类似问题时，先确认基础设施清单里是不是已经有现成的东西在空转。

---

## 4. 七个坑与解法

> 坑①-④ 是设计阶段预判的，坑⑤-⑦ 是实施时实测发现的。

### 坑① HttpClient 是同步阻塞的，会打爆事件循环 ⚠️ 最易踩

`chromadb.HttpClient` 是**同步**客户端。项目是 FastAPI async，如果在 `async def` 里直接调用，会阻塞整个事件循环，把 2 副本的并发能力打回单线程——**比造造前还慢**。

**实测阻塞点（造造前就存在，只是持久化客户端足够快没暴露）**：

| 位置 | 函数 | 问题 |
|---|---|---|
| `app/services/query_service.py:113` | `async def execute_query` | 同步调 `vanna_manager.retrieve_context(question)` |
| `app/services/query_service.py:447` | `async def execute_query_stream` | 同步调 `vanna_manager.retrieve_context(question)` |

**解法**：给 store 和 manager 各加一层 async 包装，内部用 `asyncio.to_thread` 把同步调用丢进线程池，调用点造成 `await`。不要直接造原同步方法的签名——`train_service` 等模块和既有测试都依赖同步版本。

```python
# 保留同步版本不动
def retrieve_context(self, question: str, max_items: int = 5) -> str: ...

# 新增异步包装
async def aretrieve_context(self, question: str, max_items: int = 5) -> str:
    return await asyncio.to_thread(self.retrieve_context, question, max_items)
```

同理，`initialize()` 里的客户端构造 + `get_or_create_collection`（HTTP 模式下要建 TCP 连接）也是阻塞 I/O，一并包 `to_thread`。

### 坑② Chroma Server 只能单副本

它内部仍是 SQLite + 本地文件，**加副本会重蹈数据分叉的覆辙**。它的高可用不靠横向扩容，靠：

- PVC 持久化（重启数据不丢）
- K8s 自动重启
- 定期快照备份

探针要对准 `/api/v1/heartbeat`（compose 里已经写好了，直接复用）。

### 坑③ 引入了一个新的故障域

原来 ChromaDB 挂了是不可能的（它就在进程里）。现在 Server 挂了，RAG 检索全挂。

**必须做降级**：检索失败时降级为「不带训练上下文的纯 SQL 生成」，而不是整个查询 500。

现有代码里 `vanna_instance.py:417` 已经有 `Training store unavailable → Basic NL2SQL queries will still work` 的降级分支，接上即可，不用重写。

客户端构造也要包 try/except：连不上 Server 时按"训练库不可用"降级，**不能让服务起不来**。

### 坑④ 存量数据迁移

本地 `chromadb_data/` 里现有的 46 条不要试图拷贝 SQLite 文件——HNSW 索引和元数据段的路径、UUID 都对不上。

**最省事的做法是不迁移**：因为内容全部由 `scripts/init_training.py` 生成，直接对新 Server 重跑一次初始化脚本即可（脚本幂等，检测到已有数据会跳过，只有 `--force` 才清空重建）。

---

## 5. 迁移步骤（代码造动量极小）

### 5.1 配置项

`app/config/settings.py`：

```python
CHROMA_CLIENT_MODE: str = "persistent"   # persistent | http，默认保持本地开发行为不变
CHROMADB_HOST: str = "localhost"
CHROMADB_PORT: int = 8001
CHROMA_HTTP_SSL: bool = False
CHROMA_HTTP_TIMEOUT: int = 30
```

默认值必须是 `persistent`——否则本地开发每次都要先起一个 Server。

### 5.2 客户端构造

只有 `app/core/chroma_store.py` 的 `initialize()` 一处：

```python
# 现在
self._client = chromadb.PersistentClient(path=str(persist_path), settings=...)

# 造为按 mode 分支
if settings.CHROMA_CLIENT_MODE == "http":
    self._client = chromadb.HttpClient(
        host=settings.CHROMADB_HOST, port=settings.CHROMADB_PORT,
        ssl=settings.CHROMA_HTTP_SSL,
    )
else:
    self._client = chromadb.PersistentClient(path=str(persist_path), settings=...)
```

HTTP 模式下**不要**创建本地目录（`persist_path.mkdir` 要跳过），否则会留一个空目录误导排查。

**`Collection` 的 API（`add` / `query` / `get` / `delete` / `count`）在两种客户端下完全一致，所以 600 多行 CRUD 代码一行都不用造。**

### 5.3 K8s 清单

新增 `k8s/chroma-pvc.yaml` 与 `k8s/chroma-deployment.yaml`（1 副本 + PVC + ClusterIP Service + heartbeat 探针），并在 app 的 ConfigMap 注入：

```yaml
CHROMA_CLIENT_MODE: "http"
CHROMADB_HOST: "esim-chroma"
CHROMADB_PORT: "8000"     # 集群内端口，注意不是 NodePort 的 8001
```

**注意端口差异**：compose 里对外映射是 `8001:8000`，容器内部监听 8000；K8s 里 Service 的 `port` 也应是 8000。

### 5.4 异步化调用点

见坑①，造 `query_service.py` 的 113 / 447 两行为 `await vanna_manager.aretrieve_context(question)`。

---

## 6. 验证方法

1. **单元**：monkeypatch `chromadb.HttpClient`，断言构造参数正确；断言 `persistent` 分支行为不变；断言 `aretrieve_context` 不阻塞事件循环（并发计时）。
2. **集成**：`docker-compose up -d chromadb` 起真 Server，加两条记录，确认 `/api/v1/train/stats` 计数正确。
3. **多副本一致性**：起两个 app 实例指向同一个 Server，分别调 `/api/v1/train/stats`，数字必须一致。
4. **降级**：停掉 Server，确认服务仍能启动，查询能返回（无训练上下文）而非 500。
5. **回归**：全量 `pytest`（改造前 297 项）不得减少。

---

## 7. 遗留决策

- Chroma Server 是否启用 token 鉴权（生产建议开，通过 Secret 注入）。
- 是否给 Server 配资源限制（HNSW 索引常驻内存，数据量大时要调高 memory limit）。
- 定时同步方案（`docs/auto_sync_design.md`）依赖本造造完成，否则 2 副本会同步出两份不一致的知识库。
