import { describe, expect, it, vi } from "vitest";
import type { ChatEvent } from "../api/types";
import { applyEvent, type ApplyContext } from "./applyEvent";
import type { ChatMessage } from "./model";
import { createEventGate, SEEN_LRU_CAPACITY, type SequencedEvent } from "./eventGate";

function seq(turnId: string | null | undefined, eventSeq: number | null | undefined): SequencedEvent {
  return { data: { turnId, eventSeq } };
}

// 合成事件：测试只关心 applyEvent 实际读取的字段，其余必填项用窄转换补齐
function ev(event: string, data: Record<string, unknown>): ChatEvent {
  return { event, data } as unknown as ChatEvent;
}

describe("eventGate (turnId,eventSeq) 幂等门", () => {
  it("1a. 同 (turnId,seq) post→stream：apply true 后 false", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", 1), "post")).toEqual({ apply: true, gap: false });
    expect(gate.ingest(seq("t1", 1), "stream")).toEqual({ apply: false, gap: false });
  });

  it("1b. 同 (turnId,seq) stream→post：apply true 后 false", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", 1), "stream")).toEqual({ apply: true, gap: false });
    expect(gate.ingest(seq("t1", 1), "post")).toEqual({ apply: false, gap: false });
  });

  it("2. 同 turnId 不同 seq：依次放行", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", 1), "post").apply).toBe(true);
    expect(gate.ingest(seq("t1", 2), "post").apply).toBe(true);
    expect(gate.ingest(seq("t1", 2), "stream").apply).toBe(false);
  });

  it("3. 不同 turnId 同 seq：均放行", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", 7), "post").apply).toBe(true);
    expect(gate.ingest(seq("t2", 7), "stream").apply).toBe(true);
  });

  it("4. 乱序到达：seq 5 先到、seq 3 后到，均 apply 且不报 gap", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", 5), "post")).toEqual({ apply: true, gap: false });
    expect(gate.ingest(seq("t1", 3), "stream")).toEqual({ apply: true, gap: false });
  });

  it("5. 无 seq（缺失/null/非数字）：post 放行不记忆，stream 忽略", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", undefined), "post")).toEqual({ apply: true, gap: false });
    expect(gate.ingest(seq("t1", null), "stream")).toEqual({ apply: false, gap: false });
    // post 通道两条无 seq 均放行（无法构造键，不做记忆，重复投递不去重——防御分支）
    expect(gate.ingest(seq("t1", undefined), "post")).toEqual({ apply: true, gap: false });
    expect(gate.ingest(seq("t1", null), "post")).toEqual({ apply: true, gap: false });
    // 非数字 seq 同样走无 seq 防御分支（运行时异常数据，窄转换绕过静态类型）
    const stringSeq = { data: { turnId: "t1", eventSeq: "3" } } as unknown as SequencedEvent;
    expect(gate.ingest(stringSeq, "stream")).toEqual({ apply: false, gap: false });
    expect(gate.ingest(stringSeq, "post")).toEqual({ apply: true, gap: false });
  });

  it("6. 无 turnId 有 seq（notification 形态）：可去重且不与 turn 事件键冲突", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq(null, 1), "post").apply).toBe(true);
    expect(gate.ingest(seq(undefined, 1), "stream").apply).toBe(false);
    // turnId 缺省统一塌缩为空串键：null/undefined 互视为同一 notification 键
    expect(gate.ingest(seq("t1", 1), "post").apply).toBe(true);
    expect(gate.ingest(seq("t1", 1), "stream").apply).toBe(false);
  });

  it("7. 两实例隔离：同事件两个 gate 各自 apply", () => {
    const g1 = createEventGate();
    const g2 = createEventGate();
    expect(g1.ingest(seq("t1", 1), "post").apply).toBe(true);
    expect(g2.ingest(seq("t1", 1), "post").apply).toBe(true);
  });

  it("8. G 批挂账：gate+applyEvent 串联，重复 clarify 不覆盖用户已选 selected", () => {
    const gate = createEventGate();
    const ctx: ApplyContext = { activeAssistantId: "a1" };
    const messages: ChatMessage[] = [
      { id: "a1", role: "assistant", content: "", toolCalls: [], turnId: "t1" },
    ];
    const options = [
      { label: "选项A", value: "a" },
      { label: "选项B", value: "b" },
    ];
    const clarifyEv = ev("clarify", { turnId: "t1", traceId: "tr", eventSeq: 3, options });

    // 首次从 post 到达：ingest 放行并应用，卡片落 options 且初始 disabled
    expect(gate.ingest(clarifyEv, "post")).toEqual({ apply: true, gap: false });
    let next = applyEvent(messages, ctx, clarifyEv);
    expect(next[0].clarify).toEqual({ options, disabled: true });

    // 模拟用户点选：消息上写入 selected
    next = [{ ...next[0], clarify: { ...next[0].clarify!, selected: "a" } }];
    expect(next[0].clarify?.selected).toBe("a");

    // 同一 (turnId,3) 再从 stream 到达：ingest 拦截，applyEvent 不被调用
    const apply = vi.fn(applyEvent);
    const r = gate.ingest(clarifyEv, "stream");
    expect(r.apply).toBe(false);
    if (r.apply) {
      next = apply(next, ctx, clarifyEv);
    }
    expect(apply).not.toHaveBeenCalled();
    expect(next[0].clarify?.selected).toBe("a");
  });
});

