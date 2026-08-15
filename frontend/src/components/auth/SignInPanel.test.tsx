/* @vitest-environment jsdom */

import { act } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { SignInPanel } from './SignInPanel';

let root: Root | undefined;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
});

describe('signed-in account panel', () => {
  it('presents every authenticated identity as an admin with a right-aligned primary sign-out action', () => {
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);

    act(() => root?.render(
      <SignInPanel
        status={null}
        session={{ accessToken: 'access', refreshToken: 'refresh', expiresAtMs: 1 }}
        identity={{
          user_id: 'user-1',
          display_name: 'Test User',
          role: 'driver',
          expires_at: 1,
          role_source: 'crossflow_profiles',
        }}
        onSessionChange={vi.fn()}
        onIdentityChange={vi.fn()}
        onClose={vi.fn()}
      />,
    ));

    expect(container.querySelector('.signin-role')?.textContent).toContain('Admin');
    expect(container.textContent).not.toContain('Re-check role');
    expect(container.textContent).not.toContain('Role resolved by the server');

    const signOut = Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Sign out'));
    expect(signOut?.classList.contains('ui-button-primary')).toBe(true);
    expect(signOut?.classList.contains('signin-panel__signout')).toBe(true);
  });
});
