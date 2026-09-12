import { describe, expect, it } from "vitest";
import type { ChatEvent } from "../api/types";
import { applyEvent, type ApplyContext } from "./applyEvent";
import type { ChatMessage, ToolCall } from "./model";

function userMsg(id: string, content = ""): ChatMessage {
  return { id, role: "user", content, toolCalls: [] };
}

function assistant(
  overrides: Partial<ChatMessage> & Pick<ChatMessage, "id">,
): ChatMessage {
  return { role: "assistant", content: "", toolCalls: [], ...overrides };
}

function tool(overrides: Partial<ToolCall> & Pick<ToolCall, "toolCallId">): ToolCall {
  return { toolName: "stub", args: {}, status: "running", ...overrides };
}

// 合成事件：测试只关心 applyEvent 实际读取的字段，其余必填项用窄转换补齐
function ev(event: string, data: Record<string, unknown>): ChatEvent {
  return { event, data } as unknown as ChatEvent;
}

const ctx: ApplyContext = { activeAssistantId: "a1" };

describe("applyEvent 六事件纯函数推进", () => {
  it("turn_start：把 turnId 绑定到活动轮气泡；重复投递无变化且引用稳定", () => {
    const u = userMsg("u1", "hi");
    const a = assistant({ id: "a1", streaming: true });
    const messages = [u, a];
    const e = ev("turn_start", { turnId: "t1", traceId: "tr" });

    const next = applyEvent(messages, ctx, e);

    expect(next).not.toBe(messages);
    expect(next).toHaveLength(2);
    expect(next[1].turnId).toBe("t1");
    expect(next[0]).toBe(u);
    expect(messages[1].turnId).toBeUndefined();

    const again = applyEvent(next, ctx, e);
    expect(again).toBe(next);
    expect(again[1]).toBe(next[1]);
  });

  it("turn_start：活动轮气泡不存在时原样返回数组引用、不新建消息", () => {
    const messages = [assistant({ id: "other", turnId: "t9" })];
    const out = applyEvent(messages, ctx, ev("turn_start", { turnId: "t1" }));
    expect(out).toBe(messages);
    expect(out).toHaveLength(1);
  });

  it("turn_delta：按 turnId 定位并顺序追加文本", () => {
    const a1 = assistant({ id: "a1", turnId: "t1" });
    const a2 = assistant({ id: "a2", turnId: "t2" });
    const messages = [a1, a2];

    const n1 = applyEvent(messages, ctx, ev("turn_delta", { turnId: "t1", content: "你好，" }));
    const n2 = applyEvent(n1, ctx, ev("turn_delta", { turnId: "t1", content: "世界" }));

    expect(n2[0].content).toBe("你好，世界");
    expect(n2[1]).toBe(a2);
    expect(messages[0].content).toBe("");
  });

  it("tool_start：追加 running 工具卡；args 缺省为 {}", () => {
    const a = assistant({ id: "a1", turnId: "t1" });
    const originalCalls = a.toolCalls;

    const next = applyEvent(
      [a],
      ctx,
      ev("tool_start", {
        turnId: "t1",
        toolCallId: "tc1",
        toolName: "search",
        args: { q: "x" },
      }),
    );

    expect(next[0].toolCalls).toEqual([
      { toolCallId: "tc1", toolName: "search", args: { q: "x" }, status: "running" },
    ]);
    expect(a.toolCalls).toBe(originalCalls);
    expect(a.toolCalls).toHaveLength(0);

    const noArgs = applyEvent(
      next,
      ctx,
      ev("tool_start", { turnId: "t1", toolCallId: "tc2", toolName: "calc" }),
    );
    expect(noArgs[0].toolCalls[1].status).toBe("running");
    expect(noArgs[0].toolCalls[1].args).toEqual({});
  });

  it("tool_end：success/error 两态更新对应工具卡，不动其他卡", () => {
    const t1 = tool({ toolCallId: "tc1" });
    const t2 = tool({ toolCallId: "tc2" });
    const a = assistant({ id: "a1", turnId: "t1", toolCalls: [t1, t2] });

    const next = applyEvent(
      [a],
      ctx,
      ev("tool_end", {
        turnId: "t1",
        toolCallId: "tc1",
        status: "success",
        result: { n: 1 },
      }),
    );

    expect(next[0].toolCalls[0]).toMatchObject({
      status: "success",
      result: { n: 1 },
    });
    expect(next[0].toolCalls[1]).toBe(t2);

    const next2 = applyEvent(
      next,
      ctx,
      ev("tool_end", {
        turnId: "t1",
        toolCallId: "tc2",
        status: "error",
        error: "boom",
        errorCode: "E1",
      }),
    );
    expect(next2[0].toolCalls[1]).toMatchObject({
      status: "error",
      error: "boom",
      errorCode: "E1",
    });
    expect(next2[0].toolCalls[0]).toBe(next[0].toolCalls[0]);
  });

  it("tool_end：未知 toolCallId 时数组与消息引用稳定、其他工具卡不变", () => {
    const t1 = tool({ toolCallId: "tc1" });
    const t2 = tool({ toolCallId: "tc2" });
    const a = assistant({ id: "a1", turnId: "t1", toolCalls: [t1, t2] });
    const messages = [a];

    const next = applyEvent(
      messages,
      ctx,
      ev("tool_end", { turnId: "t1", toolCallId: "missing", status: "success" }),
    );

    expect(next).toBe(messages);
    expect(next[0]).toBe(a);
    expect(next[0].toolCalls).toBe(a.toolCalls);
    expect(next[0].toolCalls[0]).toBe(t1);
    expect(next[0].toolCalls[1]).toBe(t2);
  });

  it("turn_end：仅结束对应轮消息的 streaming", () => {
    const a1 = assistant({ id: "a1", turnId: "t1", streaming: true });
    const a2 = assistant({ id: "a2", turnId: "t2", streaming: true });

    const next = applyEvent(
      [a1, a2],
      ctx,
      ev("turn_end", { turnId: "t1", finishReason: "stop" }),
    );

    expect(next[0].streaming).toBe(false);
    expect(next[1].streaming).toBe(true);
    expect(next[1]).toBe(a2);
  });

  it("error：写 streaming=false 与 error 文本", () => {
    const a = assistant({ id: "a1", turnId: "t1", streaming: true });

    const next = applyEvent(
      [a],
      ctx,
      ev("error", { turnId: "t1", message: "出错了" }),
    );

    expect(next[0].streaming).toBe(false);
    expect(next[0].error).toBe("出错了");
  });

  it("未知 turnId：delta/tool_end 返回原数组引用、不新增消息", () => {
    const a = assistant({
      id: "a1",
      turnId: "t1",
      content: "x",
      toolCalls: [tool({ toolCallId: "tc1" })],
    });
    const messages = [a];

    expect(applyEvent(messages, ctx, ev("turn_delta", { turnId: "nope", content: "z" }))).toBe(
      messages,
    );
    expect(
      applyEvent(
        messages,
        ctx,
        ev("tool_end", { turnId: "nope", toolCallId: "tc1", status: "success" }),
      ),
    ).toBe(messages);
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe("x");
  });

  it("未知事件名（运行时强转传入）：返回原数组引用、消息不变", () => {
    const a = assistant({ id: "a1", turnId: "t1", content: "x" });
    const messages = [a];

    const out = applyEvent(
      messages,
      ctx,
      ev("mystery_event", { turnId: "t1", options: [{ id: "o1", label: "是" }] }),
    );

    expect(out).toBe(messages);
    expect(out[0]).toBe(a);
    expect(out[0].content).toBe("x");
  });

  it("纯函数性：输入数组与未触碰对象（含嵌套工具卡）均不被 mutate", () => {
    const u = userMsg("u1");
    const t1 = tool({ toolCallId: "tc1", status: "success", result: 1 });
    const a1 = assistant({ id: "a1", turnId: "t1", content: "a", toolCalls: [t1] });
    const a2 = assistant({ id: "a2", turnId: "t2", content: "b" });
    const messages = [u, a1, a2];

    const next = applyEvent(
      messages,
      ctx,
      ev("tool_end", { turnId: "t1", toolCallId: "tc1", status: "error", error: "e" }),
    );

    expect(next).not.toBe(messages);
    expect(next[0]).toBe(u);
    expect(next[2]).toBe(a2);
    expect(messages.map((m) => m.id)).toEqual(["u1", "a1", "a2"]);
    expect(messages[1]).toBe(a1);
    expect(a1.toolCalls[0]).toBe(t1);
    expect(t1.status).toBe("success");
  });
});

