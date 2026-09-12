import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createStreamConnection, type StreamHandlers } from "./stream";
import type { ChatEvent } from "./types";

// 记录型假 EventSource：模拟浏览器真实派发语义——
// 命名事件只进按名注册的 listener（绝不走 onmessage）；
// 业务 error 是带 data 的 MessageEvent（进 onerror），连接错误是无 data 普通 Event。
interface FakeInstance {
  url: string;
  readyState: number;
  listeners: Map<string, (ev: Event) => void>;
  onmessage: ((ev: MessageEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  close: ReturnType<typeof vi.fn>;
}

const instances: FakeInstance[] = [];

class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  url: string;
  readyState = FakeEventSource.CONNECTING;
  listeners = new Map<string, (ev: Event) => void>();
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  close = vi.fn();
  constructor(url: string) {
    this.url = url;
    instances.push(this);
  }
  addEventListener(type: string, listener: (ev: Event) => void) {
    this.listeners.set(type, listener);
  }
}

function dispatchNamed(es: FakeInstance, name: string, data: string) {
  const listener = es.listeners.get(name);
  if (!listener) throw new Error(`未注册命名 listener: ${name}`);
  listener.call(es, new MessageEvent(name, { data }));
}

function fireError(es: FakeInstance, ev: Event) {
  if (!es.onerror) throw new Error("未设置 onerror");
  es.onerror.call(es, ev);
}

const SEVEN_NAMES = [
  "turn_start",
  "turn_delta",
  "turn_end",
  "tool_start",
  "tool_end",
  "clarify",
  "notification",
] as const;

function connect(sessionId: string) {
  const received: ChatEvent[] = [];
  const fatals: boolean[] = [];
  const handlers: StreamHandlers = {
    onEvent: (ev) => received.push(ev),
    onConnectionError: (fatal) => fatals.push(fatal),
  };
  const conn = createStreamConnection(sessionId, handlers);
  return { conn, received, fatals };
}

beforeEach(() => {
  instances.length = 0;
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createStreamConnection", () => {
  it("URL 为同源 /stream 且 sessionId 经 encodeURIComponent", () => {
    connect("s a&b=c/中");
    expect(instances).toHaveLength(1);
    expect(instances[0].url).toBe(`/stream?sessionId=${encodeURIComponent("s a&b=c/中")}`);
  });

  it("七个命名事件各注册按名 listener，且不使用 onmessage", () => {
    connect("s1");
    const es = instances[0];
    for (const name of SEVEN_NAMES) {
      expect(es.listeners.has(name)).toBe(true);
    }
    expect(es.listeners.has("error")).toBe(false);
    expect(es.onmessage).toBeNull();
  });

  it.each(SEVEN_NAMES.map((name) => [name]))(
    "命名帧 %s 解析后以 {event,data} 交 onEvent（不经 onmessage）",
    (name) => {
      const { received } = connect("s1");
      const es = instances[0];
      const payload = name === "notification" ? { taskId: "tk", status: "done" } : { turnId: "t1" };
      dispatchNamed(es, name, JSON.stringify(payload));
      expect(received).toEqual([{ event: name, data: payload }]);
      expect(es.onmessage).toBeNull();
    },
  );

  it("业务 error：onerror 收到带 data 的 MessageEvent 时恰好交一次 error 业务事件，不报连接错误", () => {
    const { received, fatals } = connect("s1");
    const es = instances[0];
    es.readyState = FakeEventSource.OPEN;
    const errorData = { turnId: "t1", traceId: "tr", message: "boom", code: "X" };
    fireError(es, new MessageEvent("error", { data: JSON.stringify(errorData) }));
    expect(received).toEqual([{ event: "error", data: errorData }]);
    expect(fatals).toEqual([]);
    expect(es.close).not.toHaveBeenCalled();
  });

  it("连接 error CLOSED：普通 Event + readyState=2 回调 fatal=true 且主动 close", () => {
    const { received, fatals } = connect("s1");
    const es = instances[0];
    es.readyState = FakeEventSource.CLOSED;
    fireError(es, new Event("error"));
    expect(fatals).toEqual([true]);
    expect(received).toEqual([]);
    expect(es.close).toHaveBeenCalledTimes(1);
  });

  it("连接 error CONNECTING：普通 Event + readyState=0 回调 fatal=false 且不 close（浏览器自动重连）", () => {
    const { received, fatals } = connect("s1");
    const es = instances[0];
    es.readyState = FakeEventSource.CONNECTING;
    fireError(es, new Event("error"));
    expect(fatals).toEqual([false]);
    expect(received).toEqual([]);
    expect(es.close).not.toHaveBeenCalled();
  });

  it("命名帧畸形 JSON：静默忽略，不抛、不回调", () => {
    const { received, fatals } = connect("s1");
    const es = instances[0];
    expect(() => dispatchNamed(es, "turn_delta", "{not json")).not.toThrow();
    expect(received).toEqual([]);
    expect(fatals).toEqual([]);
  });

  // 帧形状 hardening：JSON.parse 成功但 data 不是非 null 对象时，与畸形 JSON
  // 一样静默忽略（命名帧 event 由监听名决定，{} 无 event 字段不受影响）。
  it.each([
    ["null", "null"],
    ["数字", "5"],
    ["字符串", '"x"'],
  ])("命名帧 data 解析为非对象（%s）：静默忽略，不投递、不报连接错误", (_label, json) => {
    const { received, fatals } = connect("s1");
    const es = instances[0];
    expect(() => dispatchNamed(es, "turn_delta", json)).not.toThrow();
    expect(received).toEqual([]);
    expect(fatals).toEqual([]);
  });

  // onerror 业务 error 分流 hardening：parse 成功但 data 非对象时不得当业务 error
  // 投递，落入连接 error 分支（CONNECTING→false；CLOSED→true 且 close）。
  it.each([
    ["null", "null"],
    ["数字", "5"],
    ["字符串", '"x"'],
  ])(
    "onerror MessageEvent data 为非对象 JSON（%s）：按连接 error 分流，不投递业务 error",
    (_label, json) => {
      const connecting = connect("s1");
      const es0 = instances[0];
      es0.readyState = FakeEventSource.CONNECTING;
      fireError(es0, new MessageEvent("error", { data: json }));
      expect(connecting.received).toEqual([]);
      expect(connecting.fatals).toEqual([false]);
      expect(es0.close).not.toHaveBeenCalled();

      const closed = connect("s2");
      const es2 = instances[1];
      es2.readyState = FakeEventSource.CLOSED;
      fireError(es2, new MessageEvent("error", { data: json }));
      expect(closed.received).toEqual([]);
      expect(closed.fatals).toEqual([true]);
      expect(es2.close).toHaveBeenCalledTimes(1);
    },
  );

  it("连接关闭：返回对象 close() 调用底层 EventSource.close", () => {
    const { conn } = connect("s1");
    const es = instances[0];
    conn.close();
    expect(es.close).toHaveBeenCalledTimes(1);
  });
});
