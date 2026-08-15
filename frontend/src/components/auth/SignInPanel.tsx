import { useEffect, useId, useRef, useState } from 'react';
import { LogIn, LogOut, ShieldCheck, TriangleAlert, UserRound } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';
import type { AuthSession, AuthStatus, StoredSession } from '../../types';
import {
  AuthError,
  fetchSession,
  signIn,
  signOut,
  supabaseConfigured,
  validSession,
} from '../../services/auth';
import './SignInPanel.css';

interface SignInPanelProps {
  status: AuthStatus | null;
  session: StoredSession | null;
  identity: AuthSession | null;
  onSessionChange: (session: StoredSession | null) => void;
  onIdentityChange: (identity: AuthSession | null) => void;
  onClose: () => void;
}

export function SignInPanel({
  status,
  session,
  identity,
  onSessionChange,
  onIdentityChange,
  onClose,
}: SignInPanelProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const emailId = useId();
  const passwordId = useId();
  const emailRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!identity) emailRef.current?.focus();
  }, [identity]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  // The server decides whether signing in is even possible. Showing a form
  // when Supabase is unreachable would invite a failure the user cannot fix,
  // while the public corridor views keep working regardless.
  const unavailableReason = !supabaseConfigured()
    ? 'Sign-in is not configured in this build. Set VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY.'
    : status && !status.enabled
      ? 'Sign-in is turned off on this server (CROSSFLOW_AUTH_MODE is not "supabase").'
      : status && !status.configured
        ? 'The server cannot reach Supabase, so sign-in is unavailable right now.'
        : null;

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const stored = await signIn(email, password);
      onSessionChange(stored);
      const resolved = await fetchSession(stored);
      onIdentityChange(resolved);
      setPassword('');
    } catch (caught) {
      const message = caught instanceof AuthError
        ? caught.message
        : 'Sign-in failed. Please try again.';
      setError(message);
      onSessionChange(null);
      onIdentityChange(null);
    } finally {
      setBusy(false);
    }
  };

  const handleSignOut = async () => {
    setBusy(true);
    await signOut(session);
    onSessionChange(null);
    onIdentityChange(null);
    setBusy(false);
    setEmail('');
    setPassword('');
  };

  const handleRecheck = async () => {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      const fresh = await validSession(session);
      onSessionChange(fresh);
      onIdentityChange(await fetchSession(fresh));
    } catch (caught) {
      setError(caught instanceof AuthError ? caught.message : 'Could not refresh your session.');
      onSessionChange(null);
      onIdentityChange(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="signin-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="glass-panel signin-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="signin-title"
      >
        <h2 id="signin-title" className="signin-panel__title">
          <ShieldCheck aria-hidden="true" size={ICON_SIZE.large} color="var(--accent-cyan)" />
          {identity ? 'Signed in' : 'Sign in to CrossFlow'}
        </h2>

        {identity ? (
          <>
            <dl className="signin-identity">
              <dt>Signed in as</dt>
              <dd>{identity.display_name || identity.user_id}</dd>
              <dt>Role</dt>
              <dd>
                <span className={`badge signin-role signin-role--${identity.role.toLowerCase()}`}>
                  {identity.role}
                </span>
              </dd>
            </dl>
            {/* Naming the source matters: the badge is only trustworthy
                because the server read it from the database, not the token. */}
            <p className="signin-panel__note">
              Role resolved by the server from <code>{identity.role_source}</code>, not from the
              access token.
            </p>
            {error ? (
              <p className="signin-panel__error" role="alert">
                <TriangleAlert aria-hidden="true" size={ICON_SIZE.medium} /> {error}
              </p>
            ) : null}
            <div className="signin-panel__actions">
              <button
                type="button"
                className="ui-button-primary"
                onClick={handleRecheck}
                disabled={busy}
              >
                <UserRound aria-hidden="true" size={ICON_SIZE.medium} /> Re-check role
              </button>
              <button
                type="button"
                className="signin-panel__secondary"
                onClick={handleSignOut}
                disabled={busy}
              >
                <LogOut aria-hidden="true" size={ICON_SIZE.medium} /> Sign out
              </button>
            </div>
          </>
        ) : unavailableReason ? (
          <p className="signin-panel__error" role="status">
            <TriangleAlert aria-hidden="true" size={ICON_SIZE.medium} /> {unavailableReason}
          </p>
        ) : (
          <form className="signin-form" onSubmit={handleSubmit}>
            <p className="signin-panel__note">
              Your password goes to Supabase directly and never reaches the CrossFlow API.
            </p>

            <label className="signin-field" htmlFor={emailId}>
              <span>Email</span>
              <input
                id={emailId}
                ref={emailRef}
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                disabled={busy}
              />
            </label>

            <label className="signin-field" htmlFor={passwordId}>
              <span>Password</span>
              <input
                id={passwordId}
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={busy}
              />
            </label>

            {error ? (
              <p className="signin-panel__error" role="alert">
                <TriangleAlert aria-hidden="true" size={ICON_SIZE.medium} /> {error}
              </p>
            ) : null}

            <div className="signin-panel__actions">
              <button type="submit" className="ui-button-primary" disabled={busy}>
                <LogIn aria-hidden="true" size={ICON_SIZE.medium} />
                {busy ? 'Signing in…' : 'Sign in'}
              </button>
              <button
                type="button"
                className="signin-panel__secondary"
                onClick={onClose}
                disabled={busy}
              >
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
