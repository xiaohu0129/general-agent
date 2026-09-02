import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { streamChat } from "../api/chat";
import * as sessionsApi from "../api/sessions";
import { artifactDownloadUrl } from "../api/sessions";
import type { HistoryMessage, SessionItem } from "../api/types";
import type { ChatMessage, ToolCall } from "../chat/model";
import Composer from "../components/Composer";
import MessageList from "../components/MessageList";
import Sidebar from "../components/Sidebar";
import Welcome from "../components/Welcome";
import { useAuth } from "../state/auth-context";
import "./ChatPage.css";

interface ToolResultMeta {
  content: string;
  messageId?: number | null;
  offloaded: boolean;
  size?: number | null;
  kind?: string | null;
}

function historyToMessages(rows: HistoryMessage[], sessionId: string): ChatMessage[] {
  const toolResults = new Map<string, ToolResultMeta>();
  for (const r of rows) {
    if (r.role === "tool" && r.toolCallId) {
      toolResults.set(r.toolCallId, {
        content: r.content,
        messageId: r.messageId,
        offloaded: !!r.contentRef,
        size: r.contentSize,
        kind: r.contentKind,
      });
    }
  }
  const out: ChatMessage[] = [];
  for (const r of rows) {
    if (r.role === "user") {
      out.push({ id: `h-${r.turnId}-u`, role: "user", content: r.content, toolCalls: [] });
    } else if (r.role === "assistant") {
      const tools: ToolCall[] = (r.toolCalls || []).map((tc) => {
        let args: Record<string, unknown> = {};
        try {
          args = typeof tc.arguments === "string" ? JSON.parse(tc.arguments) : (tc.arguments || {});
        } catch {
          args = {};
        }
        const id = tc.id || "";
        const meta = toolResults.get(id);
        return {
          toolCallId: id,
          toolName: tc.name || "tool",
          args,
          status: "success",
          result: meta ? safeParse(meta.content) : undefined,
          offloaded: meta?.offloaded || false,
          artifactUrl:
            meta?.offloaded && meta.messageId != null
              ? artifactDownloadUrl(sessionId, meta.messageId)
              : undefined,
          artifactSize: meta?.size ?? null,
          artifactKind: meta?.kind ?? null,
        };
      });
      out.push({
        id: `h-${r.turnId}-a`,
        role: "assistant",
        content: r.content,
        toolCalls: tools,
        turnId: r.turnId,
        offloaded: !!r.contentRef,
        artifactUrl:
          r.contentRef && r.messageId != null
            ? artifactDownloadUrl(sessionId, r.messageId)
            : undefined,
        artifactSize: r.contentSize ?? null,
      });
    }
  }
  return out;
}

