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
  signal?: AbortSignal,
  clarifySelection?: string
): Promise<void> {
  const body: Record<string, unknown> = { message, sessionId: sessionId ?? undefined };
  if (clarifySelection) {
    body.clarify_selection = { value: clarifySelection };
  }
  const resp = await fetch(`${API_BASE}/chat`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
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
    let id: string | null = null;
    const dataLines: string[] = [];
    // 块内行分隔兼容 CRLF（sse-starlette 默认）/ LF / CR
    for (const line of block.split(/\r\n|\r|\n/)) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      } else if (line.startsWith("id:")) {
        id = line.slice(3).trim();
      }
      // ": heartbeat" 等注释行忽略
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
    handlers.onEvent(
      (id !== null ? { event: eventName, data, id } : { event: eventName, data }) as ChatEvent
    );
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    // SSE 事件以空行分隔，空行可以是 CRLFCRLF / LFLF / CRCR
    const sep = /\r\n\r\n|\r\r|\n\n/;
    let m: RegExpExecArray | null;
    while ((m = sep.exec(buffer)) !== null) {
      const block = buffer.slice(0, m.index);
      buffer = buffer.slice(m.index + m[0].length);
      if (block.trim()) {
        dispatch(block);
      }
    }
  }
  if (buffer.trim()) {
    dispatch(buffer);
  }
}
