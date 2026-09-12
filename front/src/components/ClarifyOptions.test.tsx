import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ClarifyOptions from "./ClarifyOptions";

const OPTIONS = [
  { label: "选项甲", value: "opt:alpha" },
  { label: "选项乙", value: "beta" },
  { label: "选项丙", value: "gamma:123" },
];

function buttons() {
  return OPTIONS.map((o) => screen.getByRole("button", { name: o.label }));
}

describe("ClarifyOptions", () => {
  it("按 options 顺序渲染 label，按钮文本不是 value", () => {
    render(<ClarifyOptions options={OPTIONS} onSelect={() => {}} />);

    const rendered = screen.getAllByRole("button").map((b) => b.textContent);
    expect(rendered).toEqual(["选项甲", "选项乙", "选项丙"]);
    expect(screen.queryByText("opt:alpha")).toBeNull();
  });

  it("可点态点击回调携带原始 (value, label)，不解析前缀", () => {
    const onSelect = vi.fn();
    render(<ClarifyOptions options={OPTIONS} onSelect={onSelect} />);

    const [a, b, c] = buttons();
    expect(a).not.toBeDisabled();
    fireEvent.click(c);
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("gamma:123", "选项丙");
    fireEvent.click(a);
    expect(onSelect).toHaveBeenCalledWith("opt:alpha", "选项甲");
    expect(b).not.toBeDisabled();
  });

  it("selected 命中：匹配项高亮且 aria-pressed，全部按钮禁用", () => {
    render(<ClarifyOptions options={OPTIONS} selected="beta" onSelect={() => {}} />);

    const [a, b, c] = buttons();
    expect(b).toHaveClass("selected");
    expect(b).toHaveAttribute("aria-pressed", "true");
    expect(a).not.toHaveClass("selected");
    expect(c).not.toHaveClass("selected");
    for (const btn of [a, b, c]) {
      expect(btn).toBeDisabled();
      expect(btn).toHaveAttribute("aria-pressed", btn === b ? "true" : "false");
    }
  });

  it("脏 selected（不在 options 集合）：全部禁用、无高亮、不崩溃", () => {
    render(<ClarifyOptions options={OPTIONS} selected="not-exist" onSelect={() => {}} />);

    for (const btn of buttons()) {
      expect(btn).toBeDisabled();
      expect(btn).not.toHaveClass("selected");
    }
  });

  it("脏 selected 空串：全部禁用、无高亮、不崩溃", () => {
    render(<ClarifyOptions options={OPTIONS} selected="" onSelect={() => {}} />);

    for (const btn of buttons()) {
      expect(btn).toBeDisabled();
      expect(btn).not.toHaveClass("selected");
    }
  });

  it("disabled=true：全部禁用且点击不触发回调", () => {
    const onSelect = vi.fn();
    render(<ClarifyOptions options={OPTIONS} disabled onSelect={onSelect} />);

    for (const btn of buttons()) {
      expect(btn).toBeDisabled();
      fireEvent.click(btn);
    }
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("streaming=true：全部禁用且点击不触发回调", () => {
    const onSelect = vi.fn();
    render(<ClarifyOptions options={OPTIONS} streaming onSelect={onSelect} />);

    for (const btn of buttons()) {
      expect(btn).toBeDisabled();
      fireEvent.click(btn);
    }
    expect(onSelect).not.toHaveBeenCalled();
  });
});
