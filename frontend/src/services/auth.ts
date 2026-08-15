/**
 * Supabase-backed sign-in for CrossFlow.
 *
 * The password goes to Supabase Auth directly and never to the CrossFlow API,
 * so Supabase's own rate limiting, lockout and password-reset flows apply
 * (backend roadmap decision D2). CrossFlow only ever receives the resulting
 * access token as a Bearer credential.
 *
 * The role shown to the user is the one `/api/auth/session` resolved from
 * `crossflow_profiles` with the caller's own token — never a claim decoded
 * from the token here, which the client could trivially forge.
 */

import type { AuthSession, AuthStatus, StoredSession } from '../types';

const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL?.trim().replace(/\/$/, '') ?? '';
const SUPABASE_KEY = (
  import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY
  ?? import.meta.env.VITE_SUPABASE_ANON_KEY
  ?? ''
).trim();

const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, '');
const API_BASE = configuredApiBase ?? '';

// These are intentionally browser-visible demo credentials. They must point
// only at a disposable test project/account; Vite embeds every VITE_ value in
// the public bundle, so this can never be a production administrator secret.
const TEST_ADMIN_EMAIL = import.meta.env.VITE_TEST_ADMIN_EMAIL?.trim() ?? '';
const TEST_ADMIN_PASSWORD = import.meta.env.VITE_TEST_ADMIN_PASSWORD ?? '';

const STORAGE_KEY = 'crossflow.session';
const AUTH_TIMEOUT_MS = 15_000;
/** Refresh this long before expiry so an in-flight request cannot age out. */
const REFRESH_SKEW_S = 60;

export class AuthError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'AuthError';
  }
}

export function supabaseConfigured(): boolean {
  return SUPABASE_URL.length > 0 && SUPABASE_KEY.length > 0;
}

/** Whether this build offers one-click access to its shared test admin. */
export function testAdminConfigured(): boolean {
  return TEST_ADMIN_EMAIL.length > 0 && TEST_ADMIN_PASSWORD.length > 0;
}

/** The Supabase project this build authenticates against. */
export function configuredProjectOrigin(): string {
  return SUPABASE_URL;
}

/**
 * Detect the browser and the API authenticating against different projects.
 *
 * This is worth its own check because the symptom is so misleading: sign-in
 * succeeds, Supabase issues a perfectly valid token, and then every API call
 * returns a bare 401 because the server is validating it against a different
 * project's keys. Nothing in that sequence points at the real cause.
 */
export function projectMismatch(status: AuthStatus | null): string | null {
  const serverOrigin = status?.project_origin?.trim().replace(/\/$/, '');
  if (!serverOrigin || !SUPABASE_URL) return null;
  if (serverOrigin === SUPABASE_URL) return null;
  return (
    `This build signs in to ${SUPABASE_URL}, but the API validates tokens `
    + `against ${serverOrigin}. A token from one project is never valid for `
    + 'the other, so sign-in would fail with an unexplained 401. Point '
    + 'VITE_SUPABASE_URL and the server\'s SUPABASE_URL at the same project.'
  );
}

async function supabaseFetch(
  path: string,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  if (!supabaseConfigured()) {
    throw new AuthError(
      'Sign-in is not configured: VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY are unset.',
    );
  }
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), AUTH_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(`${SUPABASE_URL}${path}`, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        apikey: SUPABASE_KEY,
        Authorization: `Bearer ${SUPABASE_KEY}`,
      },
      body: JSON.stringify(body),
    });
  } catch (error) {
    throw new AuthError(
      (error as Error)?.name === 'AbortError'
        ? 'Supabase did not respond in time. Check your connection and try again.'
        : 'Could not reach Supabase. Check your connection and try again.',
    );
  } finally {
    window.clearTimeout(timer);
  }

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    // Surface Supabase's own wording; it distinguishes bad credentials from
    // an unconfirmed email, which a generic message would hide.
    const detail = typeof payload?.error_description === 'string'
      ? payload.error_description
      : typeof payload?.msg === 'string'
        ? payload.msg
        : typeof payload?.message === 'string'
          ? payload.message
          : 'Sign-in failed.';
    throw new AuthError(detail);
  }
  return payload as Record<string, unknown>;
}

