import { describe, expect, it } from "vitest";
import type { HistoryMessage } from "../api/types";
import { historyToMessages } from "./history";

function row(overrides: Partial<HistoryMessage> & Pick<HistoryMessage, "turnId" | "role">): HistoryMessage {
  return { content: "", createdAt: null, ...overrides };
}

const opts = [
  { label: "选项A", value: "a" },
  { label: "选项B", value: "b" },
];

describe("historyToMessages：clarify/source 历史还原", () => {
  it("assistant 带 options 无 selected：clarify 存在、options 透传、无 selected 键、未禁用、source=restored", () => {
    const rows = [row({ turnId: "t1", role: "assistant", content: "请选择", options: opts })];

    const [m] = historyToMessages(rows, "sess-1");

    expect(m.source).toBe("restored");
    expect(m.clarify).toBeDefined();
    expect(m.clarify?.options).toEqual(opts);
    expect(m.clarify).not.toHaveProperty("selected");
    expect(m.clarify?.disabled).not.toBe(true);
  });

  it("options + selected 命中某 value：disabled=true 且 selected 为该值", () => {
    const rows = [row({ turnId: "t1", role: "assistant", options: opts, selected: "b" })];

    const [m] = historyToMessages(rows, "sess-1");

    expect(m.clarify?.disabled).toBe(true);
    expect(m.clarify?.selected).toBe("b");
  });

  it("脏数据：selected 不在 options value 集合内，不崩溃、全禁用、selected 原样保留", () => {
    const rows = [row({ turnId: "t1", role: "assistant", options: opts, selected: "  weird-value " })];

    const [m] = historyToMessages(rows, "sess-1");

    expect(m.clarify).toBeDefined();
    expect(m.clarify?.disabled).toBe(true);
    expect(m.clarify?.selected).toBe("  weird-value ");
  });

  it.each([
    ["空数组", []],
    ["null", null],
    ["缺失", undefined],
  ])("options 为%s：消息不带 clarify 字段，普通字段映射不变", (_label, options) => {
    const rows = [
      row({ turnId: "t1", role: "user", content: "你好" }),
      row({ turnId: "t1", role: "assistant", content: "回答", options, toolCalls: [] }),
    ];

    const out = historyToMessages(rows, "sess-1");

    expect(out).toHaveLength(2);
    const a = out[1];
    expect(a).not.toHaveProperty("clarify");
    expect(a.content).toBe("回答");
    expect(a.toolCalls).toEqual([]);
    expect(a.source).toBe("restored");
  });

  it("只有 selected 无 options：不带 clarify 字段", () => {
    const rows = [row({ turnId: "t1", role: "assistant", selected: "a" })];

    const [m] = historyToMessages(rows, "sess-1");

    expect(m).not.toHaveProperty("clarify");
  });

  it("历史 user 消息：source=restored 且无 clarify", () => {
    const rows = [row({ turnId: "t1", role: "user", content: "提问" })];

    const [m] = historyToMessages(rows, "sess-1");

    expect(m.role).toBe("user");
    expect(m.source).toBe("restored");
    expect(m).not.toHaveProperty("clarify");
  });

  it("工具卡与外置产物字段映射不回归：tool 结果合并、artifactUrl 拼接、assistant 正文外置", () => {
    const rows: HistoryMessage[] = [
      row({ turnId: "t1", role: "user", content: "跑一下" }),
      row({
        turnId: "t1",
        role: "assistant",
        content: "head…",
        contentRef: "blob-assistant",
        contentSize: 123,
        contentKind: "text/markdown",
        messageId: 10,
        toolCalls: [{ id: "tc1", name: "shell", arguments: '{"cmd":"ls"}' }],
      }),
      row({
        turnId: "t1",
        role: "tool",
        toolCallId: "tc1",
        content: '{"ok":true}',
        messageId: 11,
        contentRef: "blob-tool",
        contentSize: 456,
        contentKind: "application/json",
      }),
    ];

    const out = historyToMessages(rows, "sess-1");
    const a = out[1];

    expect(a.toolCalls).toHaveLength(1);
    const tc = a.toolCalls[0];
    expect(tc.toolCallId).toBe("tc1");
    expect(tc.toolName).toBe("shell");
    expect(tc.args).toEqual({ cmd: "ls" });
    expect(tc.status).toBe("success");
    expect(tc.result).toEqual({ ok: true });
    expect(tc.offloaded).toBe(true);
    expect(tc.artifactUrl).toContain("/sessions/sess-1/artifacts/11");
    expect(tc.artifactSize).toBe(456);
    expect(tc.artifactKind).toBe("application/json");
    expect(a.offloaded).toBe(true);
    expect(a.artifactUrl).toContain("/sessions/sess-1/artifacts/10");
    expect(a.artifactSize).toBe(123);
  });
});

describe("historyToMessages：工具卡真实 status（U11a 历史 DTO）", () => {
  function toolRows(status: HistoryMessage["status"]) {
    return [
      row({ turnId: "t1", role: "user", content: "跑一下" }),
      row({
        turnId: "t1",
        role: "assistant",
        toolCalls: [{ id: "tc1", name: "shell", arguments: "{}" }],
      }),
      row({ turnId: "t1", role: "tool", toolCallId: "tc1", content: "err", status }),
    ];
  }

  it("tool 行 status=error：对应工具卡 status=error（刷新后失败可见）", () => {
    const [, a] = historyToMessages(toolRows("error"), "sess-1");

    expect(a.toolCalls).toHaveLength(1);
    expect(a.toolCalls[0].status).toBe("error");
  });

  it("tool 行 status=success：工具卡 status=success", () => {
    const [, a] = historyToMessages(toolRows("success"), "sess-1");

    expect(a.toolCalls[0].status).toBe("success");
  });

  it("tool 行 status 缺省或 null：按 success 兜底（兼容旧数据）", () => {
    const [, a1] = historyToMessages(toolRows(undefined), "sess-1");
    const [, a2] = historyToMessages(toolRows(null), "sess-1");

    expect(a1.toolCalls[0].status).toBe("success");
    expect(a2.toolCalls[0].status).toBe("success");
  });

  it("非 tool 行携带的 status 不影响工具卡：无匹配 tool 结果时仍 success 兜底", () => {
    const rows = [
      row({ turnId: "t1", role: "user", content: "q", status: "error" }),
      row({
        turnId: "t1",
        role: "assistant",
        content: "a",
        status: null,
        toolCalls: [{ id: "tc-x", name: "shell", arguments: "{}" }],
      }),
    ];

    const [, a] = historyToMessages(rows, "sess-1");
    expect(a.toolCalls[0].status).toBe("success");
  });
});
