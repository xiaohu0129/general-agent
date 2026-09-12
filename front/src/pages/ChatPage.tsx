import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { streamChat } from "../api/chat";
import { createStreamConnection } from "../api/stream";
import * as sessionsApi from "../api/sessions";
import type { ChatEvent, SessionItem } from "../api/types";
import type { ChatMessage } from "../chat/model";
import { applyEvent } from "../chat/applyEvent";
import {
  createEventGate,
  type EventChannel,
  type EventGate,
} from "../chat/eventGate";
import { historyToMessages } from "../chat/history";
import Composer from "../components/Composer";
import MessageList from "../components/MessageList";
import Sidebar from "../components/Sidebar";
import Welcome from "../components/Welcome";
import { useAuth } from "../state/auth-context";
import "./ChatPage.css";

// 缺口重拉成功提示的停留时长：到时自动消失；连续重拉会重置计时
const GAP_NOTICE_TTL_MS = 3000;

// /stream 连接承载组件：随会话挂载/卸载（条件渲染），sid 变化时 cleanup 后重建。
// 独立成组件而非内联 effect：null 会话期根本不挂载；StrictMode 双挂载也能真实
// 演练 setup→cleanup→setup，保证不会泄漏 EventSource 连接。
function SessionStream({
  sid,
  dispatch,
}: {
  sid: string;
  dispatch: (sid: string, ev: ChatEvent, channel: EventChannel) => void;
}) {
  useEffect(() => {
    const conn = createStreamConnection(sid, {
      onEvent: (ev) => dispatch(sid, ev, "stream"),
      onConnectionError: (fatal) => {
        // 降级仅记日志：不改 messages、不阻断 POST 即时流（无 toast 基建）
        console.warn(
          fatal
            ? `[stream] 连接已关闭，降级为仅即时流：${sid}`
            : `[stream] 连接中断，浏览器将自动重连：${sid}`,
        );
      },
    });
    return () => conn.close();
  }, [sid, dispatch]);
  return null;
}

