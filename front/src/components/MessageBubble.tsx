import type { ChatMessage } from "../chat/model";
import Markdown from "./Markdown";
import ToolCallCard from "./ToolCallCard";
import "./MessageBubble.css";

export default function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";

  return (
    <div className={`bubble-row ${isUser ? "bubble-user" : "bubble-assistant"}`}>
      {!isUser && <div className="avatar avatar-ai">G</div>}
      <div className="bubble">
        {!isUser &&
          message.toolCalls.map((t) => <ToolCallCard key={t.toolCallId} tool={t} />)}
        {isUser ? (
          <div className="bubble-text">{message.content}</div>
        ) : message.content ? (
          <div className={message.streaming ? "typing-cursor" : ""}>
            <Markdown text={message.content} />
          </div>
        ) : message.toolCalls.length === 0 && message.streaming ? (
          <div className="thinking">
            <span className="thinking-dots">
              <i /> <i /> <i />
            </span>
          </div>
        ) : null}
        {!isUser && message.offloaded && message.artifactUrl && (
          <a
            className="artifact-download"
            href={message.artifactUrl}
            download
            target="_blank"
            rel="noreferrer"
          >
            下载完整回复
            {message.artifactSize != null && (
              <span className="artifact-size">（{(message.artifactSize / 1024).toFixed(1)} KB）</span>
            )}
          </a>
        )}
        {message.stopped && <div className="stopped-hint">已停止生成（后台仍在完成，刷新后可见完整结果）</div>}
        {message.error && <div className="bubble-error">{message.error}</div>}
      </div>
      {isUser && <div className="avatar avatar-user">我</div>}
    </div>
  );
}
