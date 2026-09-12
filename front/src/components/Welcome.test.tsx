import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Welcome from "./Welcome";

describe("Welcome", () => {
  it("renders greeting with username and suggestion cards", () => {
    render(<Welcome username="小明" onPick={() => {}} />);

    expect(screen.getByText("你好，小明")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /帮我写一段 Python 快排/ })).toBeInTheDocument();
  });
});
