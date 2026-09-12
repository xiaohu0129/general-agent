import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamChat } from "../api/chat";
import { createStreamConnection } from "../api/stream";
import type { ChatEvent } from "../api/types";
import * as sessionsApi from "../api/sessions";
import ChatPage from "./ChatPage";

vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
}));

const streamState = vi.hoisted(() => ({
  // 每次 createStreamConnection 调用记录：handles 被测试捕获用于手动推流
  connections: [] as Array<{
    sessionId: string;
    handlers: { onEvent: (ev: unknown) => void; onConnectionError?: (fatal: boolean) => void };
    close: ReturnType<typeof vi.fn>;
  }>,
}));

vi.mock("../api/stream", () => ({
  createStreamConnection: vi.fn((sessionId: string, handlers: unknown) => {
    const close = vi.fn();
    streamState.connections.push({
      sessionId,
      handlers: handlers as (typeof streamState.connections)[number]["handlers"],
      close,
    });
    return { close };
  }),
}));

vi.mock("../api/sessions", () => ({
  listSessions: vi.fn(),
  listMessages: vi.fn(),
  renameSession: vi.fn(),
  deleteSession: vi.fn(),
  artifactDownloadUrl: vi.fn(() => ""),
}));

vi.mock("../state/auth-context", () => ({
  useAuth: () => ({
    user: { uid: "u1", username: "tester" },
    logout: vi.fn(),
  }),
}));

const SID = "sess-1";
const TURN = "t-live";
const TRACE = "tr-1";

const mockedStreamChat = vi.mocked(streamChat);
const mockedCreateStream = vi.mocked(createStreamConnection);

function ev<T extends ChatEvent["event"]>(
  event: T,
  data: Extract<ChatEvent, { event: T }>["data"],
): ChatEvent {
  return { event, data } as ChatEvent;
}

async function renderAndOpen() {
  render(<ChatPage />);
  fireEvent.click(await screen.findByText("售后会话"));
  await waitFor(() => expect(mockedCreateStream).toHaveBeenCalled());
  return streamState.connections[streamState.connections.length - 1];
}

async function sendMessage(text: string) {
  const input = await screen.findByPlaceholderText(
    "输入消息，Enter 发送，Shift+Enter 换行",
  );
  fireEvent.change(input, { target: { value: text } });
  fireEvent.click(screen.getByTitle("发送"));
  await waitFor(() => expect(mockedStreamChat).toHaveBeenCalled());
}

function assistantBubbles() {
  return document.querySelectorAll(".bubble-assistant");
}

function lastAssistantText() {
  const bubbles = assistantBubbles();
  return bubbles[bubbles.length - 1]?.textContent ?? "";
}

