/**
 * Minimal fetch-based API client: base config, auth interceptor, error normalisation.
 * Auth: Bearer token from the registered provider; the HTTP-only session cookie is
 * also sent (`credentials: "include"`), matching the backend's get_principal.
 */

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/+$/, "");

const DEFAULT_TIMEOUT_MS = 15_000;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}

type TokenProvider = () => string | null | undefined | Promise<string | null | undefined>;
type UnauthorizedHandler = (error: ApiError) => void;

let tokenProvider: TokenProvider | null = null;
let unauthorizedHandler: UnauthorizedHandler | null = null;

export function setAuthTokenProvider(provider: TokenProvider | null): void {
  tokenProvider = provider;
}

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

/** Auth headers for non-`request` callers (e.g. the SSE stream). */
export async function getAuthHeaders(): Promise<Record<string, string>> {
  const token = tokenProvider ? await tokenProvider() : null;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

type SessionRefresher = () => Promise<void>;

let sessionRefresher: SessionRefresher | null = null;

/** Re-establishes the session after a 401; the failed request is retried once. */
export function setSessionRefresher(refresher: SessionRefresher | null): void {
  sessionRefresher = refresher;
}

export interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  timeoutMs?: number;
}

function errorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const { detail } = body as { detail: unknown };
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return "invalid request";
  }
  return fallback;
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return send<T>(path, options, false);
}

async function send<T>(path: string, options: RequestOptions, retried: boolean): Promise<T> {
  const { method = "GET", body, headers = {}, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options;

  const finalHeaders = new Headers({ Accept: "application/json", ...headers });
  if (body !== undefined) finalHeaders.set("Content-Type", "application/json");

  const token = tokenProvider ? await tokenProvider() : null;
  if (token) finalHeaders.set("Authorization", `Bearer ${token}`);

  const timeout = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers: finalHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "include",
      signal: combined,
    });
  } catch (cause) {
    if (signal?.aborted) throw cause;
    const reason = timeout.aborted ? "request timed out" : "network error";
    throw new ApiError(0, reason, cause);
  }

  const data = await parseBody(response);
  if (!response.ok) {
    const error = new ApiError(
      response.status,
      errorMessage(data, response.statusText || "request failed"),
      data,
    );
    if (error.isUnauthorized && sessionRefresher && !retried) {
      try {
        await sessionRefresher();
      } catch {
        // the refresh failed: report the original 401 below
        unauthorizedHandler?.(error);
        throw error;
      }
      return send<T>(path, options, true);
    }
    if (error.isUnauthorized) unauthorizedHandler?.(error);
    throw error;
  }
  return data as T;
}
