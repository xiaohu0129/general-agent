import { artifactDownloadUrl } from "../api/sessions";
import type { HistoryMessage } from "../api/types";
import type { ChatMessage, ToolCall } from "./model";

interface ToolResultMeta {
  content: string;
  messageId?: number | null;
  offloaded: boolean;
  size?: number | null;
  kind?: string | null;
}

function safeParse(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

export function historyToMessages(rows: HistoryMessage[], sessionId: string): ChatMessage[] {
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
      out.push({
        id: `h-${r.turnId}-u`,
        role: "user",
        content: r.content,
        toolCalls: [],
        source: "restored",
      });
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
      const msg: ChatMessage = {
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
        source: "restored",
      };
      if (Array.isArray(r.options) && r.options.length > 0) {
        msg.clarify = { options: r.options, disabled: r.selected != null };
        if (r.selected != null) {
          msg.clarify.selected = r.selected;
        }
      }
      out.push(msg);
    }
  }
  return out;
}
