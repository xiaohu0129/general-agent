"""对话会话管理（需登录）：列表/新建/历史消息/重命名/删除。

所有操作按登录 Identity.user（uid）归属校验，越权访问返回 404（不暴露存在性）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..logging_setup import get_logger
from ..security import GovernanceError, Identity, governance_dep

router = APIRouter(prefix="/sessions", tags=["sessions"])
logger = get_logger(__name__)

_ARTIFACT_MEDIA = {"json": "application/json", "text": "text/plain; charset=utf-8"}


class CreateSessionRequest(BaseModel):
    title: str | None = None


class RenameSessionRequest(BaseModel):
    title: str


def _stores(request: Request):
    return request.app.state.chat_sessions, request.app.state.message_store


async def _owned_session(request: Request, identity: Identity, session_id: str) -> dict:
    chat_sessions, _ = _stores(request)
    row = await chat_sessions.get_owned(session_id, identity.user)
    if row is None:
        raise GovernanceError(404, "SESSION_NOT_FOUND", "会话不存在")
    return row


@router.get("")
async def list_sessions(request: Request, identity: Identity = Depends(governance_dep)) -> dict:
    chat_sessions, _ = _stores(request)
    items = await chat_sessions.list_for_user(identity.user)
    return {"sessions": items}


@router.post("")
async def create_session(
    body: CreateSessionRequest, request: Request, identity: Identity = Depends(governance_dep)
) -> dict:
    chat_sessions, _ = _stores(request)
    title = (body.title or "新会话").strip() or "新会话"
    row = await chat_sessions.create(identity.user, identity.service, identity.env, title)
    return row


@router.get("/{session_id}/messages")
async def session_messages(
    session_id: str,
    request: Request,
    before: int | None = None,
    limit: int = 50,
    identity: Identity = Depends(governance_dep),
) -> dict:
    await _owned_session(request, identity, session_id)
    _, message_store = _stores(request)
    return await message_store.load_web_messages(
        identity.service,
        identity.env,
        identity.user,
        session_id,
        before=before,
        limit=limit,
    )


@router.patch("/{session_id}")
async def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    request: Request,
    identity: Identity = Depends(governance_dep),
) -> dict:
    chat_sessions, _ = _stores(request)
    ok = await chat_sessions.rename(session_id, identity.user, body.title)
    if not ok:
        raise GovernanceError(404, "SESSION_NOT_FOUND", "会话不存在或标题为空")
    return {"ok": True}


@router.delete("/{session_id}")
async def delete_session(
    session_id: str, request: Request, identity: Identity = Depends(governance_dep)
) -> dict:
    await _owned_session(request, identity, session_id)  # 归属校验，越权 404
    chat_sessions, message_store = _stores(request)
    # 级联清理外置 blob 产物（best-effort，失败不阻断会话删除）
    blob_store = getattr(request.app.state, "blob_store", None)
    if blob_store is not None:
        try:
            refs = await message_store.list_artifact_refs(
                identity.service, identity.env, identity.user, session_id
            )
        except Exception:
            refs = []
        for ref in refs:
            try:
                await blob_store.delete(ref)
            except Exception:
                logger.warning("artifact_cascade_delete_failed", ref=ref)
    ok = await chat_sessions.delete(session_id, identity.user)
    if not ok:
        raise GovernanceError(404, "SESSION_NOT_FOUND", "会话不存在")
    return {"ok": True}


@router.get("/{session_id}/artifacts/{message_id}")
async def download_artifact(
    session_id: str,
    message_id: int,
    request: Request,
    identity: Identity = Depends(governance_dep),
) -> FileResponse:
    """下载某条外置消息的完整 blob 产物（登录 + 会话归属校验，越权 404）。

    经 FileResponse 流式发送本地 blob 文件，不全量读入内存；Content-Type 按 kind 透传。
    """
    await _owned_session(request, identity, session_id)
    _, message_store = _stores(request)
    try:
        result = await message_store.get_artifact(
            identity.service, identity.env, identity.user, session_id, message_id
        )
    except FileNotFoundError:
        result = None
    if result is None:
        raise GovernanceError(404, "ARTIFACT_NOT_FOUND", "产物不存在")
    path, kind = result
    media = _ARTIFACT_MEDIA.get(kind, "application/octet-stream")
    ext = ".json" if kind == "json" else ".txt" if kind == "text" else ".bin"
    return FileResponse(
        path,
        media_type=media,
        filename=f"artifact-{message_id}{ext}",
    )
