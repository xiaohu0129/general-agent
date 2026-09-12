import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamChat } from "../api/chat";
import type { ChatEvent } from "../api/types";
import * as sessionsApi from "../api/sessions";
import ChatPage from "./ChatPage";

vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
}));

vi.mock("../api/stream", () => ({
  createStreamConnection: vi.fn(() => ({ close: vi.fn() })),
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

const mockedStreamChat = vi.mocked(streamChat);

function emitLifecycle(handlers: { onEvent: (ev: ChatEvent) => void }, sessionId: string) {
  handlers.onEvent({
    event: "turn_start",
    data: { turnId: "t-live", traceId: "tr-1", sessionId },
  });
  handlers.onEvent({
    event: "turn_end",
    data: { turnId: "t-live", traceId: "tr-1", finishReason: "stop" },
  });
}

describe("ChatPage 澄清点选回发", () => {
  beforeEach(() => {
    mockedStreamChat.mockImplementation(
      async (_message, _sessionId, handlers, _signal, _clarifySelection) => {
        emitLifecycle(handlers, SID);
      },
    );
    vi.mocked(sessionsApi.listSessions).mockResolvedValue([
      { sessionId: SID, title: "售后会话", createdAt: null, updatedAt: null },
    ]);
    vi.mocked(sessionsApi.listMessages).mockResolvedValue({
      nextCursor: null,
      hasMore: false,
      messages: [
        { messageId: 1, turnId: "t-a", role: "user", content: "我要退款", createdAt: null },
        {
          messageId: 2,
          turnId: "t-a",
          role: "assistant",
          content: "请选择退款类型",
          createdAt: null,
          options: [
            { label: "退款", value: "category:refund" },
            { label: "投诉", value: "category:complaint" },
          ],
          selected: null,
        },
        { messageId: 3, turnId: "t-b", role: "user", content: "物流到哪了", createdAt: null },
        {
          messageId: 4,
          turnId: "t-b",
          role: "assistant",
          content: "物流问题请选择",
          createdAt: null,
          options: [{ label: "物流进度", value: "topic:logistics" }],
          selected: null,
        },
      ],
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("点选路径：streamChat 以 (label, sid, handlers, signal, value) 调用，卡片乐观已选且全禁用", async () => {
    render(<ChatPage />);

    fireEvent.click(await screen.findByText("售后会话"));
    const optionBtn = await screen.findByRole("button", { name: "退款" });
    expect(optionBtn).not.toBeDisabled();

    fireEvent.click(optionBtn);

    await waitFor(() => expect(mockedStreamChat).toHaveBeenCalled());
    expect(mockedStreamChat).toHaveBeenCalledWith(
      "退款",
      SID,
      expect.anything(),
      expect.any(AbortSignal),
      "category:refund",
    );

    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "退款" });
      expect(btn).toBeDisabled();
      expect(btn).toHaveAttribute("aria-pressed", "true");
      expect(btn).toHaveClass("selected");
    });

    // Minor #3：同卡未中选的另一个选项也必须禁用且无 selected/aria-pressed
    const otherBtn = screen.getByRole("button", { name: "投诉" });
    expect(otherBtn).toBeDisabled();
    expect(otherBtn).toHaveAttribute("aria-pressed", "false");
    expect(otherBtn).not.toHaveClass("selected");

    const userBubbles = document.querySelectorAll(".bubble-user .bubble-text");
    expect(userBubbles[userBubbles.length - 1]?.textContent).toBe("退款");
  });

  it("streaming 期间点另一张历史卡片：不新增请求，被点卡片不出现假已选/禁用", async () => {
    // 让首轮流挂起：只发 turn_start，turn_end 等测试手动放行
    let resolveStream!: () => void;
    mockedStreamChat.mockImplementation(
      async (_message, _sessionId, handlers, _signal, _clarifySelection) => {
        handlers.onEvent({
          event: "turn_start",
          data: { turnId: "t-live", traceId: "tr-1", sessionId: SID },
        });
        await new Promise<void>((resolve) => {
          resolveStream = resolve;
        });
      },
    );

    render(<ChatPage />);
    fireEvent.click(await screen.findByText("售后会话"));
    fireEvent.click(await screen.findByRole("button", { name: "退款" }));

    await screen.findByTitle("停止生成"); // streaming=true
    expect(mockedStreamChat).toHaveBeenCalledTimes(1);

    const otherCardBtn = screen.getByRole("button", { name: "物流进度" });
    expect(otherCardBtn).not.toBeDisabled(); // 历史卡片本身不因全局 streaming 禁用
    fireEvent.click(otherCardBtn);

    expect(mockedStreamChat).toHaveBeenCalledTimes(1); // 守卫拦截：未新增请求
    expect(otherCardBtn).not.toBeDisabled(); // 无假禁用
    expect(otherCardBtn).toHaveAttribute("aria-pressed", "false"); // 无假已选
    expect(otherCardBtn).not.toHaveClass("selected");

    resolveStream(); // 放行收尾，避免 streaming 状态泄漏到下个用例
    await screen.findByTitle("发送");
  });

  it("手打路径：streamChat 第 5 参为 undefined（请求体不含 clarify_selection）", async () => {
    vi.mocked(sessionsApi.listSessions).mockResolvedValue([]);
    render(<ChatPage />);

    const input = await screen.findByPlaceholderText("输入消息，Enter 发送，Shift+Enter 换行");
    fireEvent.change(input, { target: { value: "你好" } });
    fireEvent.click(screen.getByTitle("发送"));

    await waitFor(() => expect(mockedStreamChat).toHaveBeenCalled());
    const call = mockedStreamChat.mock.calls[0];
    expect(call[0]).toBe("你好");
    expect(call[1]).toBeNull();
    expect(call[3]).toBeInstanceOf(AbortSignal);
    expect(call[4]).toBeUndefined();
  });
});