export default function ChatPage() {
  const { user, logout } = useAuth();
  // logout 引用不稳定（如测试替身每次渲染重建）会经 useCallback 依赖链拖垮
  // dispatch 的稳定性、导致 SessionStream 反复重连：用 ref 镜像断开依赖。
  const logoutRef = useRef(logout);
  logoutRef.current = logout;
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [current, setCurrent] = useState<SessionItem | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [hasEarlier, setHasEarlier] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [gapNotice, setGapNotice] = useState<string | null>(null);
  const earlierCursor = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // 当前活动轮乐观气泡 id：双通道统一入口用它绑定 turnId
  const activeAssistantRef = useRef<string | null>(null);
  // 本次 send 的 live 用户/助手气泡 id 对：turn_start 时双双绑定 turnId，
  // 缺口重拉合并才能按 turnId 把已落库的 live 一对气泡整体替换、避免用户消息重复
  const activePairRef = useRef<{ userId: string; assistantId: string } | null>(null);
  // streaming 的 ref 镜像：事件回调（非 React 渲染闭包）里判断活动轮是否收尾
  const streamingRef = useRef(false);
  // 活动轮中检测到 seq 缺口：挂账到 turn_end/error 后再重拉
  const pendingGapRef = useRef<string | null>(null);
  // 重拉幂等守卫：连续多个缺口事件/挂账收尾只允许一次在途请求
  const gapReloadingRef = useRef(false);
  // 缺口提示自动消失计时器：showGapNotice 重置式设置，切会话/卸载时清理
  const gapNoticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 当前会话 id 的 ref 镜像：异步回调里丢弃已切走会话的陈旧事件/响应
  const currentSidRef = useRef<string | null>(null);
  // 事件去重门按 sessionId 持有：切会话不共享游标
  const gatesRef = useRef<Map<string, EventGate>>(new Map());

  const gateFor = useCallback((sid: string): EventGate => {
    let gate = gatesRef.current.get(sid);
    if (!gate) {
      gate = createEventGate();
      gatesRef.current.set(sid, gate);
    }
    return gate;
  }, []);

  const clearGapNoticeTimer = useCallback(() => {
    if (gapNoticeTimerRef.current !== null) {
      clearTimeout(gapNoticeTimerRef.current);
      gapNoticeTimerRef.current = null;
    }
  }, []);

  // 重置式展示缺口提示：先清旧 timer 再排新 timer，连续重拉不叠加计时；
  // TTL 到点自动清空提示。切会话/卸载由 clearGapNoticeTimer 收口。
  const showGapNotice = useCallback(
    (text: string) => {
      clearGapNoticeTimer();
      setGapNotice(text);
      gapNoticeTimerRef.current = setTimeout(() => {
        gapNoticeTimerRef.current = null;
        setGapNotice(null);
      }, GAP_NOTICE_TTL_MS);
    },
    [clearGapNoticeTimer],
  );

  // 卸载时清 timer，避免卸载后 setState
  useEffect(() => clearGapNoticeTimer, [clearGapNoticeTimer]);

  // 静默历史重拉合并（缺口恢复）：独立于 openSession——不 abort 即时流、不整体
  // 替换 messages、不 setCurrent、不清输入框。历史按 turnId 替换/转正 live 气泡，
  // 仍在跑（历史尚无该 turnId）的 live 气泡保留；selected 以历史为权威。
  const runGapReload = useCallback(
    async (sid: string) => {
      if (gapReloadingRef.current) return;
      if (sid !== currentSidRef.current) return;
      gapReloadingRef.current = true;
      try {
        // 循环收口：每轮"取账→清零→拉取合并"，飞行结束时若仍有同 sid 挂账
        // （飞行期间活动轮缺口+收尾再次挂账），紧接着再执行一轮；单次拉取失败
        // 即退出循环（防热循环），静默语义不变。
        do {
          if (pendingGapRef.current === sid) {
            pendingGapRef.current = null;
          }
          const page = await sessionsApi.listMessages(sid);
          // 请求期间切走会话：丢弃结果，新会话的 openSession/newChat 已重置状态
          if (sid !== currentSidRef.current) return;
          earlierCursor.current = page.nextCursor;
          setHasEarlier(page.hasMore);
          const restored = historyToMessages(page.messages, sid);
          const restoredTurnIds = new Set(
            restored
              .filter((m) => m.role === "assistant" && m.turnId !== undefined)
              .map((m) => m.turnId as string),
          );
          setMessages((prev) => [
            ...restored,
            ...prev.filter(
              (m) =>
                m.source === "live" &&
                (m.turnId === undefined || !restoredTurnIds.has(m.turnId)),
            ),
          ]);
          showGapNotice("事件已过期，已刷新");
        } while (pendingGapRef.current === sid);
      } catch (err) {
        if (sid !== currentSidRef.current) return;
        if (err instanceof ApiError && err.status === 401) {
          logoutRef.current();
        }
        // 其他失败静默：不阻断即时流、不清消息、不弹通知；挂账随本轮取账已清零
      } finally {
        gapReloadingRef.current = false;
      }
    },
    [showGapNotice],
  );

  // 缺口调度：活动轮中只挂账（不动消息、不发请求），收尾后由 dispatchEvent 补拉；
  // 空闲立即重拉。非当前会话的陈旧事件忽略。
  const scheduleGapReload = useCallback(
    (sid: string) => {
      if (sid !== currentSidRef.current) return;
      if (streamingRef.current) {
        pendingGapRef.current = sid;
        return;
      }
      void runGapReload(sid);
    },
    [runGapReload],
  );

  // 双通道（post/stream）事件统一入口：先过会话级 gate 去重+缺口检测，再交纯函数 applyEvent
  const dispatchEvent = useCallback(
    (sid: string, ev: ChatEvent, channel: EventChannel) => {
      const r = gateFor(sid).ingest(ev, channel);
      if (r.apply) {
        setMessages((prev) => {
          let next = applyEvent(
            prev,
            { activeAssistantId: activeAssistantRef.current ?? "" },
            ev,
          );
          // turn_start 副作用：除 applyEvent 绑定助手气泡外，把同一 turnId 写到
          // 本次 send 的 live 用户气泡，供重拉合并按 turnId 成对替换。
          if (ev.event === "turn_start") {
            const pair = activePairRef.current;
            const userNeedsBind =
              pair !== null &&
              next.some(
                (m) =>
                  m.id === pair.userId &&
                  m.role === "user" &&
                  m.turnId === undefined &&
                  m.source !== "restored",
              );
            if (pair && userNeedsBind) {
              next = next.map((m) =>
                m.id === pair.userId ? { ...m, turnId: ev.data.turnId } : m,
              );
            }
          }
          return next;
        });
      }
      if (r.gap) {
        scheduleGapReload(sid);
      }
      // 活动轮中挂账的缺口：turn_end/error 应用之后立即补一次静默重拉
      // （正常轮 turn_end 前已落库；error 轮不保证落库，接受历史缺该轮）。
      if (
        (ev.event === "turn_end" || ev.event === "error") &&
        pendingGapRef.current === sid
      ) {
        void runGapReload(sid);
      }
    },
    [gateFor, scheduleGapReload, runGapReload],
  );

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

  // GET /stream 持久通道：current=null 时 SessionStream 不挂载（不建连）；
  // turn_start 在新会话 setCurrent 后补挂载；切换会话 prop 变化触发 cleanup 重建。
  const streamSid = current?.sessionId;

  // current 的 ref 镜像：dispatch/重拉等异步回调据此丢弃已切走会话的陈旧事件
  useEffect(() => {
    currentSidRef.current = streamSid ?? null;
  }, [streamSid]);

  const openSession = useCallback(async (s: SessionItem) => {
    abortRef.current?.abort();
    gatesRef.current.clear();
    pendingGapRef.current = null;
    gapReloadingRef.current = false;
    clearGapNoticeTimer();
    setGapNotice(null);
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
  }, [logout, clearGapNoticeTimer]);

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
    gatesRef.current.clear();
    pendingGapRef.current = null;
    gapReloadingRef.current = false;
    clearGapNoticeTimer();
    setGapNotice(null);
    setCurrent(null);
    setMessages([]);
    earlierCursor.current = null;
    setHasEarlier(false);
  }, [clearGapNoticeTimer]);

  const send = useCallback(
    async (text: string, clarifySelection?: string) => {
      if (streaming) return;
      const userMsg: ChatMessage = {
        id: `u-${Date.now()}`,
        role: "user",
        content: text,
        toolCalls: [],
        source: "live",
      };
      const assistantId = `a-${Date.now()}`;
      const assistantMsg: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        toolCalls: [],
        streaming: true,
        source: "live",
      };
      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setStreaming(true);
      streamingRef.current = true;
      activeAssistantRef.current = assistantId;
      activePairRef.current = { userId: userMsg.id, assistantId };
      // 新一轮开始：清掉本 sid 上一轮残留的缺口挂账（挂账只从属于当前轮）
      const startSid = current?.sessionId ?? null;
      if (startSid && pendingGapRef.current === startSid) {
        pendingGapRef.current = null;
      }

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
              // 建会话/侧边栏插入/setCurrent 是 React 副作用，留在回调；
              // createdSessionId 为本次 send 的局部变量，先于事件推进更新以保持时序。
              if (ev.event === "turn_start" && ev.data.sessionId && !createdSessionId) {
                createdSessionId = ev.data.sessionId;
                currentSidRef.current = createdSessionId;
                const title = text.trim().split("\n")[0].slice(0, 20);
                setSessions((prev) => [
                  { sessionId: ev.data.sessionId!, title, createdAt: null, updatedAt: null },
                  ...prev,
                ]);
                setCurrent({
                  sessionId: ev.data.sessionId!,
                  title,
                  createdAt: null,
                  updatedAt: null,
                });
              }
              // turn_start 前新会话 sid 尚为 null：契约上 turn_start 是首帧，
              // 拿到 sid 后 post/stream 走同一 gate+applyEvent 入口。
              const sid = createdSessionId;
              if (sid) {
                dispatchEvent(sid, ev, "post");
              }
            },
          },
          controller.signal,
          clarifySelection
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
          // 非 abort（网络错误等）：挂账不得跨轮残留（abort 分支不动——
          // 用户主动 stop 后 stream 通道仍可能补来 turn_end 触发恢复）。
          if (createdSessionId && pendingGapRef.current === createdSessionId) {
            pendingGapRef.current = null;
          }
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
          streamingRef.current = false;
          setStreaming(false);
        }
        // 无论成功/失败/停止都刷新会话列表：停止时后端 producer 仍会建好会话，需补到侧边栏
        refreshSessions();
      }
    },
    [streaming, current, logout, refreshSessions, dispatchEvent]
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
      {streamSid && <SessionStream sid={streamSid} dispatch={dispatchEvent} />}
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
            {gapNotice && (
              <div className="gap-notice" role="status">
                {gapNotice}
              </div>
            )}
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
                <MessageList
                  messages={messages}
                  onClarifySelect={(messageId, value, label) => {
                    // 轮次进行中禁止再点其他卡片：否则乐观置已选后 send 会被
                    // streaming 守卫静默拦截，卡片永久假已选（只能刷新恢复）。
                    if (streaming) return;
                    // 乐观置已选先于 send：同一事件批内先更新卡片再发请求，
                    // 防止 streaming 推进期间卡片仍可点。
                    setMessages((prev) =>
                      prev.map((m) =>
                        m.id === messageId && m.clarify
                          ? {
                              ...m,
                              clarify: { ...m.clarify, selected: value, disabled: true },
                            }
                          : m,
                      ),
                    );
                    void send(label, value);
                  }}
                />
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
