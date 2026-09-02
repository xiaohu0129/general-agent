import { API_BASE, ApiError } from "./client";
import type { ChatEvent } from "./types";

interface StreamHandlers {
  onEvent: (ev: ChatEvent) => void;
}

// POST /chat 是 SSE 流，EventSource 不支持 POST，故用 fetch + ReadableStream 手动解析 SSE 帧。
export async function streamChat(
  message: string,
  sessionId: string | null,
  handlers: StreamHandlers,
  signal?: AbortSignal
): Promise<void> {
  const resp = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ message, sessionId: sessionId ?? undefined }),
    signal,
  });

  if (!resp.ok) {
    let code = "HTTP_ERROR";
    let msg = `请求失败（${resp.status}）`;
    try {
      const body = await resp.json();
      code = body.code || code;
      msg = body.message || msg;
    } catch {
      // 非 JSON 错误体，沿用默认消息
    }
    throw new ApiError(resp.status, code, msg);
  }
  if (!resp.body) {
    throw new ApiError(0, "NO_STREAM", "响应不是流式数据");
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const dispatch = (block: string) => {
    let eventName: string | null = null;
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
      // id: 行与 ": heartbeat" 注释行前端无需处理
    }
    if (!eventName || dataLines.length === 0) {
      return;
    }
    let data: unknown;
    try {
      data = JSON.parse(dataLines.join("\n"));
    } catch {
      return;
    }
    handlers.onEvent({ event: eventName, data } as ChatEvent);
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    // SSE 事件以空行分隔
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (block.trim()) {
        dispatch(block);
      }
    }
  }
  if (buffer.trim()) {
    dispatch(buffer);
  }
}
