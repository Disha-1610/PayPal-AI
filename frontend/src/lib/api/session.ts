import { API_BASE_URL, ApiError } from "./client";

/**
 * Establishes the browser session. In development the backend offers an anonymous sandbox sign-in that sets an
 * HTTP-only cookie; where it is switched off (404) authentication comes from the token provider instead
 * (`setAuthTokenProvider`), so there is nothing to do here.
 */
let pending: Promise<void> | null = null;

async function startSession(): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/auth/sandbox-session`, {
      method: "POST",
      credentials: "include",
      signal: AbortSignal.timeout(10_000),
    });
  } catch (cause) {
    throw new ApiError(0, "could not reach the server", cause);
  }
  if (response.ok || response.status === 404) return;
  throw new ApiError(response.status, "could not start a session");
}

/** Idempotent and shared: concurrent callers wait on one request; a failure is not cached. */
export function ensureSession(): Promise<void> {
  pending ??= startSession().catch((error: unknown) => {
    pending = null;
    throw error;
  });
  return pending;
}

/** Forget the session (e.g. after a 401) so the next `ensureSession` signs in again. */
export function resetSession(): void {
  pending = null;
}