describe("ChatPage /stream 持久通道与双通道合并", () => {
  beforeEach(() => {
    streamState.connections.length = 0;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.mocked(sessionsApi.listSessions).mockResolvedValue([
      { sessionId: SID, title: "售后会话", createdAt: null, updatedAt: null },
    ]);
    vi.mocked(sessionsApi.listMessages).mockResolvedValue({
      nextCursor: null,
      hasMore: false,
      messages: [],
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("停止生成后 /stream 补齐同轮 delta/clarify/turn_end：两段文本、卡片可点、streaming=false", async () => {
    mockedStreamChat.mockImplementation(
      async (_message, sessionId, handlers, signal) => {
        handlers.onEvent(
          ev("turn_start", {
            turnId: TURN,
            traceId: TRACE,
            sessionId: sessionId ?? undefined,
            eventSeq: 1,
          }),
        );
        handlers.onEvent(
          ev("turn_delta", { turnId: TURN, traceId: TRACE, content: "第一段", eventSeq: 2 }),
        );
        await new Promise((_resolve, reject) => {
          (signal as AbortSignal).addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        });
      },
    );

    const conn = await renderAndOpen();
    await sendMessage("继续");

    await screen.findByTitle("停止生成");
    await waitFor(() => expect(lastAssistantText()).toContain("第一段"));

    fireEvent.click(screen.getByTitle("停止生成"));
    await screen.findByTitle("发送");

    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: TURN, traceId: TRACE, content: "第二段", eventSeq: 3 }),
      );
      conn.handlers.onEvent(
        ev("clarify", {
          turnId: TURN,
          traceId: TRACE,
          question: "请选择",
          options: [{ label: "选项A", value: "a" }],
          eventSeq: 4,
        }),
      );
      conn.handlers.onEvent(
        ev("turn_end", { turnId: TURN, traceId: TRACE, finishReason: "stop", eventSeq: 5 }),
      );
    });

    await waitFor(() => expect(lastAssistantText()).toContain("第一段第二段"));
    const optionBtn = await screen.findByRole("button", { name: "选项A" });
    expect(optionBtn).not.toBeDisabled();
    // streaming 已收尾：Composer 回到发送态
    expect(screen.queryByTitle("停止生成")).toBeNull();
    expect(screen.getByTitle("发送")).toBeInTheDocument();
  });

  it("双通道幂等：同一 (turnId,eventSeq) 先 post 后 stream，文本不重复", async () => {
    mockedStreamChat.mockImplementation(async (_message, _sid, handlers) => {
      handlers.onEvent(ev("turn_start", { turnId: TURN, traceId: TRACE, eventSeq: 1 }));
      handlers.onEvent(
        ev("turn_delta", { turnId: TURN, traceId: TRACE, content: "X", eventSeq: 2 }),
      );
      handlers.onEvent(
        ev("turn_end", { turnId: TURN, traceId: TRACE, finishReason: "stop", eventSeq: 3 }),
      );
    });

    const conn = await renderAndOpen();
    await sendMessage("你好");
    await screen.findByTitle("发送");
    expect(lastAssistantText()).toContain("X");

    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: TURN, traceId: TRACE, content: "Y", eventSeq: 2 }),
      );
    });

    const text2 = lastAssistantText();
    expect(text2).toContain("X");
    expect(text2).not.toContain("Y");
  });

  it("未知 turnId 的 stream 事件不新增气泡", async () => {
    const conn = await renderAndOpen();
    expect(assistantBubbles()).toHaveLength(0);

    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", {
          turnId: "t-does-not-exist",
          traceId: TRACE,
          content: "野事件",
          eventSeq: 900,
        }),
      );
    });

    expect(assistantBubbles()).toHaveLength(0);
  });

  it("notification（无 turnId 带 seq）：消息列表不变、不报错", async () => {
    const conn = await renderAndOpen();
    expect(assistantBubbles()).toHaveLength(0);

    expect(() =>
      act(() => {
        conn.handlers.onEvent(
          ev("notification", { taskId: "tk-1", status: "done", eventSeq: 901 }),
        );
      }),
    ).not.toThrow();

    expect(assistantBubbles()).toHaveLength(0);
  });

  it("fatal 连接降级后 POST 路径仍可正常发送一轮", async () => {
    const conn = await renderAndOpen();

    act(() => {
      conn.handlers.onConnectionError?.(true);
    });

    mockedStreamChat.mockImplementation(async () => {});
    await sendMessage("降级后发送");
    expect(mockedStreamChat).toHaveBeenCalledTimes(1);
  });

  it("StrictMode 双挂载：建连两次且首个连接被 cleanup close，最终保留一条活动连接", async () => {
    render(
      <StrictMode>
        <ChatPage />
      </StrictMode>,
    );
    fireEvent.click(await screen.findByText("售后会话"));

    await waitFor(() => expect(mockedCreateStream).toHaveBeenCalledTimes(2));
    expect(streamState.connections).toHaveLength(2);
    expect(streamState.connections[0].sessionId).toBe(SID);
    expect(streamState.connections[0].close).toHaveBeenCalledTimes(1);
    expect(streamState.connections[1].close).not.toHaveBeenCalled();
  });
});
