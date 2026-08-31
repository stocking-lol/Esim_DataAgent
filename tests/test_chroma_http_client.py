"""
ChromaDB 客户端双模式（persistent / http）单元测试
-------------------------------------------------
验证 K8s 多副本改造的正确性，全部用 mock 隔离，不依赖真实 Chroma Server。

对应 docs/pitfalls_chromadb_server.md：
  - persistent 模式在多副本下会导致每个 Pod 各持一份数据并永久分叉
  - http 模式必须传对 host/port、立即探活，且不能创建本地目录
  - 异步包装必须真正把阻塞 I/O 移出事件循环（坑①）
  - 初始化失败必须留下干净状态，供上层降级为「无训练上下文」（坑③）
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core import chroma_store as cs_mod
from app.core.chroma_store import ChromaTrainingStore


def _make_client_mock():
    """构造一个最小可用的 Chroma 客户端 mock"""
    client = MagicMock(name="chroma_client")
    collection = MagicMock(name="collection")
    collection.count.return_value = 0
    client.get_or_create_collection.return_value = collection
    client.heartbeat.return_value = {"nanosecond heartbeat": 1}
    return client


@pytest.fixture
def store(monkeypatch, tmp_path):
    """未初始化的 store 实例：embedding 函数与本地目录都被隔离"""
    monkeypatch.setattr(
        ChromaTrainingStore,
        "_create_embedding_function",
        staticmethod(lambda: MagicMock(name="embedding_fn")),
    )
    return ChromaTrainingStore(persist_dir=str(tmp_path / "chromadb_data"))


# ============================================================
# persistent 模式（默认，本地开发 / 单副本）
# ============================================================

def test_persistent_mode_uses_local_directory(store, monkeypatch):
    captured = {}

    def fake_persistent(path=None, settings=None):
        captured["path"] = path
        return _make_client_mock()

    monkeypatch.setattr(cs_mod.chromadb, "PersistentClient", fake_persistent)

    store._connect("persistent")

    assert captured["path"] == store._persist_dir
    assert Path(store._persist_dir).is_dir(), "persistent 模式应创建本地目录"
    assert store.client_mode == "persistent"


def test_default_mode_is_persistent(store, monkeypatch):
    """默认必须保持原有本地行为，否则本地开发每次都要先起一个 Server"""
    assert cs_mod.settings.CHROMA_CLIENT_MODE == "persistent"

    monkeypatch.setattr(
        cs_mod.chromadb, "PersistentClient",
        lambda path=None, settings=None: _make_client_mock(),
    )

    store._connect(cs_mod.settings.CHROMA_CLIENT_MODE)

    assert store.client_mode == "persistent"


def test_sync_api_is_preserved(store):
    """train_service 与初始化脚本依赖同步版本，改造后必须仍然存在"""
    for name in ("retrieve_context", "search", "get_all", "count_by_type",
                 "add_ddl", "add_documentation", "add_sql_example"):
        assert callable(getattr(store, name, None)), f"同步方法 {name} 缺失"


# ============================================================
# http 模式（K8s 多副本）
# ============================================================

def test_http_mode_passes_host_port_ssl(store, monkeypatch):
    captured = {}

    def fake_http(host=None, port=None, ssl=None, **kwargs):
        captured.update(host=host, port=port, ssl=ssl)
        return _make_client_mock()

    monkeypatch.setattr(cs_mod.chromadb, "HttpClient", fake_http)
    monkeypatch.setattr(cs_mod.settings, "CHROMADB_HOST", "esim-chroma")
    monkeypatch.setattr(cs_mod.settings, "CHROMADB_PORT", 8000)
    monkeypatch.setattr(cs_mod.settings, "CHROMA_HTTP_SSL", False)

    store._connect("http")

    assert captured == {"host": "esim-chroma", "port": 8000, "ssl": False}
    assert store.client_mode == "http"


def test_http_mode_probes_heartbeat_immediately(store, monkeypatch):
    """必须立即探活：否则会带着一个连不上的客户端启动，故障推迟到首次查询才暴露"""
    client = _make_client_mock()
    monkeypatch.setattr(cs_mod.chromadb, "HttpClient", lambda **kw: client)

    store._connect("http")

    client.heartbeat.assert_called_once()


def test_http_mode_does_not_create_local_directory(store, monkeypatch):
    monkeypatch.setattr(
        cs_mod.chromadb, "HttpClient", lambda **kw: _make_client_mock()
    )

    store._connect("http")

    assert not Path(store._persist_dir).exists(), (
        "http 模式不应创建本地目录，否则会留下空目录误导排查"
    )


# ============================================================
# 初始化与降级
# ============================================================

async def test_invalid_mode_raises_runtime_error(store, monkeypatch):
    monkeypatch.setattr(cs_mod.settings, "CHROMA_CLIENT_MODE", "bogus")

    with pytest.raises(RuntimeError, match="不支持的 CHROMA_CLIENT_MODE"):
        await store.initialize()


async def test_init_failure_leaves_clean_state_for_degradation(store, monkeypatch):
    """Server 不可达时必须清空状态，让上层降级为「无训练上下文」而非半可用状态"""
    monkeypatch.setattr(cs_mod.settings, "CHROMA_CLIENT_MODE", "http")

    def refuse(**kwargs):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(cs_mod.chromadb, "HttpClient", refuse)

    with pytest.raises(RuntimeError, match="ChromaDB 初始化失败"):
        await store.initialize()

    assert store.is_initialized is False
    assert store._client is None
    assert store._collections == {}


async def test_initialize_succeeds_in_http_mode(store, monkeypatch):
    monkeypatch.setattr(cs_mod.settings, "CHROMA_CLIENT_MODE", "http")
    monkeypatch.setattr(
        cs_mod.chromadb, "HttpClient", lambda **kw: _make_client_mock()
    )

    await store.initialize()

    assert store.is_initialized is True
    assert store.client_mode == "http"
    assert set(store._collections) == set(ChromaTrainingStore.COLLECTION_NAMES)


# ============================================================
# 异步包装（坑①：不得阻塞事件循环）
# ============================================================

async def test_async_wrappers_do_not_block_event_loop(store, monkeypatch):
    """异步包装必须把阻塞 I/O 移出事件循环

    若实现错误（在事件循环里直接同步调用），4 个任务会串行执行约 0.8s；
    正确实现走线程池，4 个任务并行，总耗时约 0.2s。
    """
    def blocking_retrieve(question, max_items=5):
        time.sleep(0.2)
        return "ctx"

    monkeypatch.setattr(store, "retrieve_context", blocking_retrieve)

    async def probe():
        await asyncio.sleep(0.2)
        return "ok"

    start = time.perf_counter()
    results = await asyncio.gather(
        store.aretrieve_context("一个问题"), probe(), probe(), probe()
    )
    elapsed = time.perf_counter() - start

    assert results == ["ctx", "ok", "ok", "ok"]
    assert elapsed < 0.5, (
        f"疑似阻塞事件循环：4 个任务耗时 {elapsed:.2f}s（并行应约 0.2s）"
    )


async def test_async_wrappers_delegate_to_sync_versions(store, monkeypatch):
    """异步包装必须与同步方法行为一致（同参数、同返回值）"""
    monkeypatch.setattr(store, "retrieve_context", lambda q, max_items=5: f"{q}:{max_items}")
    monkeypatch.setattr(store, "count_by_type", lambda: {"ddl": 7})
    monkeypatch.setattr(store, "get_all", lambda record_type=None: ["rec"])

    assert await store.aretrieve_context("q", 3) == "q:3"
    assert await store.aretrieve_context("q") == "q:5"
    assert await store.acount_by_type() == {"ddl": 7}
    assert await store.aget_all() == ["rec"]
