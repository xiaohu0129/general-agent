import { API_BASE } from "./client";
import type { ChatEvent } from "./types";

// GET /stream 持久 SSE 通道的 EventSource 薄封装。
// 注意：Last-Event-ID 携带与断线自动重连均由浏览器原生承担，前端不做退避/缺口重拉
// （缺口检测在后续批次处理）。

export interface StreamHandlers {
  // 命名业务事件解析后（ChatEvent 联合，event 字段为名字）
  onEvent: (ev: ChatEvent) => void;
  // fatal=false：readyState=CONNECTING，浏览器将自动重连（调用方仅记日志）
  // fatal=true：readyState=CLOSED，本封装已 es.close()，调用方降级为仅即时流
  onConnectionError?: (fatal: boolean) => void;
}

export interface StreamConnection {
  close(): void;
}

// 后端每类帧都带 event: 行：浏览器只派发给按名注册的 listener，onmessage 收不到，
// 故必须逐名 addEventListener（不含业务 error——见下方 onerror 分流）。
const NAMED_EVENTS = [
  "turn_start",
  "turn_delta",
  "turn_end",
  "tool_start",
  "tool_end",
  "clarify",
  "notification",
] as const;

export function createStreamConnection(
  sessionId: string,
  handlers: StreamHandlers,
): StreamConnection {
  // 帧形状 hardening：业务帧 data 必须是非 null 对象（命名帧 event 由监听名决定，
  // 允许 {}）；parse 成功但得到 null/数字/字符串等一律按畸形帧处理。
  const isObjectData = (v: unknown): boolean =>
    typeof v === "object" && v !== null;

  const es = new EventSource(
    `${API_BASE}/stream?sessionId=${encodeURIComponent(sessionId)}`,
  );

  for (const name of NAMED_EVENTS) {
    es.addEventListener(name, (e) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse((e as MessageEvent).data);
      } catch {
        // 畸形帧静默忽略，不影响后续事件
        return;
      }
      if (!isObjectData(parsed)) return;
      handlers.onEvent({ event: name, data: parsed } as ChatEvent);
    });
  }

  // 业务 error 与连接 error 的同名陷阱：SSE 的 `event: error` 数据帧在浏览器里
  // 表现为类型 "error" 的 MessageEvent（同样进入 onerror），而连接失败是无 data 的
  // 普通 Event。若再按名监听 "error" 会与 onerror 双发，因此统一在此分流，保证
  // 业务 error 恰好投递一次。
  es.onerror = (e: Event) => {
    let parsed: unknown;
    let parsedOk = false;
    if (e instanceof MessageEvent && typeof e.data === "string" && e.data.length > 0) {
      try {
        parsed = JSON.parse(e.data);
        parsedOk = true;
      } catch {
        // 畸形 JSON：不当业务 error，落入连接 error 分流
      }
    }
    if (parsedOk && isObjectData(parsed)) {
      handlers.onEvent({ event: "error", data: parsed } as ChatEvent);
      return;
    }
    if (es.readyState === EventSource.CLOSED) {
      es.close();
      handlers.onConnectionError?.(true);
    } else {
      // CONNECTING（浏览器即将自动重连）或其他状态，一律按可恢复处理
      handlers.onConnectionError?.(false);
    }
  };

  return {
    close() {
      es.close();
    },
  };
}
