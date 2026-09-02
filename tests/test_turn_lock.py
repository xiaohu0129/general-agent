"""TurnLockRegistry：同 key 互斥 + 引用计数回收（防释放/唤醒竞态）。"""
from __future__ import annotations

import asyncio

from general_agent.turn_lock import TurnLockRegistry


async def test_mutual_exclusion_same_key():
    reg = TurnLockRegistry()
    order: list[str] = []
    gate = asyncio.Event()

    async def worker(name: str, wait_first: bool):
        async with reg.lock("s1"):
            order.append(f"{name}-in")
            if wait_first:
                await asyncio.wait_for(gate.wait(), timeout=5)
            order.append(f"{name}-out")

    t1 = asyncio.create_task(worker("a", True))
    await asyncio.sleep(0.05)  # 确保 a 先拿到锁并卡在 gate
    t2 = asyncio.create_task(worker("b", False))
    await asyncio.sleep(0.05)
    # b 必须等 a 释放：此时 a 未出
    assert order == ["a-in"]
    gate.set()
    await asyncio.gather(t1, t2)
    assert order == ["a-in", "a-out", "b-in", "b-out"]


async def test_different_keys_run_concurrently():
    reg = TurnLockRegistry()
    active = 0
    max_active = 0

    async def worker(key: str):
        nonlocal active, max_active
        async with reg.lock(key):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(worker("x"), worker("y"))
    assert max_active == 2  # 不同 key 并行


async def test_lock_recycled_after_all_done():
    reg = TurnLockRegistry()
    async with reg.lock("s"):
        pass
    assert "s" not in reg._locks
    assert "s" not in reg._refs


async def test_no_recycle_while_waiter_queued():
    """竞态回归：持有者释放瞬间，等待者尚未拿到锁——锁不得被回收，
    且后到者必须与等待者共用同一把锁（互斥不失效）。"""
    reg = TurnLockRegistry()
    same_lock = reg._locks  # noqa: SLF001 (测试内省)

    started: list[str] = []
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with reg.lock("s"):
            started.append("first")
            await release_first.wait()

    async def second():
        # 等 first 持锁后再进入（会阻塞在锁上）
        async with reg.lock("s"):
            started.append("second")
            second_entered.set()

    t1 = asyncio.create_task(first())
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(second())
    await asyncio.sleep(0.05)  # second 已注册引用并排队等待
    assert reg._refs.get("s") == 2  # 持有者 + 等待者都计数
    lock_before = reg._locks.get("s")

    # 第三个进入者在 first 释放前注册：应复用同一把锁
    async def third():
        async with reg.lock("s"):
            started.append("third")

    t3 = asyncio.create_task(third())
    await asyncio.sleep(0.05)
    assert reg._locks.get("s") is lock_before  # 没有被重建

    release_first.set()  # 唤醒 second
    await asyncio.wait_for(second_entered.wait(), timeout=5)
    # second 持锁期间锁仍在，且 third 还在等
    assert reg._locks.get("s") is lock_before
    await asyncio.gather(t1, t2, t3)
    # 全部结束后回收
    assert "s" not in same_lock
    assert started == ["first", "second", "third"]
