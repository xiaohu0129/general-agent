import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamChat } from "../api/chat";
import { ApiError } from "../api/client";
import { createStreamConnection } from "../api/stream";
import type { ChatEvent, HistoryMessage, MessagePage } from "../api/types";
import * as sessionsApi from "../api/sessions";
import ChatPage from "./ChatPage";

vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
}));

const authState = vi.hoisted(() => ({ logout: vi.fn() }));

const streamState = vi.hoisted(() => ({
  // 每次 createStreamConnection 调用记录：handles 被测试捕获用于手动推流
  connections: [] as Array<{
    sessionId: string;
    handlers: { onEvent: (ev: unknown) => void };
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
    logout: authState.logout,
  }),
}));

const SID = "sess-1";
const SID2 = "sess-2";
const TURN = "t-live";
const TRACE = "tr-1";
const GAP_NOTICE = "事件已过期，已刷新";

const mockedStreamChat = vi.mocked(streamChat);
const mockedCreateStream = vi.mocked(createStreamConnection);
const mockedListMessages = vi.mocked(sessionsApi.listMessages);

function ev<T extends ChatEvent["event"]>(
  event: T,
  data: Extract<ChatEvent, { event: T }>["data"],
): ChatEvent {
  return { event, data } as ChatEvent;
}

function row(overrides: Partial<HistoryMessage> & Pick<HistoryMessage, "turnId" | "role">): HistoryMessage {
  return { content: "", createdAt: null, ...overrides };
}

function page(messages: HistoryMessage[]): MessagePage {
  return { nextCursor: null, hasMore: false, messages };
}

