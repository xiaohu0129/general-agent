import { useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { useAuth } from "../state/auth-context";
import "./LoginPage.css";

export default function LoginPage() {
  const { login, register } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (mode === "login") {
        await login(username.trim(), password);
      } else {
        await register(username.trim(), password);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "网络错误，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <div className="login-logo">G</div>
        <h1 className="login-title">General Agent</h1>
        <p className="login-sub">
          {mode === "login" ? "登录以开始对话" : "注册一个新账号"}
        </p>

        <label className="field">
          <span>用户名</span>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="2-32 位字母、数字、下划线或中文"
            autoComplete="username"
            autoFocus
          />
        </label>
        <label className="field">
          <span>密码</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="至少 8 位"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
          />
        </label>

        {error && <div className="login-error">{error}</div>}

        <button className="btn-primary" type="submit" disabled={busy}>
          {busy ? "请稍候…" : mode === "login" ? "登录" : "注册并登录"}
        </button>

        <div className="login-switch">
          {mode === "login" ? (
            <>
              还没有账号？
              <button type="button" onClick={() => { setMode("register"); setError(""); }}>
                立即注册
              </button>
            </>
          ) : (
            <>
              已有账号？
              <button type="button" onClick={() => { setMode("login"); setError(""); }}>
                返回登录
              </button>
            </>
          )}
        </div>
      </form>
    </div>
  );
}
