const SUGGESTIONS = [
  { title: "帮我写一段 Python 快排", prompt: "用 Python 写一个快速排序，并解释思路" },
  { title: "总结一下 HTTP 与 HTTPS 的区别", prompt: "请总结 HTTP 与 HTTPS 的核心区别" },
  { title: "制定一周健身计划", prompt: "帮我制定一个一周三次的健身计划" },
  { title: "解释 LangGraph 是什么", prompt: "简单解释 LangGraph 的核心概念和用途" },
];

export default function Welcome({
  username,
  onPick,
}: {
  username: string;
  onPick: (prompt: string) => void;
}) {
  return (
    <div className="welcome">
      <div className="welcome-logo">G</div>
      <h1 className="welcome-title">你好，{username}</h1>
      <p className="welcome-sub">有什么可以帮你的吗？</p>
      <div className="suggest-grid">
        {SUGGESTIONS.map((s) => (
          <button key={s.title} className="suggest-card" onClick={() => onPick(s.prompt)}>
            <div className="suggest-title">{s.title}</div>
            <div className="suggest-prompt">{s.prompt}</div>
          </button>
        ))}
      </div>
    </div>
  );
}