describe("applyEvent clarify 状态机与跨轮作废", () => {
  const optsA = [{ label: "选项A", value: "a" }];
  const optsOld = [{ label: "旧选项", value: "old" }];

  it("clarify：本卡落 options 且初始 disabled、question 不入 content；更早 live 未选卡片作废；selected 与 restored 卡片不动", () => {
    const oldCard = assistant({
      id: "a0",
      turnId: "t0",
      clarify: { options: optsOld, disabled: false },
    });
    const pickedCard = assistant({
      id: "ap",
      turnId: "tp",
      clarify: { options: optsOld, selected: "old", disabled: false },
    });
    const restoredCard = assistant({
      id: "ar",
      turnId: "tr",
      source: "restored",
      clarify: { options: optsOld, disabled: false },
    });
    const target = assistant({ id: "a1", turnId: "t1", content: "同轮增量文本", streaming: true });
    const messages = [oldCard, pickedCard, restoredCard, target];

    const next = applyEvent(
      messages,
      ctx,
      ev("clarify", {
        turnId: "t1",
        traceId: "tr1",
        question: "请选择？",
        options: optsA,
      }),
    );

    expect(next[3].clarify).toEqual({ options: optsA, disabled: true });
    expect(next[3].content).toBe("同轮增量文本");
    expect(next[3].content).not.toContain("请选择？");
    expect(next[3].streaming).toBe(true);
    expect(next[0].clarify?.disabled).toBe(true);
    expect(next[1].clarify).toEqual({ options: optsOld, selected: "old", disabled: false });
    expect(next[2]).toBe(restoredCard);
    expect(next[2].clarify?.disabled).toBe(false);
  });

  it("clarify：options=[] 时按无卡片处理，不写 clarify 字段、原数组引用", () => {
    const a = assistant({ id: "a1", turnId: "t1", streaming: true });
    const messages = [a];

    const next = applyEvent(
      messages,
      ctx,
      ev("clarify", { turnId: "t1", traceId: "tr1", question: "q", options: [] }),
    );

    expect(next).toBe(messages);
    expect(next[0]).toBe(a);
    expect(next[0].clarify).toBeUndefined();
  });

  it("clarify：未知 turnId 时原数组引用，不作废任何卡片", () => {
    const oldCard = assistant({
      id: "a0",
      turnId: "t0",
      clarify: { options: optsOld, disabled: false },
    });
    const messages = [oldCard];

    const next = applyEvent(
      messages,
      ctx,
      ev("clarify", { turnId: "nope", traceId: "x", question: "q", options: optsA }),
    );

    expect(next).toBe(messages);
    expect(next[0]).toBe(oldCard);
    expect(next[0].clarify?.disabled).toBe(false);
  });

  it("clarify：turnId 只命中 restored 消息时不生成卡片、原数组引用", () => {
    const restored = assistant({
      id: "ar",
      turnId: "t1",
      source: "restored",
      content: "历史",
    });
    const messages = [restored];

    const next = applyEvent(
      messages,
      ctx,
      ev("clarify", { turnId: "t1", traceId: "x", question: "q", options: optsA }),
    );

    expect(next).toBe(messages);
    expect(next[0].clarify).toBeUndefined();
  });

  it("turn_end：正常收尾时最新 live 轮卡片启用（disabled=false）且 streaming=false", () => {
    const a = assistant({
      id: "a1",
      turnId: "t1",
      streaming: true,
      clarify: { options: optsA, disabled: true },
    });

    const next = applyEvent([a], ctx, ev("turn_end", { turnId: "t1", finishReason: "stop" }));

    expect(next[0].streaming).toBe(false);
    expect(next[0].clarify?.disabled).toBe(false);
  });

  it("turn_end：旧轮迟到（其后已有更新的 live 轮）时 streaming 结束但卡片保持 disabled", () => {
    const oldTurn = assistant({
      id: "a1",
      turnId: "t1",
      streaming: true,
      clarify: { options: optsOld, disabled: true },
    });
    const restoredAfter = assistant({ id: "ar", turnId: "t9", source: "restored" });
    const newTurn = assistant({ id: "a2", turnId: "t2", streaming: true });
    const messages = [oldTurn, restoredAfter, newTurn];

    const next = applyEvent(
      messages,
      ctx,
      ev("turn_end", { turnId: "t1", finishReason: "stop" }),
    );

    expect(next[0].streaming).toBe(false);
    expect(next[0].clarify?.disabled).toBe(true);
    expect(next[1]).toBe(restoredAfter);
    expect(next[2].streaming).toBe(true);
  });

  it("turn_start：绑定新乐观气泡时作废旧 live 未选卡片；selected/restored 不动；重复投递幂等", () => {
    const oldCard = assistant({
      id: "a0",
      turnId: "t0",
      clarify: { options: optsOld, disabled: false },
    });
    const pickedCard = assistant({
      id: "ap",
      turnId: "tp",
      clarify: { options: optsOld, selected: "old", disabled: false },
    });
    const restoredCard = assistant({
      id: "ar",
      turnId: "tr",
      source: "restored",
      clarify: { options: optsOld, disabled: false },
    });
    const bubble = assistant({ id: "a1", streaming: true });
    const messages = [oldCard, pickedCard, restoredCard, bubble];
    const e = ev("turn_start", { turnId: "t1", traceId: "tr1" });

    const next = applyEvent(messages, ctx, e);

    expect(next[3].turnId).toBe("t1");
    expect(next[0].clarify?.disabled).toBe(true);
    expect(next[1].clarify).toEqual({ options: optsOld, selected: "old", disabled: false });
    expect(next[2]).toBe(restoredCard);
    expect(next[2].clarify?.disabled).toBe(false);

    const again = applyEvent(next, ctx, e);
    expect(again).toBe(next);
  });

  it("error：异常收尾时卡片保持 disabled=true，写 error 与 streaming=false，不启用卡片", () => {
    const a = assistant({
      id: "a1",
      turnId: "t1",
      streaming: true,
      clarify: { options: optsA, disabled: true },
    });

    const next = applyEvent(
      [a],
      ctx,
      ev("error", { turnId: "t1", message: "出错了" }),
    );

    expect(next[0].streaming).toBe(false);
    expect(next[0].error).toBe("出错了");
    expect(next[0].clarify?.disabled).toBe(true);
  });
});

