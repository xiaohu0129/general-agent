import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../chat/model";
import MessageBubble from "./MessageBubble";

function msg(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "你好",
    toolCalls: [],
    ...over,
  };
}

const CLARIFY = {
  options: [
    { label: "选项甲", value: "opt:alpha" },
    { label: "选项乙", value: "beta" },
  ],
};

describe("MessageBubble 澄清卡片挂载", () => {
  it("无 clarify 的消息不渲染任何澄清按钮", () => {
    render(<MessageBubble message={msg()} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("user 消息即使带 clarify 也不渲染卡片", () => {
    render(<MessageBubble message={msg({ role: "user", clarify: CLARIFY })} />);
    expect(screen.queryByText("选项甲")).toBeNull();
  });

  it("assistant clarify 存在且传入 handler：渲染选项，点击透传 (messageId, value, label)", () => {
    const onClarifySelect = vi.fn();
    render(
      <MessageBubble message={msg({ clarify: CLARIFY })} onClarifySelect={onClarifySelect} />,
    );

    const btn = screen.getByRole("button", { name: "选项甲" });
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(onClarifySelect).toHaveBeenCalledWith("m1", "opt:alpha", "选项甲");
  });

  it("缺省 onClarifySelect 时按钮按禁用渲染，点击无动作", () => {
    render(<MessageBubble message={msg({ clarify: CLARIFY })} />);

    for (const label of ["选项甲", "选项乙"]) {
      expect(screen.getByRole("button", { name: label })).toBeDisabled();
    }
  });

  it("clarify.disabled=true 时全部禁用", () => {
    render(
      <MessageBubble
        message={msg({ clarify: { ...CLARIFY, disabled: true } })}
        onClarifySelect={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: "选项甲" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "选项乙" })).toBeDisabled();
  });

  it("消息 streaming 时全部禁用", () => {
    render(
      <MessageBubble
        message={msg({ streaming: true, clarify: CLARIFY })}
        onClarifySelect={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: "选项甲" })).toBeDisabled();
  });
});
