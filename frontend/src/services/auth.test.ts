/* @vitest-environment jsdom */
/* jsdom refuses localStorage on an opaque origin, which is what the default
   about:blank document has. Give the document a real origin so the session
   persistence under test behaves as it does in a browser. */
/* @vitest-environment-options { "url": "http://localhost:3000" } */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const SUPABASE_URL = 'https://wtednggryrikyvkuhqjo.supabase.co';
const PUBLISHABLE_KEY = 'sb_publishable_test';

vi.stubEnv('VITE_SUPABASE_URL', SUPABASE_URL);
vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', PUBLISHABLE_KEY);

const {
  AuthError, completeOAuthRedirect, fetchSession, readStoredSession,
  refreshSession, signIn, signInWithGitHub, signOut, supabaseConfigured,
  validSession,
} = await import('./auth');

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const TOKEN_PAYLOAD = {
  access_token: 'access-token-abc',
  refresh_token: 'refresh-token-xyz',
  expires_in: 3600,
};

/**
 * Own the storage under test.
 *
 * Node 25 exposes an experimental native `localStorage` that shadows jsdom's
 * and is inert without `--localstorage-file`, so the ambient implementation
 * differs by Node version. An explicit in-memory stub keeps these assertions
 * about our code rather than about the runtime.
 */
function installMemoryStorage(): Storage {
  const entries = new Map<string, string>();
  const storage: Storage = {
    get length() { return entries.size; },
    clear: () => entries.clear(),
    getItem: (key: string) => entries.get(key) ?? null,
    key: (index: number) => Array.from(entries.keys())[index] ?? null,
    removeItem: (key: string) => { entries.delete(key); },
    setItem: (key: string, value: string) => { entries.set(key, String(value)); },
  };
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: storage,
  });
  return storage;
}

