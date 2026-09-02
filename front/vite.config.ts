import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发态把后端接口代理到 :9093，浏览器视为同源，cookie 无需跨站配置。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/auth": { target: "http://localhost:9093", changeOrigin: true },
      "/chat": { target: "http://localhost:9093", changeOrigin: true },
      "/stream": { target: "http://localhost:9093", changeOrigin: true },
      "/sessions": { target: "http://localhost:9093", changeOrigin: true },
      "/health": { target: "http://localhost:9093", changeOrigin: true },
    },
  },
});
