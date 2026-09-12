import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../chat/model";
import MessageList from "./MessageList";

describe("MessageList 澄清回调透传", () => {
  it("无 clarify 的消息不渲染澄清按钮，有 clarify 的消息点击触发透传回调", () => {
    const onClarifySelect = vi.fn();
    const messages: ChatMessage[] = [
      {
        id: "u1",
        role: "user",
        content: "问",
        toolCalls: [],
      },
      {
        id: "a1",
        role: "assistant",
        content: "答",
        toolCalls: [],
        clarify: {
          options: [
            { label: "选项甲", value: "opt:alpha" },
            { label: "选项乙", value: "beta" },
          ],
        },
      },
    ];

    render(<MessageList messages={messages} onClarifySelect={onClarifySelect} />);

    expect(screen.getAllByRole("button")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "选项乙" }));
    expect(onClarifySelect).toHaveBeenCalledWith("a1", "beta", "选项乙");
  });
});