function safeParse(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

export default function ChatPage() {
  const { user, logout } = useAuth();
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [current, setCurrent] = useState<SessionItem | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [hasEarlier, setHasEarlier] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const earlierCursor = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await sessionsApi.listSessions());
    } catch {
      // 列表加载失败不阻断对话
    }
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  const openSession = useCallback(async (s: SessionItem) => {
    abortRef.current?.abort();
    setCurrent(s);
    earlierCursor.current = null;
    setHasEarlier(false);
    try {
      const page = await sessionsApi.listMessages(s.sessionId);
      earlierCursor.current = page.nextCursor;
      setHasEarlier(page.hasMore);
      setMessages(historyToMessages(page.messages, s.sessionId));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        logout();
      }
      setMessages([]);
    }
  }, [logout]);

  const loadEarlier = useCallback(async () => {
    if (!current || loadingEarlier || earlierCursor.current == null) return;
    setLoadingEarlier(true);
    try {
      const page = await sessionsApi.listMessages(current.sessionId, {
        before: earlierCursor.current,
      });
      earlierCursor.current = page.nextCursor;
      setHasEarlier(page.hasMore);
      const older = historyToMessages(page.messages, current.sessionId);
      setMessages((prev) => [...older, ...prev]);
    } catch {
      // 加载更早失败不阻断当前会话
    } finally {
      setLoadingEarlier(false);
    }
  }, [current, loadingEarlier]);

  const newChat = useCallback(() => {
    abortRef.current?.abort();
    setCurrent(null);
    setMessages([]);
    earlierCursor.current = null;
    setHasEarlier(false);
  }, []);

  const send = useCallback(
    async (text: string) => {
      if (streaming) return;
      const userMsg: ChatMessage = {
        id: `u-${Date.now()}`,
        role: "user",
        content: text,
        toolCalls: [],
      };
      const assistantId = `a-${Date.now()}`;
      const assistantMsg: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        toolCalls: [],
        streaming: true,
      };
      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;
      let createdSessionId: string | null = current?.sessionId || null;

      const patchAssistant = (fn: (m: ChatMessage) => ChatMessage) => {
        setMessages((prev) => prev.map((m) => (m.id === assistantId ? fn(m) : m)));
      };

      try {
        await streamChat(
          text,
          createdSessionId,
          {
            onEvent: (ev) => {
              switch (ev.event) {
                case "turn_start": {
                  if (ev.data.sessionId && !createdSessionId) {
                    createdSessionId = ev.data.sessionId;
                    patchAssistant((m) => ({ ...m, turnId: ev.data.turnId }));
                    const title = text.trim().split("\n")[0].slice(0, 20);
                    setSessions((prev) => [
                      { sessionId: ev.data.sessionId!, title, createdAt: null, updatedAt: null },
                      ...prev,
                    ]);
                    setCurrent({ sessionId: ev.data.sessionId!, title, createdAt: null, updatedAt: null });
                  } else {
                    patchAssistant((m) => ({ ...m, turnId: ev.data.turnId }));
                  }
                  break;
                }
                case "turn_delta":
                  patchAssistant((m) => ({ ...m, content: m.content + ev.data.content }));
                  break;
                case "tool_start":
                  patchAssistant((m) => ({
                    ...m,
                    toolCalls: [
                      ...m.toolCalls,
                      {
                        toolCallId: ev.data.toolCallId,
                        toolName: ev.data.toolName,
                        args: ev.data.args || {},
                        status: "running",
                      },
                    ],
                  }));
                  break;
                case "tool_end":
                  patchAssistant((m) => ({
                    ...m,
                    toolCalls: m.toolCalls.map((t) =>
                      t.toolCallId === ev.data.toolCallId
                        ? {
                            ...t,
                            status: ev.data.status === "error" ? "error" : "success",
                            result: ev.data.result,
                            error: ev.data.error,
                            errorCode: ev.data.errorCode,
                          }
                        : t
                    ),
                  }));
                  break;
                case "turn_end":
                  patchAssistant((m) => ({ ...m, streaming: false }));
                  break;
                case "error":
                  patchAssistant((m) => ({
                    ...m,
                    streaming: false,
                    error: ev.data.message,
                  }));
                  break;
              }
            },
          },
          controller.signal
        );
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          logout();
          return;
        }
        if (controller.signal.aborted) {
          // 用户主动停止：收尾气泡（文本截断保留），把仍在执行的工具标记为已中断，
          // 不报错；后端 producer 仍会跑完落库，刷新后可见完整结果。
          patchAssistant((m) => ({
            ...m,
            streaming: false,
            stopped: true,
            toolCalls: m.toolCalls.map((t) =>
              t.status === "running" ? { ...t, status: "stopped" } : t
            ),
          }));
        } else {
          patchAssistant((m) => ({
            ...m,
            streaming: false,
            error: err instanceof ApiError ? err.message : "网络错误，请稍后重试",
          }));
        }
      } finally {
        // 仅当本次仍是活动流时才清理全局 streaming 标志；切换会话/停止后另起新流时，
        // 旧流的收尾不得误清新流状态。
        if (abortRef.current === controller) {
          abortRef.current = null;
          setStreaming(false);
        }
        // 无论成功/失败/停止都刷新会话列表：停止时后端 producer 仍会建好会话，需补到侧边栏
        refreshSessions();
      }
    },
    [streaming, current, logout, refreshSessions]
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const rename = useCallback(
    async (s: SessionItem, title: string) => {
      await sessionsApi.renameSession(s.sessionId, title);
      setSessions((prev) => prev.map((x) => (x.sessionId === s.sessionId ? { ...x, title } : x)));
      if (current?.sessionId === s.sessionId) {
        setCurrent({ ...s, title });
      }
    },
    [current]
  );

  const remove = useCallback(
    async (s: SessionItem) => {
      await sessionsApi.deleteSession(s.sessionId);
      if (current?.sessionId === s.sessionId) {
        newChat();
      }
      refreshSessions();
    },
    [current, newChat, refreshSessions]
  );

  return (
    <div className="chat-layout">
      <Sidebar
        sessions={sessions}
        currentId={current?.sessionId || null}
        username={user?.username || ""}
        onNew={newChat}
        onSelect={openSession}
        onRename={rename}
        onDelete={remove}
        onLogout={logout}
      />
      <main className="chat-main">
        <div className="chat-scroll">
          <div className="chat-content">
            {messages.length === 0 && !streaming ? (
              <Welcome username={user?.username || ""} onPick={send} />
            ) : (
              <>
                {hasEarlier && (
                  <button
                    className="load-earlier"
                    onClick={loadEarlier}
                    disabled={loadingEarlier}
                  >
                    {loadingEarlier ? "加载中…" : "加载更早的消息"}
                  </button>
                )}
                <MessageList messages={messages} />
              </>
            )}
          </div>
        </div>
        <div className="chat-composer">
          <div className="chat-content">
            <Composer
              disabled={false}
              streaming={streaming}
              onSend={send}
              onStop={stop}
            />
          </div>
        </div>
      </main>
    </div>
  );
}