describe("eventGate 会话级高水位缺口检测", () => {
  it("G1 首事件 seq=50：建基线，apply=true gap=false（首连重放起点 seq 很大也不报）", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", 50), "stream")).toEqual({ apply: true, gap: false });
    expect(gate.maxSeenSeq).toBe(50);
  });

  it("G2a notification 占 seq：10→notification(11)→12 连续无 gap（相邻 turn 夹 notification 不误报）", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", 10), "stream").gap).toBe(false);
    expect(gate.ingest(seq(null, 11), "stream").gap).toBe(false);
    expect(gate.ingest(seq("t2", 12), "stream")).toEqual({ apply: true, gap: false });
    expect(gate.maxSeenSeq).toBe(12);
  });

  it("G2b 缺 notification：10→12 直接跳变才 gap", () => {
    const gate = createEventGate();
    gate.ingest(seq("t1", 10), "stream");
    expect(gate.ingest(seq("t2", 12), "stream")).toEqual({ apply: true, gap: true });
  });

  it("G3 seq1→seq3：gap=true 且 apply=true；迟到 seq2 不重复报 gap（水位已 3）", () => {
    const gate = createEventGate();
    gate.ingest(seq("t1", 1), "post");
    expect(gate.ingest(seq("t2", 3), "stream")).toEqual({ apply: true, gap: true });
    expect(gate.maxSeenSeq).toBe(3);
    // 乱序迟到的 seq2 是未见键：仍 apply，但 gap=false
    expect(gate.ingest(seq("t3", 2), "post")).toEqual({ apply: true, gap: false });
    expect(gate.maxSeenSeq).toBe(3);
  });

  it("G4 重复键：apply=false gap=false，不触发缺口重拉", () => {
    const gate = createEventGate();
    gate.ingest(seq("t1", 1), "post");
    gate.ingest(seq("t2", 3), "stream"); // 真缺口
    expect(gate.ingest(seq("t2", 3), "post")).toEqual({ apply: false, gap: false });
  });

  it("G5 NaN 走无 seq 防御：post apply/stream 忽略，不建水位，后续首个有限 seq 仍按基线", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", Number.NaN), "post")).toEqual({ apply: true, gap: false });
    expect(gate.ingest(seq("t1", Number.NaN), "stream")).toEqual({ apply: false, gap: false });
    expect(gate.maxSeenSeq).toBeNull();
    expect(gate.ingest(seq("t1", 1), "stream")).toEqual({ apply: true, gap: false });
    expect(gate.maxSeenSeq).toBe(1);
  });
});

