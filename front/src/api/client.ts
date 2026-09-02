import type { ApiErrorBody } from "./types";

// 开发态经 Vite 代理同源访问；生产可通过相对路径由网关转发。
export const API_BASE = "";

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init.headers || {}) },
    ...init,
  });
  if (resp.status === 204) {
    return undefined as T;
  }
  const text = await resp.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!resp.ok) {
    const errBody = body as ApiErrorBody | null;
    throw new ApiError(
      resp.status,
      errBody?.code || "HTTP_ERROR",
      errBody?.message || `请求失败（${resp.status}）`
    );
  }
  return body as T;
}

export function getJson<T>(path: string): Promise<T> {
  return request<T>(path, { method: "GET" });
}

export function postJson<T>(path: string, payload?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
}

export function patchJson<T>(path: string, payload: unknown): Promise<T> {
  return request<T>(path, { method: "PATCH", body: JSON.stringify(payload) });
}

export function deleteJson<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}
