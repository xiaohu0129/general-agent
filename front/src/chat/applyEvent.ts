import type { ChatEvent } from "../api/types";
import type { ChatClarify, ChatMessage, ToolCall } from "./model";

export interface ApplyContext {
  // 当前活动轮乐观气泡 id：turn_start 用它把 turnId 绑定到该消息
  activeAssistantId: string;
}

// 纯函数：输入当前 messages + 活动轮绑定上下文 + SSE 事件，输出推进后的新 messages。
// 不触碰 React/fetch/全局状态；不原地 mutate 输入数组与消息对象；定位不到目标时返回原数组。
export function applyEvent(
  messages: ChatMessage[],
  ctx: ApplyContext,
  ev: ChatEvent,
): ChatMessage[] {
  switch (ev.event) {
    case "turn_start":
      return applyTurnStart(messages, ctx, ev.data.turnId);
    case "turn_delta":
      if (!liveTurnTarget(messages, ev.data.turnId)) return messages;
      return mapByTurnId(messages, ev.data.turnId, (m) => ({
        ...m,
        content: m.content + ev.data.content,
      }));
    case "tool_start": {
      if (!liveTurnTarget(messages, ev.data.turnId)) return messages;
      const card: ToolCall = {
        toolCallId: ev.data.toolCallId,
        toolName: ev.data.toolName,
        args: ev.data.args || {},
        status: "running",
      };
      return mapByTurnId(messages, ev.data.turnId, (m) => ({
        ...m,
        toolCalls: [...m.toolCalls, card],
      }));
    }
    case "tool_end":
      if (!liveTurnTarget(messages, ev.data.turnId)) return messages;
      return mapByTurnId(messages, ev.data.turnId, (m) => {
        // 该轮没有对应 toolCallId 时返回原消息：mapMessage 据引用判定无变化，
        // 数组与消息引用均保持稳定。
        if (!m.toolCalls.some((t) => t.toolCallId === ev.data.toolCallId)) {
          return m;
        }
        return {
          ...m,
          toolCalls: m.toolCalls.map((t): ToolCall =>
            t.toolCallId === ev.data.toolCallId
              ? {
                  ...t,
                  status: ev.data.status === "error" ? "error" : "success",
                  result: ev.data.result,
                  error: ev.data.error,
                  errorCode: ev.data.errorCode,
                }
              : t,
          ),
        };
      });
    case "clarify":
      if (!liveTurnTarget(messages, ev.data.turnId)) return messages;
      return applyClarify(messages, ev.data.turnId, ev.data.options);
    case "turn_end":
      if (!liveTurnTarget(messages, ev.data.turnId)) return messages;
      return applyTurnEnd(messages, ev.data.turnId);
    case "error":
      if (!liveTurnTarget(messages, ev.data.turnId)) return messages;
      return mapByTurnId(messages, ev.data.turnId, (m) => ({
        ...m,
        streaming: false,
        error: ev.data.message,
      }));
    // 运行时 chat.ts 会把任意事件名强转传入；
    // 未识别事件安全 no-op，保留迁移前内联 switch 的行为，返回原数组。
    default:
      return messages;
  }
}

// live 消息：本次会话实时产生（source 缺省亦视为 live）；restored 为历史还原消息。
function isLiveAssistant(m: ChatMessage): boolean {
  return m.role === "assistant" && m.source !== "restored";
}

// D9 provenance 对账：除 turn_start 外所有携带 turnId 的事件，仅当命中 live 助手
// 消息时才允许推进。命中 restored（EventSource 首连 ring 全量重放历史轮）或未命中
// （未知/更早轮次/他标签页轮次）一律整事件忽略——调用方据 undefined 返回原数组，
// 不拼 content、不插 toolCalls、不写 clarify、不翻 streaming/error，更不新建气泡。
function liveTurnTarget(messages: ChatMessage[], turnId: string): ChatMessage | undefined {
  const target = messages.find((m) => m.role === "assistant" && m.turnId === turnId);
  if (!target || target.source === "restored") {
    return undefined;
  }
  return target;
}

