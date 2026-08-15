/* @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthSession, AuthStatus, StoredSession } from '../../types';

const authMocks = vi.hoisted(() => ({
  fetchSession: vi.fn(),
  signIn: vi.fn(),
  signInAsTestAdmin: vi.fn(),
  signOut: vi.fn(),
  validSession: vi.fn(),
}));

vi.mock('../../services/auth', () => ({
  AuthError: class AuthError extends Error {},
  fetchSession: authMocks.fetchSession,
  projectMismatch: () => null,
  signIn: authMocks.signIn,
  signInAsTestAdmin: authMocks.signInAsTestAdmin,
  signOut: authMocks.signOut,
  supabaseConfigured: () => true,
  testAdminConfigured: () => true,
  validSession: authMocks.validSession,
}));

import { SignInPanel } from './SignInPanel';

const STATUS: AuthStatus = {
  mode: 'supabase',
  enabled: true,
  configured: true,
  project_origin: 'https://test.supabase.co',
  sign_in: 'supabase_auth_direct',
  notes: '',
};

const SESSION: StoredSession = {
  accessToken: 'test-admin-token',
  refreshToken: 'test-admin-refresh',
  expiresAtMs: Date.now() + 3_600_000,
};

const ADMIN: AuthSession = {
  user_id: 'admin-id',
  role: 'admin',
  display_name: 'Test Admin',
  expires_at: null,
  role_source: 'crossflow_profiles',
};

let root: Root | undefined;

function renderPanel(
  onSessionChange = vi.fn(),
  onIdentityChange = vi.fn(),
) {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(
    <SignInPanel
      status={STATUS}
      session={null}
      identity={null}
      onSessionChange={onSessionChange}
      onIdentityChange={onIdentityChange}
      onClose={vi.fn()}
    />,
  ));
  return { container, onSessionChange, onIdentityChange };
}

beforeEach(() => {
  authMocks.fetchSession.mockResolvedValue(ADMIN);
  authMocks.signInAsTestAdmin.mockResolvedValue(SESSION);
  authMocks.signOut.mockResolvedValue(undefined);
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.clearAllMocks();
});

describe('test admin access', () => {
  it('signs in with the shared account and accepts its server-resolved admin role', async () => {
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    const button = Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
      .find(candidate => candidate.textContent?.includes('Sign in as test admin'));

    expect(button).toBeDefined();
    await act(async () => button?.click());

    expect(authMocks.signInAsTestAdmin).toHaveBeenCalledOnce();
    expect(authMocks.fetchSession).toHaveBeenCalledWith(SESSION);
    expect(onSessionChange).toHaveBeenCalledWith(SESSION);
    expect(onIdentityChange).toHaveBeenCalledWith(ADMIN);
  });

  it('rejects a configured test account that the server resolves as a driver', async () => {
    authMocks.fetchSession.mockResolvedValue({ ...ADMIN, role: 'driver' });
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    const button = Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
      .find(candidate => candidate.textContent?.includes('Sign in as test admin'));

    await act(async () => button?.click());

    expect(authMocks.signOut).toHaveBeenCalledWith(SESSION);
    expect(onSessionChange).not.toHaveBeenCalledWith(SESSION);
    expect(onIdentityChange).not.toHaveBeenCalledWith(expect.objectContaining({ role: 'driver' }));
    expect(container.textContent).toContain('configured test account is not an admin');
  });
});
