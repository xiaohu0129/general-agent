"""POST /internal/notify：异步任务完成通知接入。

业务系统的异步任务完成 -> 回调 -> broker.publish_notification -> fan-out 订阅者；
通知经 GET /stream 下发，并写入 ring buffer 供断线续传。
传输方式（MQ/HTTP webhook 等）由业务侧决定。
服务间鉴权 service_auth_dep（api_key）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..logging_setup import get_logger
from ..security import service_auth_dep

router = APIRouter(tags=["notify"])
logger = get_logger(__name__)


class NotifyBody(BaseModel):
    sessionId: str
    taskId: str
    status: str
    message: str | None = None
    traceId: str | None = None


@router.post("/internal/notify", dependencies=[Depends(service_auth_dep)])
async def notify(body: NotifyBody, request: Request):
    broker = request.app.state.broker
    ev = await broker.publish_notification(
        body.sessionId,
        body.taskId,
        body.status,
        message=body.message,
        trace_id=body.traceId or "",
    )
    logger.info(
        "notify_received",
        sessionId=body.sessionId,
        taskId=body.taskId,
        status=body.status,
        eventSeq=ev["id"],
        deliveredTo=broker.active_subscribers(body.sessionId),
    )
    return {"status": "accepted", "eventSeq": int(ev["id"])}
