"""BlobStore（大产物外置存储）测试：本地相对路径实现、往返、路径安全、删除。"""
from __future__ import annotations

import pytest

from general_agent.blob_store import LocalBlobStore


async def test_put_get_roundtrip(tmp_path):
    store = LocalBlobStore(root=tmp_path)
    key = await store.put(("uid1", "sid1", "turn1"), b'{"a":1}', ext=".json")
    assert key.startswith("uid1/sid1/turn1/")
    assert key.endswith(".json")
    assert await store.get(key) == b'{"a":1}'
    p = store.local_path(key)
    assert p.exists()
    assert tmp_path in p.parents


async def test_unsafe_scope_rejected(tmp_path):
    store = LocalBlobStore(root=tmp_path)
    with pytest.raises(ValueError):
        await store.put(("..",), b"x", ext=".txt")
    with pytest.raises(ValueError):
        await store.put(("a/b",), b"x", ext=".txt")
    with pytest.raises(ValueError):
        await store.get("../escape.txt")


async def test_delete_and_missing(tmp_path):
    store = LocalBlobStore(root=tmp_path)
    key = await store.put(("u", "s", "t"), b"data", ext=".txt")
    assert store.local_path(key).exists()
    await store.delete(key)
    assert not store.local_path(key).exists()
    with pytest.raises(FileNotFoundError):
        await store.get(key)
    with pytest.raises(FileNotFoundError):
        await store.get("u/s/t/nope.txt")


async def test_distinct_keys(tmp_path):
    store = LocalBlobStore(root=tmp_path)
    k1 = await store.put(("u", "s", "t"), b"a", ext=".json")
    k2 = await store.put(("u", "s", "t"), b"b", ext=".json")
    assert k1 != k2
