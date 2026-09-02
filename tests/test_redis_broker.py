"""RedisBroker 单测（mock Redis，不连真实实例）：seq/ring/distribute/notification/本地 listener。"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from general_agent.redis_broker import RedisBroker


def _mock_redis():
    """mock Redis，覆盖 INCR/LRANGE/RPUSH/LTRIM/EXECUTE/PUBLISH/PUBSUB。"""
    r = MagicMock()
    r.incr = AsyncMock(return_value=1)
    r.lrange = AsyncMock(return_value=[])
    r.pipeline = MagicMock()
    pipe = MagicMock()
    pipe.rpush = MagicMock()
    pipe.ltrim = MagicMock()
    pipe.execute = AsyncMock()
    r.pipeline.return_value = pipe
    r.publish = AsyncMock()
    r.pubsub = MagicMock()
    return r


@pytest.fixture
def broker():
    return RedisBroker(ring_size=64, sub_queue_size=32)


@patch("general_agent.redis_broker.get_redis")
async def test_next_seq(mock_get_redis, broker):
    r = _mock_redis()
    r.incr = AsyncMock(return_value=42)
    mock_get_redis.return_value = r
    seq = await broker.next_seq("s1")
    assert seq == 42
    r.incr.assert_awaited_with("general:agent:seq:s1")


@patch("general_agent.redis_broker.get_redis")
async def test_replay_empty(mock_get_redis, broker):
    r = _mock_redis()
    r.lrange = AsyncMock(return_value=[])
    mock_get_redis.return_value = r
    result = await broker.replay("s1", 0)
    assert result == []


@patch("general_agent.redis_broker.get_redis")
async def test_replay_with_events(mock_get_redis, broker):
    r = _mock_redis()
    ev1 = {"id": "1", "event": "turn_start", "data": json.dumps({"turnId": "t1", "eventSeq": 1})}
    ev2 = {"id": "2", "event": "turn_end", "data": json.dumps({"turnId": "t1", "eventSeq": 2})}
    r.lrange = AsyncMock(return_value=[json.dumps(ev1), json.dumps(ev2)])
    mock_get_redis.return_value = r
    result = await broker.replay("s1", 0)
    assert len(result) == 2
    assert result[0]["id"] == "1"


@patch("general_agent.redis_broker.get_redis")
async def test_replay_after_seq_filters(mock_get_redis, broker):
    r = _mock_redis()
    ev1 = json.dumps({"id": "1", "event": "turn_start", "data": json.dumps({})})
    ev2 = json.dumps({"id": "2", "event": "turn_end", "data": json.dumps({})})
    r.lrange = AsyncMock(return_value=[ev1, ev2])
    mock_get_redis.return_value = r
    result = await broker.replay("s1", 1)
    assert len(result) == 1
    assert result[0]["id"] == "2"


@patch("general_agent.redis_broker.get_redis")
async def test_distribute(mock_get_redis, broker):
    r = _mock_redis()
    r.incr = AsyncMock(return_value=1)
    mock_get_redis.return_value = r
    raw = {"event": "turn_start", "data": json.dumps({"turnId": "t1"})}
    ev = await broker.distribute("s1", raw)
    assert ev["id"] == "1"
    assert "eventSeq" in json.loads(ev["data"])


async def test_subscribe_unsubscribe(broker):
    q1 = await broker.subscribe("s1")
    q2 = await broker.subscribe("s1")
    assert broker.active_subscribers("s1") == 2
    broker.unsubscribe("s1", q1)
    assert broker.active_subscribers("s1") == 1
    broker.unsubscribe("s1", q2)
    assert broker.active_subscribers("s1") == 0


@patch("general_agent.redis_broker.get_redis")
async def test_publish_notification_calls_publish(mock_get_redis, broker):
    r = _mock_redis()
    r.incr = AsyncMock(return_value=5)
    mock_get_redis.return_value = r
    ev = await broker.publish_notification("s1", "J1", "SUCCESS", message="done")
    assert "sessionId" in ev
    assert ev["sessionId"] == "s1"
    r.publish.assert_awaited_once()
    args = r.publish.call_args
    assert "general:agent:notify:s1" in str(args)


async def test_fanout_local(broker):
    q = await broker.subscribe("s1")
    ev = {"event": "notification", "data": json.dumps({"taskId": "J1"}), "id": "1"}
    await broker._fanout_local("s1", ev)
    assert not q.empty()


async def test_listener_dispatches_to_local_subscribers(broker):
    q = await broker.subscribe("s1")
    ev_dict = {"event": "notification", "sessionId": "s1",
               "data": json.dumps({"taskId": "J1", "status": "SUCCESS"}), "id": "10"}
    await broker._fanout_local("s1", ev_dict)
    assert not q.empty()
    item = q.get_nowait()
    assert item["event"] == "notification"
    assert item["sessionId"] == "s1"
