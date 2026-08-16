import type { AuthSession } from '../types';

/**
 * Whether the workspace should render its reduced driver view.
 *
 * A resolved account always wins over the remembered guest flag. Someone who
 * clicks "Continue as Driver" and then signs in as an operator must get the
 * full workspace: the guest flag survives in sessionStorage for the rest of the
 * tab's life, so consulting it first would pin them to the two-tab driver
 * layout no matter who they signed in as.
 */
export function isDriverView(
  identity: Pick<AuthSession, 'role'> | null,
  isGuest: boolean,
): boolean {
  if (identity) return identity.role.trim().toLowerCase() === 'driver';
  return isGuest;
}

/**
 * A stable key for "who is signed in", used to detect an account switch.
 *
 * Signing out yields null, and a role change on the same account counts as a
 * switch because it changes which workspace the person should be looking at.
 */
export function identityKey(
  identity: Pick<AuthSession, 'user_id' | 'role'> | null,
): string | null {
  return identity ? `${identity.user_id}:${identity.role}` : null;
}