async function openSession() {
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

function userBubbles() {
  return document.querySelectorAll(".bubble-user");
}

function assistantBubbles() {
  return document.querySelectorAll(".bubble-assistant");
}

function lastAssistantText() {
  const bubbles = assistantBubbles();
  return bubbles[bubbles.length - 1]?.textContent ?? "";
}

describe("ChatPage 会话级缺口检测与静默重拉合并", () => {
  beforeEach(() => {
    streamState.connections.length = 0;
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.mocked(sessionsApi.listSessions).mockResolvedValue([
      { sessionId: SID, title: "售后会话", createdAt: null, updatedAt: null },
    ]);
    mockedListMessages.mockResolvedValue(page([]));
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("空闲缺口：seq 跳变触发一次静默重拉，消息合并为历史形态，提示条出现；连续缺口只拉一次", async () => {
    const R1 = [
      row({ messageId: 1, turnId: "t-old", role: "user", content: "老问题" }),
      row({ messageId: 2, turnId: "t-old", role: "assistant", content: "旧回复" }),
    ];
    const R2 = [
      row({ messageId: 1, turnId: "t-old", role: "user", content: "老问题" }),
      row({ messageId: 2, turnId: "t-old", role: "assistant", content: "刷新后回复" }),
      row({ messageId: 3, turnId: "t-new", role: "user", content: "新问题" }),
      row({ messageId: 4, turnId: "t-new", role: "assistant", content: "新回复" }),
    ];
    mockedListMessages.mockResolvedValueOnce(page(R1)).mockResolvedValueOnce(page(R2));

    const conn = await openSession();
    expect(await screen.findByText("旧回复")).toBeInTheDocument();
    expect(mockedListMessages).toHaveBeenCalledTimes(1);

    // 首事件建水位（seq=10，未知 turnId 不渲染）
    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-a", traceId: TRACE, content: "x", eventSeq: 10 }),
      );
    });

    // 连续两个缺口（13、16）：只允许触发一次重拉（gapReloadingRef 幂等守卫）
    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-b", traceId: TRACE, content: "y", eventSeq: 13 }),
      );
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-c", traceId: TRACE, content: "z", eventSeq: 16 }),
      );
    });

    await waitFor(() => expect(mockedListMessages).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(GAP_NOTICE)).toBeInTheDocument();
    expect(screen.getByText("刷新后回复")).toBeInTheDocument();
    expect(screen.getByText("新问题")).toBeInTheDocument();
    expect(screen.queryByText("旧回复")).toBeNull();

    // 守卫期过去后也不得补发第三次请求
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(mockedListMessages).toHaveBeenCalledTimes(2);
  });

  it("活动轮中缺口延后到 turn_end：轮中不请求、live 气泡保留；落库后重拉转正、无重复用户气泡", async () => {
    mockedListMessages.mockResolvedValueOnce(page([])); // R1：空历史的新轮会话
    let deliverEnd!: () => void;
    mockedStreamChat.mockImplementation(async (_message, sessionId, handlers) => {
      handlers.onEvent(
        ev("turn_start", {
          turnId: TURN,
          traceId: TRACE,
          sessionId: sessionId ?? undefined,
          eventSeq: 20,
        }),
      );
      handlers.onEvent(
        ev("turn_delta", { turnId: TURN, traceId: TRACE, content: "回答中", eventSeq: 21 }),
      );
      await new Promise<void>((resolve) => {
        deliverEnd = () => {
          handlers.onEvent(
            ev("turn_end", { turnId: TURN, traceId: TRACE, finishReason: "stop", eventSeq: 25 }),
          );
          resolve();
        };
      });
    });

    const conn = await openSession();
    await sendMessage("你好");
    await screen.findByTitle("停止生成");
    await waitFor(() => expect(lastAssistantText()).toContain("回答中"));

    // 轮中 stream 推来缺口（seq 24，未知 turnId）：仅标记 pending，不请求
    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-gap", traceId: TRACE, content: "野", eventSeq: 24 }),
      );
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedListMessages).toHaveBeenCalledTimes(1);
    expect(userBubbles()).toHaveLength(1);
    expect(assistantBubbles()).toHaveLength(1);
    expect(screen.queryByText(GAP_NOTICE)).toBeNull();

    // turn_end 前历史已落库：R2 含同一 turnId 的 user+assistant 两行，assistant 带 selected
    const R2 = [
      row({ messageId: 3, turnId: TURN, role: "user", content: "你好" }),
      row({
        messageId: 4,
        turnId: TURN,
        role: "assistant",
        content: "历史权威回答",
        options: [{ label: "选项A", value: "a" }],
        selected: "a",
      }),
    ];
    mockedListMessages.mockResolvedValueOnce(page(R2));

    await act(async () => {
      deliverEnd();
      await Promise.resolve();
    });

    await waitFor(() => expect(mockedListMessages).toHaveBeenCalledTimes(2));
    // live 对（user+assistant）按 turnId 整体被 restored 转正：无重复
    expect(userBubbles()).toHaveLength(1);
    expect(assistantBubbles()).toHaveLength(1);
    expect(lastAssistantText()).toContain("历史权威回答");
    expect(lastAssistantText()).not.toContain("回答中");
    const optionBtn = await screen.findByRole("button", { name: "选项A" });
    expect(optionBtn).toBeDisabled();
    expect(optionBtn).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(GAP_NOTICE)).toBeInTheDocument();
    await screen.findByTitle("发送"); // streaming 正常收尾
  });

  it("首连不误报：新 gate 首事件即 seq=99 不触发重拉、无提示条", async () => {
    mockedListMessages.mockResolvedValueOnce(page([]));
    const conn = await openSession();

    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-x", traceId: TRACE, content: "野", eventSeq: 99 }),
      );
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(mockedListMessages).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(GAP_NOTICE)).toBeNull();
  });

  it("notification 占 seq：10→notification(11)→12 连续不触发重拉", async () => {
    mockedListMessages.mockResolvedValueOnce(page([]));
    const conn = await openSession();

    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-a", traceId: TRACE, content: "", eventSeq: 10 }),
      );
      conn.handlers.onEvent(
        ev("notification", { taskId: "tk-1", status: "done", eventSeq: 11 }),
      );
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-b", traceId: TRACE, content: "", eventSeq: 12 }),
      );
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(mockedListMessages).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(GAP_NOTICE)).toBeNull();
  });

  // M1：重拉在途守卫期间活动轮新挂账，不得被前一轮 finally 吞掉。
  it("M1 重拉在途期间活动轮新挂账：首轮 resolve 后必须再拉一轮并合并第二页", async () => {
    const conn = await openSession();
    expect(mockedListMessages).toHaveBeenCalledTimes(1);

    const R2 = [
      row({ messageId: 1, turnId: "t-old", role: "user", content: "老问题" }),
      row({ messageId: 2, turnId: "t-old", role: "assistant", content: "首轮刷新" }),
    ];
    const R3 = [
      ...R2,
      row({ messageId: 3, turnId: TURN, role: "user", content: "在途提问" }),
      row({ messageId: 4, turnId: TURN, role: "assistant", content: "飞行后新回复" }),
    ];
    let resolveFirst!: (p: MessagePage) => void;
    const firstInFlight = new Promise<MessagePage>((resolve) => {
      resolveFirst = resolve;
    });
    mockedListMessages
      .mockImplementationOnce(() => firstInFlight)
      .mockResolvedValueOnce(page(R3));

    // 空闲缺口（10 建水位 → 13 跳变）触发第 1 次重拉（在途）
    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-a", traceId: TRACE, content: "x", eventSeq: 10 }),
      );
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-b", traceId: TRACE, content: "y", eventSeq: 13 }),
      );
    });
    await waitFor(() => expect(mockedListMessages).toHaveBeenCalledTimes(2));

    // 在途期间发起活动轮：post 14/15，stream 缺口 18 挂账，post turn_end 19 补拉被守卫挡回
    let deliverEnd!: () => void;
    mockedStreamChat.mockImplementationOnce(async (_message, sessionId, handlers) => {
      handlers.onEvent(
        ev("turn_start", {
          turnId: TURN,
          traceId: TRACE,
          sessionId: sessionId ?? undefined,
          eventSeq: 14,
        }),
      );
      handlers.onEvent(
        ev("turn_delta", { turnId: TURN, traceId: TRACE, content: "在途回答", eventSeq: 15 }),
      );
      await new Promise<void>((resolve) => {
        deliverEnd = () => {
          handlers.onEvent(
            ev("turn_end", { turnId: TURN, traceId: TRACE, finishReason: "stop", eventSeq: 19 }),
          );
          resolve();
        };
      });
    });

    await sendMessage("在途提问");
    await screen.findByTitle("停止生成");
    await waitFor(() => expect(lastAssistantText()).toContain("在途回答"));

    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-gap", traceId: TRACE, content: "野", eventSeq: 18 }),
      );
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedListMessages).toHaveBeenCalledTimes(2);

    await act(async () => {
      deliverEnd();
      await Promise.resolve();
    });
    await screen.findByTitle("发送");
    // 守卫在途：turn_end 补拉被 early-return，仍无新调用
    expect(mockedListMessages).toHaveBeenCalledTimes(2);

    // 首轮 resolve：飞行期间有同 sid 新挂账 → 必须再执行一轮
    await act(async () => {
      resolveFirst(page(R2));
      await Promise.resolve();
    });
    await waitFor(() => expect(mockedListMessages).toHaveBeenCalledTimes(3));
    expect(screen.getByText("首轮刷新")).toBeInTheDocument();
    expect(screen.getByText("飞行后新回复")).toBeInTheDocument();
    expect(screen.getByText(GAP_NOTICE)).toBeInTheDocument();
  });

  // M2：POST 在终止帧前网络异常，缺口挂账不得跨轮残留。
  it("M2 POST 无终止帧网络异常后挂账不跨轮：下一轮正常收尾无多余重拉、无不实提示", async () => {
    const conn = await openSession();

    let failNow!: () => void;
    mockedStreamChat.mockImplementationOnce(async (_message, sid, handlers) => {
      handlers.onEvent(
        ev("turn_start", {
          turnId: TURN,
          traceId: TRACE,
          sessionId: sid ?? undefined,
          eventSeq: 1,
        }),
      );
      handlers.onEvent(
        ev("turn_delta", { turnId: TURN, traceId: TRACE, content: "回答半截", eventSeq: 2 }),
      );
      await new Promise((_resolve, reject) => {
        failNow = () => reject(new TypeError("Failed to fetch"));
      });
    });

    await sendMessage("第一轮");
    await screen.findByTitle("停止生成");
    // 活动轮中 stream 制造缺口挂账
    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-gap", traceId: TRACE, content: "野", eventSeq: 5 }),
      );
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedListMessages).toHaveBeenCalledTimes(1);

    // POST 在终止帧前以网络错误 reject（非 abort）
    await act(async () => {
      failNow();
      await Promise.resolve();
    });
    await waitFor(() => expect(lastAssistantText()).toContain("网络错误，请稍后重试"));
    await screen.findByTitle("发送");

    // 再走一轮正常轮（seq 6/7/8 与水位 5 连续，无新缺口）至 turn_end
    mockedStreamChat.mockImplementationOnce(async (_message, sid, handlers) => {
      handlers.onEvent(
        ev("turn_start", {
          turnId: "t-2",
          traceId: TRACE,
          sessionId: sid ?? undefined,
          eventSeq: 6,
        }),
      );
      handlers.onEvent(
        ev("turn_delta", { turnId: "t-2", traceId: TRACE, content: "第二轮回答", eventSeq: 7 }),
      );
      handlers.onEvent(
        ev("turn_end", { turnId: "t-2", traceId: TRACE, finishReason: "stop", eventSeq: 8 }),
      );
    });
    await sendMessage("第二轮");
    await screen.findByTitle("发送");
    expect(screen.getByText("第二轮回答")).toBeInTheDocument();

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(mockedListMessages).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(GAP_NOTICE)).toBeNull();
  });

  // M3a：活动轮 error 终止的延后重拉路径。
  it("M3a 活动轮缺口挂账后业务 error 经 post 到达：气泡 error 态并触发一次静默重拉", async () => {
    const conn = await openSession();

    let deliverError!: () => void;
    mockedStreamChat.mockImplementationOnce(async (_message, sid, handlers) => {
      handlers.onEvent(
        ev("turn_start", {
          turnId: TURN,
          traceId: TRACE,
          sessionId: sid ?? undefined,
          eventSeq: 1,
        }),
      );
      handlers.onEvent(
        ev("turn_delta", { turnId: TURN, traceId: TRACE, content: "回答中", eventSeq: 2 }),
      );
      await new Promise<void>((resolve) => {
        deliverError = () => {
          handlers.onEvent(
            ev("error", {
              turnId: TURN,
              traceId: TRACE,
              message: "业务出错了",
              code: "X",
              eventSeq: 5,
            }),
          );
          resolve();
        };
      });
    });

    await sendMessage("提问");
    await screen.findByTitle("停止生成");
    await waitFor(() => expect(lastAssistantText()).toContain("回答中"));

    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-gap", traceId: TRACE, content: "野", eventSeq: 4 }),
      );
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedListMessages).toHaveBeenCalledTimes(1);

    await act(async () => {
      deliverError();
      await Promise.resolve();
    });
    await waitFor(() => expect(mockedListMessages).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(lastAssistantText()).toContain("业务出错了"));
    await screen.findByTitle("发送");
  });

  // M3b：缺口挂账后切换会话，旧 sid 不得再被重拉。
  it("M3b 缺口挂账后切换到另一会话：不触发旧 sid 重拉，迟到事件也不补发", async () => {
    vi.mocked(sessionsApi.listSessions).mockResolvedValue([
      { sessionId: SID, title: "售后会话", createdAt: null, updatedAt: null },
      { sessionId: SID2, title: "第二个会话", createdAt: null, updatedAt: null },
    ]);
    const conn = await openSession();

    mockedStreamChat.mockImplementationOnce(
      async (_message, sid, handlers, signal) => {
        handlers.onEvent(
          ev("turn_start", {
            turnId: TURN,
            traceId: TRACE,
            sessionId: sid ?? undefined,
            eventSeq: 1,
          }),
        );
        handlers.onEvent(
          ev("turn_delta", { turnId: TURN, traceId: TRACE, content: "回答中", eventSeq: 2 }),
        );
        await new Promise((_resolve, reject) => {
          (signal as AbortSignal).addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        });
      },
    );
    await sendMessage("提问");
    await screen.findByTitle("停止生成");

    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-gap", traceId: TRACE, content: "野", eventSeq: 9 }),
      );
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockedListMessages).toHaveBeenCalledTimes(1);

    mockedListMessages.mockResolvedValueOnce(page([]));
    fireEvent.click(screen.getByText("第二个会话"));
    await waitFor(() => expect(mockedCreateStream).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(mockedListMessages).toHaveBeenCalledTimes(2));
    expect(mockedListMessages.mock.calls[1]?.[0]).toBe(SID2);

    // 旧连接迟到 turn_end + 缺口事件：当前会话已切走，不得补发
    await act(async () => {
      conn.handlers.onEvent(
        ev("turn_end", { turnId: TURN, traceId: TRACE, finishReason: "stop", eventSeq: 10 }),
      );
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-x", traceId: TRACE, content: "野2", eventSeq: 13 }),
      );
      await Promise.resolve();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(mockedListMessages).toHaveBeenCalledTimes(2);
    const sidCalls = mockedListMessages.mock.calls.filter((c) => c[0] === SID).length;
    expect(sidCalls).toBe(1);
    expect(screen.queryByText(GAP_NOTICE)).toBeNull();
  });

  // M3c：静默重拉 401 → logout。
  it("M3c 静默重拉 reject ApiError 401：调用 logout，不抛错、无提示条", async () => {
    const conn = await openSession();
    mockedListMessages.mockRejectedValueOnce(new ApiError(401, "AUTH_REQUIRED", "未登录"));

    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-a", traceId: TRACE, content: "x", eventSeq: 10 }),
      );
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-b", traceId: TRACE, content: "y", eventSeq: 13 }),
      );
    });

    await waitFor(() => expect(authState.logout).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(GAP_NOTICE)).toBeNull();
  });

  // M3d：重拉页 hasMore/nextCursor 更新“加载更早”分页状态。
  it("M3d 静默重拉页带 hasMore/nextCursor：按钮出现且按游标翻页，末页后按钮消失", async () => {
    const conn = await openSession();
    mockedListMessages.mockResolvedValueOnce({
      messages: [
        row({ messageId: 5, turnId: "t-new", role: "assistant", content: "刷新内容" }),
      ],
      nextCursor: 100,
      hasMore: true,
    });

    act(() => {
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-a", traceId: TRACE, content: "x", eventSeq: 10 }),
      );
      conn.handlers.onEvent(
        ev("turn_delta", { turnId: "t-b", traceId: TRACE, content: "y", eventSeq: 13 }),
      );
    });

    const loadBtn = await screen.findByRole("button", { name: "加载更早的消息" });
    expect(screen.getByText("刷新内容")).toBeInTheDocument();

    mockedListMessages.mockResolvedValueOnce({
      messages: [
        row({ messageId: 1, turnId: "t-old", role: "assistant", content: "更早的消息" }),
      ],
      nextCursor: null,
      hasMore: false,
    });
    fireEvent.click(loadBtn);

    await waitFor(() => expect(screen.getByText("更早的消息")).toBeInTheDocument());
    const cursorCall = mockedListMessages.mock.calls.find((c) => c[1] !== undefined);
    expect(cursorCall).toEqual([SID, { before: 100 }]);
    expect(screen.queryByRole("button", { name: "加载更早的消息" })).toBeNull();
  });
});
