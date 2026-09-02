import { useAuth } from "./state/auth-context";
import LoginPage from "./pages/LoginPage";
import ChatPage from "./pages/ChatPage";

export default function App() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div
        style={{
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--text-3)",
        }}
      >
        加载中…
      </div>
    );
  }

  return user ? <ChatPage /> : <LoginPage />;
}
