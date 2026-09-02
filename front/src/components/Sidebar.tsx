import { useState } from "react";
import type { SessionItem } from "../api/types";
import "./Sidebar.css";

interface Props {
  sessions: SessionItem[];
  currentId: string | null;
  username: string;
  onNew: () => void;
  onSelect: (s: SessionItem) => void;
  onRename: (s: SessionItem, title: string) => void;
  onDelete: (s: SessionItem) => void;
  onLogout: () => void;
}

export default function Sidebar({
  sessions,
  currentId,
  username,
  onNew,
  onSelect,
  onRename,
  onDelete,
  onLogout,
}: Props) {
  const [menuFor, setMenuFor] = useState<string | null>(null);

  return (
    <aside className="sidebar">
      {menuFor && (
        <div className="menu-backdrop" onClick={() => setMenuFor(null)} />
      )}
      <button className="new-chat" onClick={onNew}>
        <span className="plus">＋</span> 新建对话
      </button>

      <div className="session-list">
        {sessions.map((s) => (
          <div
            key={s.sessionId}
            className={`session-item ${s.sessionId === currentId ? "active" : ""}`}
            onClick={() => onSelect(s)}
          >
            <span className="session-title" title={s.title}>
              {s.title}
            </span>
            <button
              className="session-more"
              onClick={(e) => {
                e.stopPropagation();
                setMenuFor(menuFor === s.sessionId ? null : s.sessionId);
              }}
            >
              ⋯
            </button>
            {menuFor === s.sessionId && (
              <div className="session-menu" onClick={(e) => e.stopPropagation()}>
                <button
                  onClick={() => {
                    const title = window.prompt("新的会话标题", s.title);
                    if (title && title.trim()) onRename(s, title.trim());
                    setMenuFor(null);
                  }}
                >
                  重命名
                </button>
                <button
                  className="danger"
                  onClick={() => {
                    if (window.confirm(`确定删除会话「${s.title}」？`)) onDelete(s);
                    setMenuFor(null);
                  }}
                >
                  删除
                </button>
              </div>
            )}
          </div>
        ))}
        {sessions.length === 0 && <div className="session-empty">暂无历史对话</div>}
      </div>

      <div className="sidebar-footer">
        <div className="user-box">
          <div className="user-avatar">{username.slice(0, 1).toUpperCase()}</div>
          <span className="user-name" title={username}>
            {username}
          </span>
        </div>
        <button className="btn-ghost" onClick={onLogout}>
          退出登录
        </button>
      </div>
    </aside>
  );
}
