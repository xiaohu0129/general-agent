import { deleteJson, getJson, patchJson, postJson } from "./client";
import { API_BASE } from "./client";
import type { MessagePage, SessionItem } from "./types";

export function listSessions(): Promise<SessionItem[]> {
  return getJson<{ sessions: SessionItem[] }>("/sessions").then((r) => r.sessions);
}

export function createSession(title?: string): Promise<SessionItem> {
  return postJson<SessionItem>("/sessions", title ? { title } : {});
}

export function listMessages(
  sessionId: string,
  opts: { before?: number; limit?: number } = {}
): Promise<MessagePage> {
  const params = new URLSearchParams();
  if (opts.before != null) params.set("before", String(opts.before));
  if (opts.limit != null) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return getJson<MessagePage>(
    `/sessions/${sessionId}/messages${qs ? `?${qs}` : ""}`
  );
}

export function artifactDownloadUrl(sessionId: string, messageId: number): string {
  return `${API_BASE}/sessions/${sessionId}/artifacts/${messageId}`;
}

export function renameSession(sessionId: string, title: string): Promise<{ ok: boolean }> {
  return patchJson<{ ok: boolean }>(`/sessions/${sessionId}`, { title });
}

export function deleteSession(sessionId: string): Promise<{ ok: boolean }> {
  return deleteJson<{ ok: boolean }>(`/sessions/${sessionId}`);
}
