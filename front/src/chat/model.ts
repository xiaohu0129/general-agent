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

export interface ClarifyOption {
  label: string;
  value: string;
}

// 结构化澄清卡片状态：历史还原时由 options/selected 生成；
// selected 命中与否由组件判定，模型只保证 selected 有值即整卡禁用
export interface ChatClarify {
  options: ClarifyOption[];
  selected?: string;
  disabled?: boolean;
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
  // 结构化澄清选项（仅 assistant 可能有）
  clarify?: ChatClarify;
  // 消息来源：live=本次会话实时产生；restored=历史消息还原
  source?: "live" | "restored";
}
