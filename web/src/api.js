/*
 * RAVAN cloud API client.
 *
 * Two rules this file exists to enforce:
 *   1. Every request carries a client-generated request_id. The server caches
 *      replies by that id, so a retry after a dropped connection cannot
 *      produce a second answer (or a second spoken sentence).
 *   2. Nothing is silent. Timeouts, cold starts and error bodies turn into
 *      readable Error objects with a `code` the UI can act on.
 */

const SETTINGS_KEY = "ravan.settings.v1";

export function loadLocalSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export function saveLocalSettings(patch) {
  const next = { ...loadLocalSettings(), ...patch };
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(next));
  } catch {
    /* storage full or blocked — settings stay in memory for this session */
  }
  return next;
}

function normaliseBase(url) {
  const value = String(url || "").trim().replace(/\/+$/, "");
  if (!value) return "";
  const isLocal = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/i.test(value);
  if (!/^https:\/\//i.test(value) && !isLocal) {
    throw new ApiError(
      "The backend URL must use https:// (plain http is only allowed on localhost).",
      "config"
    );
  }
  return value;
}

export class ApiError extends Error {
  constructor(message, code = "error", status = 0, retryAfter = 0) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

function sameOriginBase() {
  // In the APK the page origin is local (https://localhost), so "same origin"
  // is never a real backend. Leaving the base empty makes the UI ask for the
  // Render URL instead of failing against the WebView itself.
  if (window.Capacitor?.isNativePlatform?.()) return "";
  if (location.protocol === "http:" || location.protocol === "https:") {
    if (/\.github\.io$/i.test(location.hostname)) return "";
    return location.origin;
  }
  return "";
}

export class Api {
  constructor() {
    this.baseUrl = "";
    this.token = "";
    this.setConfig({});
  }

  setConfig({ backendUrl, apiToken } = {}) {
    const stored = loadLocalSettings();
    const cfg = window.RAVAN_CONFIG || {};
    const raw = backendUrl ?? stored.backendUrl ?? cfg.backendUrl ?? "";
    this.baseUrl = normaliseBase(raw) || sameOriginBase();
    this.token = apiToken ?? stored.apiToken ?? cfg.apiToken ?? "";
    return this;
  }

  get configured() {
    return Boolean(this.baseUrl);
  }

  get describe() {
    if (!this.baseUrl) return "no backend";
    try {
      return new URL(this.baseUrl).host;
    } catch {
      return this.baseUrl;
    }
  }

  _headers(extra = {}) {
    const headers = { "Content-Type": "application/json", ...extra };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    return headers;
  }

  _url(path) {
    if (!this.baseUrl) {
      throw new ApiError(
        "No backend is configured. Add the Render URL in Settings, or open the copy served by the backend.",
        "config"
      );
    }
    return `${this.baseUrl}${path}`;
  }

  async _fetch(path, { method = "GET", body, timeout = 12000, signal } = {}) {
    const controller = new AbortController();
    const onAbort = () => controller.abort();
    if (signal) {
      if (signal.aborted) controller.abort();
      else signal.addEventListener("abort", onAbort, { once: true });
    }
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      return await fetch(this._url(path), {
        method,
        headers: this._headers(),
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        mode: "cors",
        cache: "no-store",
        credentials: "omit"
      });
    } catch (err) {
      if (signal?.aborted) throw new ApiError("Cancelled.", "aborted");
      if (err?.name === "AbortError") {
        throw new ApiError(
          "The server did not answer in time. It may be waking up — try again.",
          "timeout"
        );
      }
      throw new ApiError(
        "Cannot reach the RAVAN backend. Check your connection and the backend URL.",
        "network"
      );
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onAbort);
    }
  }

  static async _detail(res) {
    let detail = "";
    try {
      const data = await res.json();
      detail = typeof data?.detail === "string" ? data.detail : "";
    } catch {
      /* non-JSON error page, e.g. a proxy's 502 */
    }
    if (!detail) {
      if (res.status === 401) detail = "This backend needs an access token. Add it in Settings.";
      else if (res.status === 429) detail = "You are sending requests too quickly. Wait a moment.";
      else if (res.status === 502 || res.status === 503 || res.status === 504) {
        detail = "The backend is starting or unavailable. Try again in a few seconds.";
      } else detail = `The backend returned an error (${res.status}).`;
    }
    return detail;
  }

  async health({ timeout = 7000, signal } = {}) {
    const res = await this._fetch("/health", { timeout, signal });
    if (!res.ok) throw new ApiError(await Api._detail(res), "health", res.status);
    const data = await res.json();
    if (!data || data.ok !== true) {
      throw new ApiError("The backend health check did not report OK.", "health");
    }
    return data;
  }

  /**
   * One chat turn. `requestId` is reused across retries on purpose: the server
   * returns the cached reply instead of calling the model twice.
   */
  async chat({ sessionId, requestId, text, toolResults, localTime }, opts = {}) {
    const body = { session_id: sessionId, request_id: requestId };
    if (text) body.text = text;
    if (toolResults) body.tool_results = toolResults;
    if (localTime) body.local_time = localTime;

    const attempt = async (timeout) =>
      this._fetch("/api/chat", {
        method: "POST",
        body,
        timeout,
        signal: opts.signal
      });

    let res;
    try {
      res = await attempt(opts.timeout ?? 50000);
    } catch (err) {
      if (err.code !== "network" || opts.signal?.aborted) throw err;
      await new Promise((resolve) => setTimeout(resolve, 1400));
      res = await attempt(opts.timeout ?? 50000);
    }

    if (!res.ok) {
      const detail = await Api._detail(res);
      const code =
        res.status === 401 ? "auth"
        : res.status === 409 ? "busy"
        : res.status === 429 ? "rate"
        : res.status >= 500 ? "unavailable"
        : "rejected";
      const retryAfter = Number(res.headers.get("Retry-After") || 0);
      throw new ApiError(detail, code, res.status, retryAfter);
    }

    const data = await res.json();
    return {
      reply: String(data.reply || ""),
      calls: Array.isArray(data.calls) ? data.calls : [],
      confirmation: data.confirmation || null,
      visemes: Array.isArray(data.visemes) ? data.visemes : [],
      mode: data.mode || "live"
    };
  }
}
