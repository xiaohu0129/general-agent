import { useState } from "react";
import type { ToolCall } from "../chat/model";
import "./ToolCallCard.css";

export default function ToolCallCard({ tool }: { tool: ToolCall }) {
  const [open, setOpen] = useState(false);

  const statusText =
    tool.status === "running"
      ? "执行中"
      : tool.status === "error"
        ? "执行失败"
        : tool.status === "stopped"
          ? "已中断"
          : "已完成";

  return (
    <div className={`tool-card tool-${tool.status}`}>
      <button className="tool-head" onClick={() => setOpen((v) => !v)}>
        <span className={`tool-icon tool-icon-${tool.status}`}>
          {tool.status === "running" ? (
            <span className="spinner" />
          ) : tool.status === "success" ? (
            "✓"
          ) : tool.status === "stopped" ? (
            "⏸"
          ) : (
            "✕"
          )}
        </span>
        <span className="tool-name">{tool.toolName}</span>
        <span className="tool-status">{statusText}</span>
        <span className={`tool-chevron ${open ? "open" : ""}`}>⌄</span>
      </button>
      {open && (
        <div className="tool-body">
          <div className="tool-section">
            <div className="tool-label">参数</div>
            <pre>{JSON.stringify(tool.args, null, 2)}</pre>
          </div>
          {tool.status === "error" ? (
            <div className="tool-section">
              <div className="tool-label">错误{tool.errorCode ? `（${tool.errorCode}）` : ""}</div>
              <pre className="tool-error-pre">{tool.error || "未知错误"}</pre>
            </div>
          ) : (
            tool.result !== undefined && (
              <div className="tool-section">
                <div className="tool-label">
                  结果
                  {tool.offloaded && tool.artifactSize != null && (
                    <span className="tool-offload-hint">
                      （已截断，完整产物 {(tool.artifactSize / 1024).toFixed(1)} KB）
                    </span>
                  )}
                </div>
                <pre>{typeof tool.result === "string" ? tool.result : JSON.stringify(tool.result, null, 2)}</pre>
                {tool.offloaded && tool.artifactUrl && (
                  <a
                    className="tool-download"
                    href={tool.artifactUrl}
                    download
                    target="_blank"
                    rel="noreferrer"
                  >
                    下载完整产物
                  </a>
                )}
              </div>
            )
          )}
        </div>
      )}
    </div>
  );
}
