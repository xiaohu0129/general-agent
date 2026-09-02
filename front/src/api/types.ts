// 后端 SSE 事件与 REST 响应的类型定义（与 general_agent/events.py、api 路由对齐）

export interface TurnStartData {
  turnId: string;
  traceId: string;
  sessionId?: string;
  eventSeq?: number;
}

export interface TurnDeltaData {
  turnId: string;
  traceId: string;
  content: string;
  eventSeq?: number;
}

export interface TurnEndData {
  turnId: string;
  traceId: string;
  finishReason: string;
  eventSeq?: number;
}

export interface ToolStartData {
  turnId: string;
  traceId: string;
  toolCallId: string;
  toolName: string;
  args: Record<string, unknown>;
  eventSeq?: number;
}

export interface ToolEndData {
  turnId: string;
  traceId: string;
  toolCallId: string;
  status: "success" | "error" | string;
  result?: unknown;
  error?: string;
  errorCode?: string;
  eventSeq?: number;
}

export interface ErrorData {
  turnId: string;
  traceId: string;
  message: string;
  code?: string;
  eventSeq?: number;
}

export type ChatEvent =
  | { event: "turn_start"; data: TurnStartData }
  | { event: "turn_delta"; data: TurnDeltaData }
  | { event: "turn_end"; data: TurnEndData }
  | { event: "tool_start"; data: ToolStartData }
  | { event: "tool_end"; data: ToolEndData }
  | { event: "error"; data: ErrorData };

export interface UserInfo {
  uid: string;
  username: string;
}

export interface SessionItem {
  sessionId: string;
  title: string;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface HistoryMessage {
  messageId?: number | null;
  turnId: string;
  role: "user" | "assistant" | "tool" | string;
  content: string;
  toolCalls?: Array<{
    id?: string;
    name?: string;
    arguments?: string | Record<string, unknown>;
  }> | null;
  toolCallId?: string | null;
  createdAt: string | null;
  // 大产物外置（Tier2）：非空表示正文已外置 blob，content 仅为 head 摘要
  contentRef?: string | null;
  contentSize?: number | null;
  contentKind?: string | null;
}

export interface MessagePage {
  messages: HistoryMessage[];
  nextCursor: number | null;
  hasMore: boolean;
}

export interface ApiErrorBody {
  code: string;
  message: string;
}