beforeEach(() => {
  installMemoryStorage();
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('supabase sign-in', () => {
  it('is configured from the Vite environment', () => {
    expect(supabaseConfigured()).toBe(true);
  });

  it('sends the password to Supabase and never to the CrossFlow API', async () => {
    const fetchMock = vi.fn<FetchLike>(async () => jsonResponse(TOKEN_PAYLOAD));
    vi.stubGlobal('fetch', fetchMock);

    await signIn('driver@example.com', 'hunter2');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${SUPABASE_URL}/auth/v1/token?grant_type=password`);
    // The password must only ever appear in the Supabase request.
    expect(String(init.body)).toContain('hunter2');
    const calledHosts = fetchMock.mock.calls.map(call => String(call[0]));
    expect(calledHosts.every(target => target.startsWith(SUPABASE_URL))).toBe(true);
  });

  it('persists the session so a reload stays signed in', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(TOKEN_PAYLOAD)));
    await signIn('driver@example.com', 'hunter2');

    const stored = readStoredSession();
    expect(stored?.accessToken).toBe('access-token-abc');
    expect(stored?.refreshToken).toBe('refresh-token-xyz');
    expect(stored?.expiresAtMs).toBeGreaterThan(Date.now());
  });

  it('surfaces Supabase\'s own message instead of a generic failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      { error_description: 'Email not confirmed' }, 400,
    )));

    // A generic "sign-in failed" would hide the one thing the user must do.
    await expect(signIn('driver@example.com', 'hunter2'))
      .rejects.toThrow('Email not confirmed');
  });

  it('does not write a session when sign-in fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ msg: 'Invalid login credentials' }, 400)));
    await expect(signIn('driver@example.com', 'wrong')).rejects.toThrow(AuthError);
    expect(readStoredSession()).toBeNull();
  });

  it('ignores corrupt stored sessions rather than throwing', () => {
    window.localStorage.setItem('crossflow.session', '{not json');
    expect(readStoredSession()).toBeNull();

    window.localStorage.setItem('crossflow.session', JSON.stringify({ accessToken: '' }));
    expect(readStoredSession()).toBeNull();
  });
});

describe('session lifetime', () => {
  it('refreshes a session that is close to expiry', async () => {
    const fetchMock = vi.fn<FetchLike>(async () => jsonResponse({
      ...TOKEN_PAYLOAD, access_token: 'refreshed-token',
    }));
    vi.stubGlobal('fetch', fetchMock);

    const nearlyExpired = {
      accessToken: 'old', refreshToken: 'refresh-token-xyz',
      expiresAtMs: Date.now() + 5_000,
    };
    const fresh = await validSession(nearlyExpired);

    expect(fresh.accessToken).toBe('refreshed-token');
    expect(String(fetchMock.mock.calls[0][0])).toContain('grant_type=refresh_token');
  });

  it('leaves a session that is still comfortably valid untouched', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    const healthy = {
      accessToken: 'still-good', refreshToken: 'r',
      expiresAtMs: Date.now() + 3_600_000,
    };
    expect((await validSession(healthy)).accessToken).toBe('still-good');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses to refresh without a refresh token', async () => {
    await expect(refreshSession({
      accessToken: 'a', refreshToken: '', expiresAtMs: 0,
    })).rejects.toThrow('cannot be refreshed');
  });

  it('clears local credentials even when the server revoke fails', async () => {
    window.localStorage.setItem('crossflow.session', JSON.stringify({
      accessToken: 'a', refreshToken: 'r', expiresAtMs: Date.now() + 1000,
    }));
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network down'); }));

    await signOut({ accessToken: 'a', refreshToken: 'r', expiresAtMs: Date.now() + 1000 });

    // Being unable to reach Supabase must not strand the user signed in.
    expect(readStoredSession()).toBeNull();
  });
});

describe('role resolution', () => {
  it('takes the role from the API, not from the token', async () => {
    const fetchMock = vi.fn<FetchLike>(async () => jsonResponse({
      user_id: 'user-1', role: 'DRIVER', display_name: 'Test Driver',
      expires_at: 1, role_source: 'crossflow_profiles',
    }));
    vi.stubGlobal('fetch', fetchMock);

    const identity = await fetchSession({
      accessToken: 'token', refreshToken: 'r', expiresAtMs: Date.now() + 1000,
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/api/auth/session');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer token');
    expect(identity.role).toBe('DRIVER');
    // The badge is only trustworthy because the server resolved it from the
    // database with the caller's own token.
    expect(identity.role_source).toBe('crossflow_profiles');
  });

  it('reports an expired session clearly on 401', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'nope' }, 401)));
    await expect(fetchSession({
      accessToken: 'stale', refreshToken: 'r', expiresAtMs: Date.now() + 1000,
    })).rejects.toThrow('session has expired');
  });
});

describe('github oauth', () => {
  function stubLocation(hash: string) {
    const replaceState = vi.fn();
    const assign = vi.fn();
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: {
        hash,
        pathname: '/',
        search: '',
        origin: 'http://localhost:3000',
        assign,
      },
    });
    Object.defineProperty(window, 'history', {
      configurable: true,
      value: { replaceState },
    });
    return { assign, replaceState };
  }

  it('sends the browser to Supabase with the provider and return URL', () => {
    const { assign } = stubLocation('');
    signInWithGitHub('http://localhost:3000');

    const target = new URL(assign.mock.calls[0][0] as string);
    expect(target.origin).toBe(SUPABASE_URL);
    expect(target.pathname).toBe('/auth/v1/authorize');
    expect(target.searchParams.get('provider')).toBe('github');
    expect(target.searchParams.get('redirect_to')).toBe('http://localhost:3000');
  });

  it('consumes tokens from the return fragment and stores the session', () => {
    stubLocation('#access_token=gh-token&refresh_token=gh-refresh&expires_in=3600&token_type=bearer');

    const session = completeOAuthRedirect();

    expect(session?.accessToken).toBe('gh-token');
    expect(session?.refreshToken).toBe('gh-refresh');
    expect(readStoredSession()?.accessToken).toBe('gh-token');
  });

  it('always strips the fragment so a live token cannot sit in the address bar', () => {
    const { replaceState } = stubLocation('#access_token=gh-token&expires_in=3600');
    completeOAuthRedirect();
    expect(replaceState).toHaveBeenCalledWith(null, '', '/');
  });

  it('reports a denied authorization and still clears the fragment', () => {
    const { replaceState } = stubLocation('#error=access_denied&error_description=The+user+denied+access');

    expect(() => completeOAuthRedirect()).toThrow('The user denied access');
    expect(replaceState).toHaveBeenCalled();
    expect(readStoredSession()).toBeNull();
  });

  it('ignores an ordinary page load with no fragment', () => {
    const { replaceState } = stubLocation('');
    expect(completeOAuthRedirect()).toBeNull();
    expect(replaceState).not.toHaveBeenCalled();
  });
});