describe("D9 provenance 来源对账（历史还原 + ring 全量重放）", () => {
  const replayOpts = [{ label: "是", value: "yes" }];

  function restoredHistory(): ChatMessage[] {
    const u: ChatMessage = {
      id: "u1",
      role: "user",
      content: "历史提问",
      toolCalls: [],
      source: "restored",
    };
    const clarify = { options: replayOpts, selected: "yes", disabled: true };
    const a = assistant({
      id: "a1",
      turnId: "T1",
      content: "A",
      source: "restored",
      clarify,
      toolCalls: [tool({ toolCallId: "tc0", status: "success", result: { n: 1 } })],
    });
    return [u, a];
  }

  it("ring 重放：六事件命中 restored 消息全部整事件忽略、返回原数组引用、内容/卡片/toolCalls 不重复", () => {
    const messages = restoredHistory();
    const a = messages[1];
    const clarifyRef = a.clarify;
    const callsRef = a.toolCalls;

    const replay: ChatEvent[] = [
      ev("turn_delta", { turnId: "T1", content: "A" }),
      ev("clarify", { turnId: "T1", traceId: "x", question: "q", options: replayOpts }),
      ev("tool_start", { turnId: "T1", toolCallId: "tcNew", toolName: "search" }),
      ev("tool_end", { turnId: "T1", toolCallId: "tc0", status: "success", result: { n: 2 } }),
      ev("turn_end", { turnId: "T1", finishReason: "stop" }),
      ev("error", { turnId: "T1", message: "boom" }),
    ];

    for (const e of replay) {
      const out = applyEvent(messages, ctx, e);
      expect(out).toBe(messages);
    }

    expect(messages).toHaveLength(2);
    expect(messages[1]).toBe(a);
    expect(messages[1].content).toBe("A");
    expect(messages[1].clarify).toBe(clarifyRef);
    expect(messages[1].clarify).toEqual({ options: replayOpts, selected: "yes", disabled: true });
    expect(messages[1].toolCalls).toBe(callsRef);
    expect(messages[1].toolCalls).toHaveLength(1);
    expect(messages[1].toolCalls[0].toolCallId).toBe("tc0");
    expect(messages[1].streaming).toBeUndefined();
    expect(messages[1].error).toBeUndefined();
  });

  it("未知 turnId：clarify/delta/turn_end 均原数组引用、长度不变、不新建气泡", () => {
    const messages = restoredHistory();

    expect(
      applyEvent(messages, ctx, ev("clarify", { turnId: "T9", options: replayOpts })),
    ).toBe(messages);
    expect(
      applyEvent(messages, ctx, ev("turn_delta", { turnId: "T9", content: "z" })),
    ).toBe(messages);
    expect(
      applyEvent(messages, ctx, ev("turn_end", { turnId: "T9", finishReason: "stop" })),
    ).toBe(messages);

    expect(messages).toHaveLength(2);
    expect(messages[1].content).toBe("A");
    expect(messages[1].toolCalls).toHaveLength(1);
  });

  it("notification 形态（无 turnId）：不抛错、不入消息模型、原数组引用", () => {
    const messages = restoredHistory();

    const out = applyEvent(
      messages,
      ctx,
      ev("notification", { taskId: "task-1", status: "done" }),
    );

    expect(out).toBe(messages);
    expect(out).toHaveLength(2);
  });

  it("turn_start：绑定目标为 restored 消息时 no-op、原数组引用", () => {
    const restored = assistant({
      id: "a1",
      source: "restored",
      content: "历史",
      toolCalls: [],
    });
    const list = [restored];

    const out = applyEvent(list, ctx, ev("turn_start", { turnId: "T1", traceId: "x" }));

    expect(out).toBe(list);
    expect(out[0].turnId).toBeUndefined();
  });

  it("G 批挂账串联：旧 live 卡可点 → 新 turn_start 绑定新气泡（旧卡作废）→ 旧轮 turn_end 迟到不复活旧卡、新轮不受影响", () => {
    const oldCard = assistant({
      id: "a0",
      turnId: "t0",
      streaming: false,
      clarify: { options: [{ label: "旧", value: "old" }], disabled: false },
    });
    const bubble = assistant({ id: "a1", streaming: true });
    const messages = [oldCard, bubble];

    const bound = applyEvent(messages, ctx, ev("turn_start", { turnId: "t1", traceId: "tr1" }));
    expect(bound[1].turnId).toBe("t1");
    expect(bound[0].clarify?.disabled).toBe(true);

    const lateEnd = applyEvent(bound, ctx, ev("turn_end", { turnId: "t0", finishReason: "stop" }));
    expect(lateEnd[0].clarify?.disabled).toBe(true);
    expect(lateEnd[0].streaming).toBe(false);
    expect(lateEnd[1].turnId).toBe("t1");
    expect(lateEnd[1].streaming).toBe(true);
    expect(lateEnd[1].clarify).toBeUndefined();
  });
});
