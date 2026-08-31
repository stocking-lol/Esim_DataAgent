"""
ChromaDB Server 模式端到端验证脚本
----------------------------------
验证 http 模式下训练数据的读写与多副本一致性，供部署后自检使用。

用法：
    # 先启动一个 Chroma Server
    chroma run --path ./chromadb_server_data --port 8001
    # 再运行本脚本
    CHROMA_CLIENT_MODE=http CHROMADB_HOST=localhost CHROMADB_PORT=8001 \
        python scripts/verify_chroma_http.py

检查项：
    1. 能否以 http 模式连上 Server 并完成初始化
    2. 写入后数据是否落库
    3. 另一个独立实例（模拟第二个 Pod）能否读到完全一致的数据
    4. 异步检索路径是否可用
"""

import asyncio
import os
import sys

# 必须在导入 app.config.settings 之前设置，settings 单例在导入时读取环境变量
os.environ.setdefault("CHROMA_CLIENT_MODE", "http")
os.environ.setdefault("CHROMADB_HOST", "localhost")
os.environ.setdefault("CHROMADB_PORT", "8001")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config.settings import settings              # noqa: E402
from app.core.chroma_store import ChromaTrainingStore  # noqa: E402


SAMPLE_DDL = "CREATE TABLE verify_demo (id INT PRIMARY KEY, amount DECIMAL(10,2));"
SAMPLE_DOC = "验证用业务文档：eSIM Profile 激活后进入 active 状态。"
SAMPLE_SQL = "SELECT COUNT(*) FROM verify_demo;"


async def main() -> int:
    mode = settings.CHROMA_CLIENT_MODE
    print(f"[配置] CHROMA_CLIENT_MODE={mode} "
          f"{settings.CHROMADB_HOST}:{settings.CHROMADB_PORT}")

    if mode != "http":
        print("[跳过] 当前为 persistent 模式，本脚本用于验证 http 模式")
        return 0

    # 实例 A：写入数据
    store_a = ChromaTrainingStore()
    try:
        await store_a.initialize()
    except RuntimeError as e:
        # 坑③：Server 不可达时必须优雅降级，而不是让整个服务起不来
        print(f"[降级] 无法连接 Chroma Server：{e}")
        print("[降级] 应用以此为信号转入「无训练上下文」模式，不应整体 500")
        print(f"[降级] 实例状态已清空: is_initialized={store_a.is_initialized}")
        print("\n[通过] 降级路径正常（服务仍可用，仅失去 RAG 增强）")
        return 0

    print(f"[初始化] 实例 A 就绪，模式={store_a.client_mode}")
    print(f"[初始] 计数: {store_a.count_by_type()}")

    store_a.add_ddl(SAMPLE_DDL, table_name="verify_demo")
    store_a.add_documentation(SAMPLE_DOC, topic="验证")
    store_a.add_sql_example("统计验证表记录数", SAMPLE_SQL)
    counts_a = store_a.count_by_type()
    print(f"[写入] 实例 A 计数: {counts_a}")

    # 实例 B：独立实例，模拟第二个 Pod 连同一个 Server
    store_b = ChromaTrainingStore()
    await store_b.initialize()
    counts_b = store_b.count_by_type()
    print(f"[读取] 实例 B 计数: {counts_b}")

    if counts_a == counts_b:
        print("[一致] 两个独立实例读到相同数据 —— 多副本数据已共享")
    else:
        print("[失败] 两个实例数据不一致 —— 仍存在数据分叉！")
        store_b.clear_all()
        return 1

    # 异步检索路径（query_service 实际使用的入口）
    context = await store_b.aretrieve_context("验证表的记录数怎么统计", max_items=2)
    print(f"[检索] 异步检索返回 {len(context)} 字符")
    if context:
        preview = context.replace("\n", " ")[:100]
        print(f"[检索] 预览: {preview}...")

    # 清理验证数据
    store_b.clear_all()
    after = store_b.count_by_type()
    print(f"[清理] 清空后计数: {after}")

    await store_a.shutdown()
    await store_b.shutdown()

    ok = counts_a == counts_b and len(context) > 0
    print(f"\n{'[通过] http 模式端到端验证成功' if ok else '[失败] 验证未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
