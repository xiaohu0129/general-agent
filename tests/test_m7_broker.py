"""M7 Broker 单元测试：seq/ring/replay/fan-out/背压/通知。"""
from __future__ import annotations

import asyncio

import pytest

from general_agent import events
from general_agent.broker import Broker


@pytest.mark.asyncio
async def test_distribute_assigns_seq_and_rings():
    b = Broker(ring_size=4)
    e1 = await b.distribute("s1", events.turn_start("t1", "tr1"))
    e2 = await b.distribute("s1", events.turn_delta("t1", "tr1", "hi"))
    assert e1["id"] == "1" and e2["id"] == "2"
    assert json_eventSeq(e1) == 1 and json_eventSeq(e2) == 2
    assert len(b.replay("s1", 0)) == 2
    assert len(b.replay("s1", 1)) == 1  # seq>1


@pytest.mark.asyncio
async def test_ring_evicts_oldest():
    b = Broker(ring_size=2)
    for i in range(4):
        await b.distribute("s1", events.turn_delta("t1", "tr1", str(i)))
    replay = b.replay("s1", 0)
    assert [json_eventSeq(e) for e in replay] == [3, 4]  # 只留最近 2 条


@pytest.mark.asyncio
async def test_fanout_multiple_subscribers():
    b = Broker()
    q1 = await b.subscribe("s1")
    q2 = await b.subscribe("s1")
    await b.distribute("s1", events.turn_start("t1", "tr1"))
    e1 = await asyncio.wait_for(q1.get(), timeout=1)
    e2 = await asyncio.wait_for(q2.get(), timeout=1)
    assert e1["id"] == e2["id"] == "1"
    assert b.active_subscribers("s1") == 2
    b.unsubscribe("s1", q1)
    assert b.active_subscribers("s1") == 1


@pytest.mark.asyncio
async def test_backpressure_drop_on_full_queue():
    b = Broker(ring_size=100, sub_queue_size=1)
    q = await b.subscribe("s1")
    # 容量 1：第二条满则丢弃该订阅者，但 ring 全量保留
    await b.distribute("s1", events.turn_delta("t1", "tr1", "a"))
    await b.distribute("s1", events.turn_delta("t1", "tr1", "b"))
    got = await asyncio.wait_for(q.get(), timeout=1)
    assert json_eventSeq(got) == 1
    assert q.empty()
    assert b.drops == 1
    # ring 不受背压影响
    assert len(b.replay("s1", 0)) == 2


@pytest.mark.asyncio
async def test_publish_notification_delivers():
    b = Broker()
    q = await b.subscribe("s1")
    ev = await b.publish_notification("s1", "J1", "SUCCESS", message="done", trace_id="tr1")
    assert ev["event"] == "notification"
    got = await asyncio.wait_for(q.get(), timeout=1)
    data = got["data"] if isinstance(got.get("data"), dict) else __import__("json").loads(got["data"])
    assert data["taskId"] == "J1" and data["status"] == "SUCCESS" and data["eventSeq"] == 1


@pytest.mark.asyncio
async def test_notification_buffered_when_no_subscriber():
    b = Broker()
    ev = await b.publish_notification("s1", "J1", "FAILED")
    assert b.active_subscribers("s1") == 0  # 无订阅者
    # 但事件入 ring，可续传
    replay = b.replay("s1", 0)
    assert len(replay) == 1 and replay[0]["event"] == "notification"


@pytest.mark.asyncio
async def test_next_seq_independent_per_session():
    b = Broker()
    await b.distribute("s1", events.turn_start("t1", "tr1"))
    await b.distribute("s2", events.turn_start("t2", "tr2"))
    await b.distribute("s1", events.turn_delta("t1", "tr1", "x"))
    assert b.replay("s1", 0) and len(b.replay("s2", 0)) == 1


def json_eventSeq(ev):
    import json

    d = ev["data"] if isinstance(ev.get("data"), dict) else json.loads(ev["data"])
    return d.get("eventSeq")
