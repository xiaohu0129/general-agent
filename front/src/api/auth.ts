import { getJson, postJson } from "./client";
import type { UserInfo } from "./types";

export function login(username: string, password: string): Promise<UserInfo> {
  return postJson<UserInfo>("/auth/login", { username, password });
}

export function register(username: string, password: string): Promise<UserInfo> {
  return postJson<UserInfo>("/auth/register", { username, password });
}

export function logout(): Promise<{ ok: boolean }> {
  return postJson<{ ok: boolean }>("/auth/logout");
}

export function fetchMe(): Promise<UserInfo> {
  return getJson<UserInfo>("/auth/me");
}
