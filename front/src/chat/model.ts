export interface ToolCall {
  toolCallId: string;
  toolName: string;
  args: Record<string, unknown>;
  status: "running" | "success" | "error" | "stopped";
  result?: unknown;
  error?: string;
  errorCode?: string;
  // 大产物外置（Tier2）：结果正文已外置 blob，result 仅为 head 摘要
  offloaded?: boolean;
  artifactUrl?: string;
  artifactSize?: number | null;
  artifactKind?: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  toolCalls: ToolCall[];
  error?: string;
  streaming?: boolean;
  stopped?: boolean;
  turnId?: string;
  // 大产物外置（Tier2）：助手正文本身被外置 blob，content 仅为 head 摘要
  offloaded?: boolean;
  artifactUrl?: string;
  artifactSize?: number | null;
}
