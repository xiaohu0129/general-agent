"""消息写入分流（大产物外置 blob）测试：小内容内联、大内容外置 head+ref、上下文不拉 blob。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from general_agent.blob_store import LocalBlobStore
from general_agent.message_store import MessageStore


class FakeCursor:
    def __init__(self, pool):
        self.pool = pool
        self.lastrowid = 1

    async def execute(self, sql, args=None):
        self.pool.execs.append((sql, args))

    async def fetchall(self):
        return list(self.pool.rows)

    async def fetchone(self):
        return self.pool.rows[0] if self.pool.rows else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    def cursor(self, *a, **k):
        return FakeCursor(self.pool)


class FakePool:
    """记录 INSERT 参数、可预置 SELECT 返回行的最小 aiomysql.Pool 替身。"""

    def __init__(self):
        self.execs: list = []
        self.rows: list[dict] = []

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self)


def _store(tmp_path, threshold=32, head_chars=10):
    return MessageStore(
        pool=FakePool(),
        blob_store=LocalBlobStore(root=tmp_path),
        inline_threshold=threshold,
        head_chars=head_chars,
    )


async def test_small_content_inlined(tmp_path):
    ms = _store(tmp_path)
    await ms.append_message("s", "dev", "u1", "sid", "t1", "user", "短消息")
    sql, args = ms._pool.execs[0]
    # args: service,env,user,session,turn,role,content,tool_calls,tool_call_id,ref,size,kind
    assert args[6] == "短消息"
    assert args[9] is None and args[10] is None and args[11] is None


async def test_large_tool_result_offloaded(tmp_path):
    ms = _store(tmp_path)
    big = '{"data": "' + "x" * 200 + '"}'
    await ms.append_message("s", "dev", "u1", "sid", "t1", "tool", big, tool_call_id="c1")
    _, args = ms._pool.execs[0]
    head, ref, size, kind = args[6], args[9], args[10], args[11]
    assert ref is not None and ref.endswith(".json")
    assert size == len(big.encode("utf-8"))
    assert kind == "json"
    assert head != big and "x" * 200 not in head  # 行内只是 head
    assert ms.blob_store.local_path(ref).exists()  # blob 文件已写入
    assert await ms.blob_store.get(ref) == big.encode("utf-8")


async def test_large_assistant_text_offloaded_as_text(tmp_path):
    ms = _store(tmp_path)
    big = "y" * 200
    await ms.append_message("s", "dev", "u1", "sid", "t1", "assistant", big)
    _, args = ms._pool.execs[0]
    assert args[11] == "text"
    assert args[9].endswith(".txt")


async def test_load_context_does_not_fetch_blob(tmp_path):
    ms = _store(tmp_path)
    # 预置一行外置消息：DB 里 content 只是 head，带 content_ref
    ms._pool.rows = [
        {"role": "tool", "content": "head...", "tool_calls": None,
         "tool_call_id": "c1", "content_ref": "u1/sid/t1/abc.json"}
    ]
    rows = await ms.load_messages("s", "dev", "u1", "sid", limit=500)
    assert rows[0]["content"] == "head..."  # 只用行内 head
    # blob 文件并不存在也不报错——证明上下文构建不读取 blob


class _FailingBlobStore:
    """模拟磁盘不可写：put 抛异常。"""

    async def put(self, *a, **k):
        raise OSError("disk full")

    async def get(self, key):
        raise FileNotFoundError(key)

    async def delete(self, key):
        return None

    def local_path(self, key):
        raise FileNotFoundError(key)


async def test_blob_write_failure_falls_back_to_inline(tmp_path):
    # 外置存储不可用时，超大内容回退为内联全量（best-effort），不中断落库
    ms = MessageStore(
        pool=FakePool(),
        blob_store=_FailingBlobStore(),
        inline_threshold=32,
        head_chars=10,
    )
    big = "z" * 200
    await ms.append_message("s", "dev", "u1", "sid", "t1", "tool", big, tool_call_id="c1")
    _, args = ms._pool.execs[0]
    assert args[6] == big  # 回退：行内为完整内容
    assert args[9] is None and args[10] is None and args[11] is None  # 无外置标记
