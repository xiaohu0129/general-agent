"""D2 Broker 空闲会话状态回收测试：TTL 懒扫描 + 默认 ring 容量 2048。"""
from __future__ import annotations

import pytest

from general_agent import events
from general_agent.broker import DEFAULT_RING_SIZE, SWEEP_MIN_SESSIONS, Broker


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.mark.asyncio
async def test_idle_session_without_subscribers_reclaimed():
    clock = FakeClock()
    b = Broker(session_ttl=100.0, time_func=clock)
    await b.distribute("s1", events.turn_start("t1", "tr1"))
    await b.distribute("s1", events.turn_delta("t1", "tr1", "hi"))
    assert "s1" in b._seq

    clock.advance(101.0)
    reclaimed = b.sweep()
    assert reclaimed == 1
    assert "s1" not in b._seq
    assert "s1" not in b._ring
    assert "s1" not in b._last_active

    ev = await b.distribute("s1", events.turn_start("t2", "tr2"))
    assert ev["id"] == "1"  # 新纪元 seq 从 1 重建
    assert b.replay("s1", 0) and len(b.replay("s1", 0)) == 1
    assert b.replay("s1", 5) == []  # 旧游标大于新纪元 -> 空


@pytest.mark.asyncio
async def test_subscribed_session_not_reclaimed_even_past_ttl():
    clock = FakeClock()
    b = Broker(session_ttl=100.0, time_func=clock)
    q = await b.subscribe("s1")
    await b.distribute("s1", events.turn_start("t1", "tr1"))
    clock.advance(1000.0)
    assert b.sweep() == 0
    assert "s1" in b._ring and "s1" in b._seq
    assert b.active_subscribers("s1") == 1
    b.unsubscribe("s1", q)
    clock.advance(101.0)
    assert b.sweep() == 1  # 退订后空闲超时才回收


@pytest.mark.asyncio
async def test_session_within_ttl_not_reclaimed():
    clock = FakeClock()
    b = Broker(session_ttl=100.0, time_func=clock)
    await b.distribute("s1", events.turn_start("t1", "tr1"))
    clock.advance(99.0)
    assert b.sweep() == 0
    assert "s1" in b._seq
    clock.advance(1.0)  # 恰好等于 ttl：MUST NOT 回收（严格大于才超时）
    assert b.sweep() == 0
    assert "s1" in b._seq


@pytest.mark.asyncio
async def test_sweep_keeps_active_session_among_idle_ones():
    clock = FakeClock()
    b = Broker(session_ttl=100.0, time_func=clock)
    await b.distribute("idle1", events.turn_start("t1", "tr1"))
    await b.distribute("idle2", events.turn_start("t2", "tr2"))
    clock.advance(50.0)
    await b.distribute("busy", events.turn_start("t3", "tr3"))
    clock.advance(60.0)  # idle1/2 已 110s，busy 仅 60s
    assert b.sweep() == 2
    assert "idle1" not in b._seq and "idle2" not in b._seq
    assert "busy" in b._seq and len(b.replay("busy", 0)) == 1


@pytest.mark.asyncio
async def test_lazy_sweep_triggers_through_distribute_over_threshold():
    clock = FakeClock()
    b = Broker(session_ttl=100.0, time_func=clock)
    stale_ids = [f"old{i}" for i in range(SWEEP_MIN_SESSIONS + 4)]
    for sid in stale_ids:
        await b.distribute(sid, events.turn_start("t", "tr"))
    clock.advance(200.0)
    # 不直接调 sweep：持续投递触发入口懒扫描（摊销条件为每 n 次入口扫一次）
    for i in range(len(stale_ids) + 1):
        await b.distribute(f"live{i}", events.turn_delta("t", "tr", "x"))
    for sid in stale_ids:
        assert sid not in b._seq


class _IterCountingDict(dict):
    """记录被迭代次数的 dict：len/get/setdefault/pop 均不迭代，仅 __iter__ 计数。"""

    def __init__(self) -> None:
        super().__init__()
        self.iter_count = 0

    def __iter__(self):  # noqa: D401
        self.iter_count += 1
        return super().__iter__()


@pytest.mark.asyncio
async def test_sweep_gating_is_o1_no_union_built_per_event():
    """回归评审 Important-1：distribute/subscribe 入口门控不得每事件构造
    set(_last_active)|set(_subs)（随会话数线性劣化）；只有真正触发的 sweep
    才允许 O(n) 迭代。5000 会话播种下，迭代次数只应来自几何级数的少量扫描。"""
    b = Broker(session_ttl=100.0, time_func=FakeClock())
    counted_last_active = _IterCountingDict()
    counted_subs = _IterCountingDict()
    b._last_active = counted_last_active
    b._subs = counted_subs

    n = 5000
    for i in range(n):
        await b.distribute(f"s{i}", events.turn_start("t", "tr"))

    # 时钟不推进 -> sweep 无回收；扫描在 n=64,128,...,4096 共触发 7 次，
    # 每次每个 dict 至多迭代 1 次。旧实现每事件构造并集 -> 各 ≥ n 次。
    assert counted_last_active.iter_count < 100
    assert counted_subs.iter_count < 100


@pytest.mark.asyncio
async def test_large_session_fanout_distribute_stays_fast():
    """性能级冒烟：5000 会话下持续分发必须快速完成（旧 O(n) 门控会线性劣化）。"""
    import time

    b = Broker(session_ttl=100.0, time_func=FakeClock())
    for i in range(5000):
        await b.distribute(f"s{i}", events.turn_start("t", "tr"))
    start = time.perf_counter()
    for i in range(2000):
        await b.distribute(f"s{i}", events.turn_delta("t", "tr", "x"))
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0  # 宽裕上限，仅防线性劣化回归，不卡绝对值


def test_default_ring_size_is_2048():
    b = Broker()
    assert b.ring_size == DEFAULT_RING_SIZE == 2048


@pytest.mark.asyncio
async def test_default_ring_retains_more_than_256_events():
    b = Broker()
    for i in range(300):
        await b.distribute("s1", events.turn_delta("t1", "tr1", str(i)))
    seqs = [int(ev["id"]) for ev in b.replay("s1", 0)]
    assert len(seqs) == 300 and seqs[0] == 1 and seqs[-1] == 300  # 300 < 2048 全保留；旧默认 256 下会被截断
