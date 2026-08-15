import { useEffect, useId, useRef, useState } from 'react';
import { CarFront, LogIn, LogOut, ShieldCheck, TriangleAlert, UserRound } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';
import type { AuthSession, AuthStatus, StoredSession } from '../../types';
import {
  AuthError,
  fetchSession,
  projectMismatch,
  signIn,
  signInWithGitHub,
  signOut,
  supabaseConfigured,
  validSession,
} from '../../services/auth';
import './SignInPanel.css';

/** lucide-react dropped brand marks, and one inline path beats a new dependency. */
function GitHubMark({ size = 18 }: { size?: number }) {
  return (
    <svg
      aria-hidden="true"
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="currentColor"
    >
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

interface SignInPanelProps {
  status: AuthStatus | null;
  session: StoredSession | null;
  identity: AuthSession | null;
  onSessionChange: (session: StoredSession | null) => void;
  onIdentityChange: (identity: AuthSession | null) => void;
  onClose: () => void;
  /** 'page' is the full-screen gate shown before the workspace loads. */
  variant?: 'modal' | 'page';
  /** Gate only: proceed without an account. */
  onContinueAsGuest?: () => void;
}

export function SignInPanel({
  status,
  session,
  identity,
  onSessionChange,
  onIdentityChange,
  onClose,
  variant = 'modal',
  onContinueAsGuest,
}: SignInPanelProps) {
  const isGate = variant === 'page';
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
    if (isGate) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [isGate, onClose]);

  // The server decides whether signing in is even possible. Showing a form
  // when Supabase is unreachable would invite a failure the user cannot fix,
  // while the public corridor views keep working regardless.
  const unavailableReason = !supabaseConfigured()
    ? 'Sign-in is not configured in this build. Set VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY.'
    : status && !status.enabled
      ? 'Sign-in is turned off on this server (CROSSFLOW_AUTH_MODE is not "supabase").'
      : status && !status.configured
        ? 'The server cannot reach Supabase, so sign-in is unavailable right now.'
        : projectMismatch(status);

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

  const handleGitHub = () => {
    setError(null);
    try {
      // Navigates away; anything after this line only runs if it threw.
      signInWithGitHub();
    } catch (caught) {
      setError(caught instanceof AuthError ? caught.message : 'Could not start GitHub sign-in.');
    }
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
      className={isGate ? 'signin-gate' : 'signin-backdrop'}
      role="presentation"
      onMouseDown={(event) => {
        if (!isGate && event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="glass-panel signin-panel"
        role={isGate ? 'region' : 'dialog'}
        aria-modal={isGate ? undefined : true}
        aria-labelledby="signin-title"
      >
        {isGate ? (
          <p className="signin-gate__brand">CrossFlow · Batam–Singapore mobility</p>
        ) : null}
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
          <>
            <p className="signin-panel__error" role="status">
              <TriangleAlert aria-hidden="true" size={ICON_SIZE.medium} /> {unavailableReason}
            </p>
            {/* Driver mode remains available when operator sign-in is down. */}
            {isGate ? (
              <div className="signin-panel__actions">
                <button
                  type="button"
                  className="signin-panel__secondary signin-panel__driver-button"
                  onClick={onContinueAsGuest}
                >
                  <CarFront aria-hidden="true" size={ICON_SIZE.medium} />
                  Continue as Driver
                </button>
              </div>
            ) : null}
          </>
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
              <button
                type="button"
                className="signin-panel__secondary signin-panel__driver-button"
                onClick={isGate ? onContinueAsGuest : onClose}
                disabled={busy}
              >
                <CarFront aria-hidden="true" size={ICON_SIZE.medium} />
                {isGate ? 'Continue as Driver' : 'Cancel'}
              </button>
              <button type="submit" className="ui-button-primary" disabled={busy}>
                <LogIn aria-hidden="true" size={ICON_SIZE.medium} />
                {busy ? 'Signing in…' : 'Sign in'}
              </button>
            </div>

            <div className="signin-divider" aria-hidden="true"><span>or</span></div>

            <button
              type="button"
              className="signin-provider"
              onClick={handleGitHub}
              disabled={busy}
            >
              <GitHubMark size={ICON_SIZE.large} />
              Continue with GitHub
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
