// Thin fetch wrapper plus the small amount of global state the UI needs.
import { t, tServer } from "./i18n.js";

export const state = {
  user: null,
  status: null,        // /api/auth/status: version, permissions, registration
};

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

function describe(data, fallback) {
  const detail = data && data.detail;
  if (typeof detail === "string") return tServer(detail);
  if (Array.isArray(detail)) {  // pydantic validation errors
    return detail.map((d) => `${(d.loc || []).slice(1).join(".") || t("value")}: ${d.msg}`).join("; ");
  }
  return fallback;
}

export async function api(method, path, body, { raw = false } = {}) {
  const headers = { "X-Requested-With": "archipelago-manager" };
  const init = { method, headers, credentials: "same-origin" };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(t("Cannot reach the server. Check your connection."), 0);
  }
  if (raw && response.ok) return response;
  const type = response.headers.get("content-type") || "";
  const data = type.includes("json") ? await response.json().catch(() => null) : await response.text();
  if (!response.ok) {
    if (response.status === 401 && state.user) onUnauthorized();
    throw new ApiError(describe(data, t("Request failed ({status})", { status: response.status })), response.status, data);
  }
  return data;
}

export const get = (path) => api("GET", path);
export const post = (path, body) => api("POST", path, body ?? {});
export const patch = (path, body) => api("PATCH", path, body);
export const put = (path, body) => api("PUT", path, body);
export const del = (path) => api("DELETE", path);

export const isAdmin = () => state.user?.role === "admin";

export async function refreshStatus() {
  state.status = await get("/api/auth/status");
  state.user = state.status.user;
  return state.status;
}