// 未 selected（无 selected 值）的 live 澄清卡片
function isStaleLiveCard(m: ChatMessage): boolean {
  return (
    isLiveAssistant(m) &&
    m.clarify !== undefined &&
    m.clarify.selected == null &&
    m.clarify.disabled !== true
  );
}

function disableClarify(m: ChatMessage): ChatMessage {
  const clarify: ChatClarify = { ...(m.clarify as ChatClarify), disabled: true };
  return { ...m, clarify };
}

// turn_start：绑定活动轮乐观气泡；绑定成功的同时作废更早的未选 live 卡片
// （D6 触发②：停止后立即重发竞态）。重复 turn_start 绑定不上即无任何副作用，
// 值幂等，数组引用稳定。
function applyTurnStart(
  messages: ChatMessage[],
  ctx: ApplyContext,
  turnId: string,
): ChatMessage[] {
  // 绑定目标必须是无 turnId 的 live 乐观气泡：restored 历史消息（id 偶合）或
  // 目标缺失时 no-op。
  const bindIdx = messages.findIndex(
    (m) => m.id === ctx.activeAssistantId && m.turnId === undefined && m.source !== "restored",
  );
  if (bindIdx < 0) {
    return messages;
  }
  let changed = false;
  const next = messages.map((m, i) => {
    if (i === bindIdx) {
      changed = true;
      return { ...m, turnId };
    }
    if (i < bindIdx && isStaleLiveCard(m)) {
      changed = true;
      return disableClarify(m);
    }
    return m;
  });
  return changed ? next : messages;
}

// clarify：只落 options（question 已由同轮 turn_delta 渲染，不写 content），
// 本卡初始 disabled=true；同时作废旧的未选 live 卡片（D6 触发①）。
// options 为空按"无卡片"防御处理：整个事件 no-op。定位不到 live 目标亦 no-op。
function applyClarify(
  messages: ChatMessage[],
  turnId: string,
  options: ChatClarify["options"],
): ChatMessage[] {
  if (!Array.isArray(options) || options.length === 0) {
    return messages;
  }
  const targetIdx = messages.findIndex(
    (m) => isLiveAssistant(m) && m.turnId === turnId,
  );
  if (targetIdx < 0) {
    return messages;
  }
  let changed = false;
  const next = messages.map((m, i) => {
    if (i === targetIdx) {
      changed = true;
      return { ...m, clarify: { options, disabled: true } };
    }
    if (i < targetIdx && isStaleLiveCard(m)) {
      changed = true;
      return disableClarify(m);
    }
    return m;
  });
  return changed ? next : messages;
}

// turn_end：streaming=false 之外，仅当目标消息是数组中最后一个 live assistant
// （当前最新 live 轮）时启用其澄清卡片；旧轮迟到的 turn_end 不复活已作废卡片。
function applyTurnEnd(messages: ChatMessage[], turnId: string): ChatMessage[] {
  let lastLiveIdx = -1;
  messages.forEach((m, i) => {
    if (isLiveAssistant(m)) {
      lastLiveIdx = i;
    }
  });
  let changed = false;
  const next = messages.map((m, i) => {
    if (m.turnId !== turnId) {
      return m;
    }
    changed = true;
    if (i === lastLiveIdx && m.clarify !== undefined) {
      return { ...m, streaming: false, clarify: { ...m.clarify, disabled: false } };
    }
    return { ...m, streaming: false };
  });
  return changed ? next : messages;
}

function mapByTurnId(
  messages: ChatMessage[],
  turnId: string,
  fn: (m: ChatMessage) => ChatMessage,
): ChatMessage[] {
  return mapMessage(messages, (m) => m.turnId === turnId, fn);
}

function mapMessage(
  messages: ChatMessage[],
  predicate: (m: ChatMessage) => boolean,
  fn: (m: ChatMessage) => ChatMessage,
): ChatMessage[] {
  // 以输出对象引用是否变化为准：谓词不命中、或命中但 fn 自身选择不改（如
  // tool_end 找不到对应 toolCallId）时，整条路径引用稳定，返回原数组。
  let changed = false;
  const next = messages.map((m) => {
    if (!predicate(m)) {
      return m;
    }
    const updated = fn(m);
    if (updated !== m) {
      changed = true;
    }
    return updated;
  });
  return changed ? next : messages;
}
