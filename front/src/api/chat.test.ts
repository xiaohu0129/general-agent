import { afterEach, describe, expect, it, vi } from "vitest";
import { streamChat } from "./chat";
import type { ChatEvent } from "./types";

const enc = new TextEncoder();

type Sep = "\r\n" | "\n" | "\r";

function frame(id: string, event: string, data: unknown, sep: Sep): string {
  return `id: ${id}${sep}event: ${event}${sep}data: ${JSON.stringify(data)}${sep}${sep}`;
}

function byteChunks(raw: string, chunkSize: number): Uint8Array[] {
  const bytes = enc.encode(raw);
  const chunks: Uint8Array[] = [];
  for (let i = 0; i < bytes.length; i += chunkSize) {
    chunks.push(bytes.slice(i, i + chunkSize));
  }
  return chunks;
}

async function collect(chunks: Uint8Array[]): Promise<ChatEvent[]> {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(chunk);
      }
      controller.close();
    },
  });
  globalThis.fetch = vi.fn(async () => new Response(stream, { status: 200 })) as unknown as
    typeof fetch;

  const events: ChatEvent[] = [];
  await streamChat("hi", null, { onEvent: (ev) => events.push(ev) });
  return events;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("streamChat clarify_selection 请求体", () => {
  async function callWith(
    clarifySelection: string | undefined
  ): Promise<{ body: Record<string, unknown> }> {
    const emptyStream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.close();
      },
    });
    const fetchMock = vi.fn<typeof fetch>(async () => new Response(emptyStream, { status: 200 }));
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    await streamChat(
      "退款",
      "sess-1",
      { onEvent: () => {} },
      undefined,
      clarifySelection
    );

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    return { body: JSON.parse(String(init.body)) };
  }

  it("clarifySelection 为非空字符串时请求体携带 clarify_selection:{value}", async () => {
    const { body } = await callWith("category:refund");

    expect(body).toEqual({
      message: "退款",
      sessionId: "sess-1",
      clarify_selection: { value: "category:refund" },
    });
  });

  it("缺省 / null / 空串时请求体不含 clarify_selection 键", async () => {
    const { body: bodyUndef } = await callWith(undefined);
    expect(bodyUndef).toEqual({ message: "退款", sessionId: "sess-1" });
    expect("clarify_selection" in (bodyUndef as object)).toBe(false);

    const { body: bodyNull } = await callWith(null as unknown as string | undefined);
    expect("clarify_selection" in (bodyNull as object)).toBe(false);

    const { body: bodyEmpty } = await callWith("");
    expect("clarify_selection" in (bodyEmpty as object)).toBe(false);
  });
});

describe("streamChat SSE 分块解析", () => {
  it("CRLF 分隔（sse-starlette 默认）：单 chunk 多帧逐帧派发并解析 id", async () => {
    const raw =
      frame("7", "turn_delta", { content: "a" }, "\r\n") +
      frame("8", "turn_end", { finishReason: "stop" }, "\r\n");

    const events = await collect([enc.encode(raw)]);

    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({
      event: "turn_delta",
      id: "7",
      data: { content: "a" },
    });
    expect(events[1]).toMatchObject({
      event: "turn_end",
      id: "8",
      data: { finishReason: "stop" },
    });
  });

  it("LF 分隔：单 chunk 多帧逐帧派发", async () => {
    const raw =
      frame("1", "turn_start", { turnId: "t1" }, "\n") +
      frame("2", "turn_delta", { content: "b" }, "\n");

    const events = await collect([enc.encode(raw)]);

    expect(events).toHaveLength(2);
    expect(events[0].event).toBe("turn_start");
    expect(events[0].id).toBe("1");
    expect(events[1].event).toBe("turn_delta");
    expect(events[1].id).toBe("2");
  });

  it("CR 分隔：单 chunk 多帧逐帧派发", async () => {
    const raw =
      frame("3", "turn_start", { turnId: "t3" }, "\r") +
      frame("4", "turn_delta", { content: "c" }, "\r");

    const events = await collect([enc.encode(raw)]);

    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({ event: "turn_start", id: "3" });
    expect(events[1]).toMatchObject({ event: "turn_delta", id: "4", data: { content: "c" } });
  });

  it("单帧跨多个 chunk（LF，逐字节小块）仍完整解析", async () => {
    const events = await collect(byteChunks(frame("5", "turn_delta", { content: "xy" }, "\n"), 4));

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      event: "turn_delta",
      id: "5",
      data: { content: "xy" },
    });
  });

  it("CRLF 帧跨 chunk 且 CJK 多字节字符从字节中间劈开", async () => {
    const raw = frame("9", "turn_delta", { content: "中" }, "\r\n");
    const bytes = enc.encode(raw);
    // “中” = E4 B8 AD，在其首字节 E4 之后劈开，强制走 TextDecoder stream 续接
    const prefixLen = enc.encode('id: 9\r\nevent: turn_delta\r\ndata: {"content":"').length + 1;

    const events = await collect([bytes.slice(0, prefixLen), bytes.slice(prefixLen)]);

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      event: "turn_delta",
      id: "9",
      data: { content: "中" },
    });
  });

  it("心跳注释行（: heartbeat）不回调、不报错，夹在帧间被忽略", async () => {
    const raw =
      ": heartbeat\r\n\r\n" +
      frame("10", "turn_start", { turnId: "t10" }, "\r\n") +
      ": heartbeat\n\n" +
      frame("11", "turn_end", { finishReason: "stop" }, "\r\n");

    const events = await collect([enc.encode(raw)]);

    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({ event: "turn_start", id: "10" });
    expect(events[1]).toMatchObject({ event: "turn_end", id: "11" });
  });
});
