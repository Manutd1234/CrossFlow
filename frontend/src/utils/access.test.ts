import { describe, expect, it } from 'vitest';
import { identityKey, isDriverView } from './access';

const asIdentity = (role: string, user_id = 'user-1') => ({ role, user_id });

describe('driver view resolution', () => {
  it('gives a guest the reduced driver view', () => {
    expect(isDriverView(null, true)).toBe(true);
  });

  it('gives an unauthenticated non-guest the full view', () => {
    expect(isDriverView(null, false)).toBe(false);
  });

  it('gives a signed-in driver the reduced view', () => {
    expect(isDriverView(asIdentity('driver'), false)).toBe(true);
  });

  it('gives a signed-in admin the full workspace', () => {
    expect(isDriverView(asIdentity('admin'), false)).toBe(false);
  });

  it('promotes an admin who signed in after continuing as a guest', () => {
    // The regression this guards: the guest flag outlives the guest session,
    // so an admin signing in from the driver screen kept the two-tab layout.
    expect(isDriverView(asIdentity('admin'), true)).toBe(false);
  });

  it('keeps a driver on the reduced view regardless of the guest flag', () => {
    expect(isDriverView(asIdentity('driver'), true)).toBe(true);
  });

  it('treats an unknown role as a full-workspace account, not a driver', () => {
    expect(isDriverView(asIdentity('dispatcher'), false)).toBe(false);
  });

  it('matches the driver role irrespective of case or stray whitespace', () => {
    expect(isDriverView(asIdentity('DRIVER'), false)).toBe(true);
    expect(isDriverView(asIdentity(' Driver '), false)).toBe(true);
  });
});

describe('identity change detection', () => {
  it('is null when signed out', () => {
    expect(identityKey(null)).toBeNull();
  });

  it('is stable for the same account and role', () => {
    expect(identityKey(asIdentity('admin'))).toBe(identityKey(asIdentity('admin')));
  });

  it('changes when a different account signs in', () => {
    expect(identityKey(asIdentity('admin', 'a')))
      .not.toBe(identityKey(asIdentity('admin', 'b')));
  });

  it('changes when the same account changes role', () => {
    // A demotion swaps which workspace the person should see, so it has to
    // count as a switch and reset the view.
    expect(identityKey(asIdentity('admin', 'a')))
      .not.toBe(identityKey(asIdentity('driver', 'a')));
  });

  it('changes on sign-out from any account', () => {
    expect(identityKey(asIdentity('admin'))).not.toBe(identityKey(null));
  });
});