function toStoredSession(payload: Record<string, unknown>): StoredSession {
  const accessToken = typeof payload.access_token === 'string' ? payload.access_token : '';
  const refreshToken = typeof payload.refresh_token === 'string' ? payload.refresh_token : '';
  const expiresIn = typeof payload.expires_in === 'number' ? payload.expires_in : 3600;
  if (!accessToken) throw new AuthError('Supabase returned no access token.');
  return {
    accessToken,
    refreshToken,
    expiresAtMs: Date.now() + expiresIn * 1000,
  };
}

export function readStoredSession(): StoredSession | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredSession>;
    if (typeof parsed?.accessToken !== 'string' || !parsed.accessToken) return null;
    if (typeof parsed?.expiresAtMs !== 'number') return null;
    return {
      accessToken: parsed.accessToken,
      refreshToken: typeof parsed.refreshToken === 'string' ? parsed.refreshToken : '',
      expiresAtMs: parsed.expiresAtMs,
    };
  } catch {
    // Corrupt or unavailable storage must not break the public views.
    return null;
  }
}

function writeStoredSession(session: StoredSession | null): void {
  try {
    if (session) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Private-mode storage failures are not fatal; the session stays in memory.
  }
}

export async function signIn(email: string, password: string): Promise<StoredSession> {
  const payload = await supabaseFetch('/auth/v1/token?grant_type=password', {
    email: email.trim(),
    password,
  });
  const session = toStoredSession(payload);
  writeStoredSession(session);
  return session;
}

/**
 * Sign in with the explicitly configured shared test administrator.
 *
 * The credentials still go directly to Supabase through `signIn`; they never
 * reach the CrossFlow API. The UI separately verifies that the server resolves
 * this account as an admin before it accepts the session.
 */
export async function signInAsTestAdmin(): Promise<StoredSession> {
  if (!testAdminConfigured()) {
    throw new AuthError('Test admin access is not configured for this build.');
  }
  return signIn(TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD);
}

export async function refreshSession(session: StoredSession): Promise<StoredSession> {
  if (!session.refreshToken) throw new AuthError('This session cannot be refreshed.');
  const payload = await supabaseFetch('/auth/v1/token?grant_type=refresh_token', {
    refresh_token: session.refreshToken,
  });
  const refreshed = toStoredSession(payload);
  writeStoredSession(refreshed);
  return refreshed;
}

/** Return a session that is still valid, refreshing it first when it is close to expiry. */
export async function validSession(session: StoredSession): Promise<StoredSession> {
  if (Date.now() < session.expiresAtMs - REFRESH_SKEW_S * 1000) return session;
  return refreshSession(session);
}

export async function signOut(session: StoredSession | null): Promise<void> {
  writeStoredSession(null);
  if (!session?.accessToken || !supabaseConfigured()) return;
  try {
    await fetch(`${SUPABASE_URL}/auth/v1/logout`, {
      method: 'POST',
      headers: {
        apikey: SUPABASE_KEY,
        Authorization: `Bearer ${session.accessToken}`,
      },
    });
  } catch {
    // The local credential is already gone, which is what signing out means
    // here. A failed server-side revoke must not strand the user signed in.
  }
}

export async function fetchAuthStatus(): Promise<AuthStatus | null> {
  try {
    const response = await fetch(`${API_BASE}/api/auth/status`);
    if (!response.ok) return null;
    return (await response.json()) as AuthStatus;
  } catch {
    return null;
  }
}

/**
 * Ask the API who it thinks the caller is.
 *
 * The returned role is authoritative. Reading it from the token client-side
 * would let anyone hand themselves an admin badge.
 */
export async function fetchSession(session: StoredSession): Promise<AuthSession> {
  const response = await fetch(`${API_BASE}/api/auth/session`, {
    headers: { Authorization: `Bearer ${session.accessToken}` },
  });
  if (response.status === 401) {
    throw new AuthError('Your session has expired. Please sign in again.');
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new AuthError(
      typeof detail?.detail === 'string' ? detail.detail : 'Could not load your session.',
    );
  }
  return (await response.json()) as AuthSession;
}
