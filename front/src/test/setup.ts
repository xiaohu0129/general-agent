import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// vitest 未开启 globals，RTL 不会自动 cleanup，需手动注册
afterEach(() => {
  cleanup();
});

// jsdom 未实现 scrollIntoView（MessageList 挂载时会调用）
Element.prototype.scrollIntoView = function scrollIntoView() {};
