import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import "./Composer.css";

interface Props {
  disabled: boolean;
  streaming: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  initialText?: string;
}

export default function Composer({ disabled, streaming, onSend, onStop, initialText }: Props) {
  const [text, setText] = useState(initialText || "");
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (initialText !== undefined) {
      setText(initialText);
    }
  }, [initialText]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  const send = () => {
    const value = text.trim();
    if (!value || disabled || streaming) return;
    onSend(value);
    setText("");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          rows={1}
        />
        {streaming ? (
          <button className="send-btn stop-btn" onClick={onStop} title="停止生成">
            <span className="stop-icon" />
          </button>
        ) : (
          <button
            className="send-btn"
            onClick={send}
            disabled={disabled || !text.trim()}
            title="发送"
          >
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 19V5" />
              <path d="M5 12l7-7 7 7" />
            </svg>
          </button>
        )}
      </div>
      <div className="composer-hint">内容由 AI 生成，请注意甄别</div>
    </div>
  );
}