describe("eventGate gap 只由 stream 通道产生（11.2）", () => {
  it("P1 post 通道 seq 1→4 跳变：gap 恒 false，但仍推进水位到 4，重复键 apply=false", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", 1), "post")).toEqual({ apply: true, gap: false });
    expect(gate.ingest(seq("t2", 4), "post")).toEqual({ apply: true, gap: false });
    expect(gate.maxSeenSeq).toBe(4);
    // post 通道仍正常去重
    expect(gate.ingest(seq("t2", 4), "post")).toEqual({ apply: false, gap: false });
    expect(gate.ingest(seq("t2", 4), "stream")).toEqual({ apply: false, gap: false });
  });

  it("P2 stream 通道 1→4 跳变仍 gap=true（对照）", () => {
    const gate = createEventGate();
    gate.ingest(seq("t1", 1), "stream");
    expect(gate.ingest(seq("t2", 4), "stream")).toEqual({ apply: true, gap: true });
  });

  it("P3 post 推进水位后 stream 连续 seq 不报 gap；stream 再跳变才报", () => {
    const gate = createEventGate();
    // post 即时流自身跳变不报缺口
    gate.ingest(seq("t1", 1), "post");
    expect(gate.ingest(seq("t2", 4), "post").gap).toBe(false);
    // stream 从 post 水位之后连续到达：不报
    expect(gate.ingest(seq("t3", 5), "stream")).toEqual({ apply: true, gap: false });
    // stream 自己跳变（缺 6）才报
    expect(gate.ingest(seq("t4", 7), "stream")).toEqual({ apply: true, gap: true });
  });

  it("P4 post 大跳变建立高水位后，stream 首个滞后事件不乱报（首基线语义不被通道破坏）", () => {
    const gate = createEventGate();
    expect(gate.ingest(seq("t1", 100), "post")).toEqual({ apply: true, gap: false });
    expect(gate.maxSeenSeq).toBe(100);
    expect(gate.ingest(seq("t2", 50), "stream")).toEqual({ apply: true, gap: false });
  });
});

describe("eventGate seen 有界 LRU（11.3，容量 2000，按插入序淘汰）", () => {
  it("L1 灌入超容量个不同键：最旧键被淘汰可再次 apply，容量内键与最新键仍去重", () => {
    const gate = createEventGate();
    for (let i = 0; i < SEEN_LRU_CAPACITY; i++) {
      expect(gate.ingest(seq(`t${i}`, 1), "post").apply).toBe(true);
    }
    // 第 2001 个新键：放行并挤掉最旧的 t0
    expect(gate.ingest(seq(`t${SEEN_LRU_CAPACITY}`, 1), "post")).toEqual({
      apply: true,
      gap: false,
    });

    // 次旧 t1 仍在容量内：重复被拦截
    expect(gate.ingest(seq("t1", 1), "stream").apply).toBe(false);
    // 最新键自身重复：两通道均拦截
    expect(gate.ingest(seq(`t${SEEN_LRU_CAPACITY}`, 1), "post").apply).toBe(false);
    // 最旧 t0 已淘汰：旧事件再次到达按新事件放行（迟到乱序不重复报 gap）
    expect(gate.ingest(seq("t0", 1), "stream")).toEqual({ apply: true, gap: false });
    // t0 重新记忆后再次到达：拦截
    expect(gate.ingest(seq("t0", 1), "post").apply).toBe(false);
  });

  it("L2 重复命中不改变插入序淘汰语义：容量打满后最旧键照样淘汰", () => {
    const gate = createEventGate();
    for (let i = 0; i < SEEN_LRU_CAPACITY; i++) {
      gate.ingest(seq(`t${i}`, 1), "post");
    }
    // 对中间键反复重复命中（apply=false），不刷新其插入位置
    for (let k = 0; k < 5; k++) {
      expect(gate.ingest(seq("t1000", 1), "post").apply).toBe(false);
    }
    // 再来一个新键挤掉 t0
    gate.ingest(seq(`t${SEEN_LRU_CAPACITY}`, 1), "post");
    expect(gate.ingest(seq("t0", 1), "post").apply).toBe(true);
    // t1000 仍在容量内，继续被去重
    expect(gate.ingest(seq("t1000", 1), "post").apply).toBe(false);
  });

  it("L3 无 seq 防御分支不进 LRU、不占容量", () => {
    const gate = createEventGate();
    for (let i = 0; i < SEEN_LRU_CAPACITY + 10; i++) {
      gate.ingest(seq("post-only", undefined), "post");
    }
    // 全部容量仍留给有 seq 的键：第 2001 个有 seq 新键才开始淘汰
    for (let i = 0; i < SEEN_LRU_CAPACITY; i++) {
      expect(gate.ingest(seq(`s${i}`, 9), "post").apply).toBe(true);
    }
    expect(gate.ingest(seq("s0", 9), "stream").apply).toBe(false);
    gate.ingest(seq(`s${SEEN_LRU_CAPACITY}`, 9), "post");
    expect(gate.ingest(seq("s0", 9), "stream").apply).toBe(true);
  });
});
