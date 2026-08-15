# Auth Backend Roadmap — WP8

> Implementation roadmap for the authentication half of the freight pivot.
> This document is retained as the design record; the implementation described
> below is now present in the current checkout. Re-verify the live Supabase steps
> before treating the integration as complete.

## Status

| Phase | State | Evidence |
|---|---|---|
| A1 schema and RLS | Written, **not yet applied** to the project | `backend/auth/schema.sql` |
| A2 user-scoped transport | Done | `backend/auth/transport.py` |
| A3 identity and role resolution | Done | `backend/auth/identity.py` |
| A4 API surface | Done | 3 endpoints live in the OpenAPI schema |
| A5 boundary tests | Done | 25 tests, all passing |
| A6 frontend sign-in flow | Done | `frontend/src/services/auth.ts`, `frontend/src/components/auth/SignInPanel.tsx` |
| A7 configuration and docs | Done | `.env.example`, README, CI step |

Verified locally: 25/25 auth tests pass; the 105 existing modular tests still
pass; auth endpoints register alongside the public ones; a missing, malformed or
machine-secret bearer token returns 401; and with auth enabled against an
unreachable project, `/api/auth/session` returns 503 while `/api/corridors` and
`/api/ferries` keep answering 200.

**Not yet verified:** anything requiring the live project — the RLS policies
themselves, the signup trigger, and a real end-to-end sign-in. That is the A1
acceptance gate and needs the SQL applied first (§10).

---

## 1. What the repo actually looks like

Facts established by reading the code, because two of them contradict the
handoff and change the plan.

| Finding | Consequence |
|---|---|
| **The frontend is the React/Vite workspace.** The auth service and sign-in panel are part of the build. | The old disposable-client assumption is obsolete; verify auth through the dashboard at `http://localhost:3000` with `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`. |
| [`supabase_server.py`](../backend/services/supabase_server.py) is **deliberately service-role-only** — its docstring says it "intentionally does not know about browser/anonymous keys", and `_REST_PATH` hard-restricts paths to `^/rest/v1/…`. | We **cannot** route user-scoped calls through it, and must not weaken it. A second, user-scoped transport is required. This is the mechanical form of the handoff's headline warning. |
| Zero third-party HTTP or crypto deps. Everything is stdlib `urllib` ([`backend/requirements.txt`](../backend/requirements.txt): fastapi, uvicorn, pydantic, numpy, scikit-learn, certifi, tzdata). | Adding `PyJWT[crypto]` for local JWT verification would be the first crypto dependency in the project and inflate the Vercel bundle. Drives Decision D1. |
| `_require_admin()` ([`main.py:681`](../backend/main.py#L681)) is a `secrets.compare_digest` check on `CROSSFLOW_ADMIN_TOKEN`, guarding the protected machine-operation endpoints. | Stays separate from human Supabase auth. Two mechanisms, two purposes. |
| CORS is `allow_origins=["*"]` with `allow_credentials=False` ([`main.py:76`](../backend/main.py#L76)). | Correct and needs **no change** — we authenticate with an `Authorization` header, not cookies. Documented so nobody "fixes" it into a wildcard+credentials violation. |
| SQL convention: RLS on, explicit `revoke … from public, anon, authenticated`, `set search_path`, guarded rerunnable DDL. See [`routing_intelligence.sql`](../backend/data/routing_intelligence.sql). | [`backend/auth/schema.sql`](../backend/auth/schema.sql) follows the same house style and intentionally grants `authenticated` access only where RLS is the boundary. |
| Optional stores fail closed behind an explicit env opt-in (`CROSSFLOW_ROUTING_INTELLIGENCE_STORE=local\|supabase`). | Auth reuses this exact pattern: `CROSSFLOW_AUTH_MODE=disabled\|supabase`. Public endpoints keep working when it is off. |
| Auth tests are `unittest` and call endpoint functions directly with `patch.dict(os.environ, …)`; CI also runs the function-style backend tests with pytest. | The auth boundary suite is run explicitly, alongside the broader backend and frontend checks. |
| [`tls.py`](../backend/services/tls.py) exists as the one verified trust store, but `supabase_server` builds its opener without it. | The new transport uses `tls.default_context()`. Pre-existing gap, cheap to not repeat. |

---

## 2. The credential boundary

This is the whole security design in one table. Getting it wrong is the
handoff's named catastrophic failure — RLS silently inert, every driver able to
read every journey, and **the app looks completely correct in testing**.

| Credential | Env var | Used by | Sees RLS? |
|---|---|---|---|
| Service role secret | `SUPABASE_SECRET_KEY` | Existing machine endpoints, background jobs | **No — bypasses it** |
| Publishable / anon key | `SUPABASE_PUBLISHABLE_KEY` *(new)* | `apikey` header on user-scoped calls | n/a (identifies project only) |
| The caller's own access token | request `Authorization` header | Every admin/driver read and write | **Yes — this is the boundary** |

Enforced structurally, not by convention:

- `auth/transport.py` **has no access to** `SUPABASE_SECRET_KEY`. It never reads
  that variable, so it cannot accidentally send it.
- `supabase_server.py` stays anon-blind. Neither module can become the other.
- A test asserts the same driver query returns **different** results through the
  two transports. If they match, RLS is being bypassed and the test fails.

---

## 3. Target module map

```
backend/
  auth/schema.sql                   DONE profiles + RLS policies
  auth/transport.py                 DONE user-JWT transport, publishable apikey
  auth/identity.py                  DONE token verification and role resolution
  auth/routes.py                    DONE /api/auth/status, /session, /admin-check
  auth/tests/test_auth.py           DONE security-boundary tests
  main.py                           DONE auth router integration
```

Request flow for a protected endpoint:

```
client ──Authorization: Bearer <supabase access token>──▶ FastAPI
                                                            │
                                    auth.require_user() ────┤
                                      1. verify token  ─────┼──▶ Supabase /auth/v1/user
                                      2. resolve role  ─────┼──▶ PostgREST /rest/v1/profiles
                                         (via the caller's own JWT, RLS applies)
                                                            │
                                    handler receives AuthenticatedUser(id, role)
```

Role is **never** read from the request body, and never from the JWT's own
`user_metadata` — both are user-writable through Supabase's own client SDK.
It is resolved from the `profiles` table on every request.

---

## 4. Decisions

*All listed decisions are settled. Recorded with their reasoning so a later reader can tell
which are load-bearing.*

**D1 — Token verification: call `/auth/v1/user`, do not verify locally.**
**Confirmed.** Costs one network hop (cached, §5 A3) but adds zero
dependencies, cannot get JWKS rotation wrong, and is authoritative about
revocation. Local ES256 verification via PyJWT is faster and offline-capable,
but it is a new crypto dependency and a class of subtle bugs (alg confusion,
key caching, clock skew) we do not need on a hackathon clock. Reversible later
behind the same `identity.py` interface.

**D2 — The backend does not handle passwords. Confirmed.** Login, refresh and
logout go client → Supabase Auth directly; the client sends us the resulting
access token. We never see a password, never store one, and inherit Supabase's
rate limiting, lockout and password-reset flows for free. The backend exposes
`GET /api/auth/session` (whoami) so a client can confirm its token and learn
its resolved role. There is deliberately **no** `POST /api/auth/login`.

**D5 — A Supabase project is already provisioned.** Every phase is still built
against a mocked transport so CI stays hermetic and offline, but the SQL in A1
gets applied for real and the boundary tests in A5 can additionally be run live
against the project. Live verification is the acceptance gate for A1, not an
optional extra.

**D6 — The current frontend is a React/Vite sign-in flow.**
`frontend/src/services/auth.ts` talks to Supabase Auth directly, stores and
refreshes the browser session, and calls `/api/auth/session` with the resulting
Bearer token. `SignInPanel` renders the flow inside the dashboard; it is covered
by the frontend test suite. `frontend/index.html` is only the Vite application
shell.

**D3 — `profiles.role` is admin-writable only, and there is no self-service
signup path to admin.** A new user gets a `driver` profile by database trigger
on `auth.users` insert. Promoting to admin is a manual SQL/console action or an
admin-authored call. Prevents the "first user signs up as admin" hole.

**D4 — Fail closed on protected routes, stay up on public ones.** If Supabase
is unreachable, `/api/auth/session` and anything requiring a role return **503
with a stable, non-leaky message**; corridor, hotspot, ferry and model
endpoints are untouched and keep serving. Auth is never a global middleware —
it is a per-endpoint dependency, so it cannot take down the demo.

---

## 5. Build phases

Ordered so each phase is independently testable and the riskiest thing (the
credential boundary) is proven before anything depends on it.

### A1 · Schema and RLS · `backend/auth/schema.sql`

```sql
create table if not exists public.crossflow_profiles (
  id           uuid primary key references auth.users(id) on delete cascade,
  role         text not null default 'driver' check (role in ('admin','driver')),
  display_name text not null check (length(display_name) between 1 and 120),
  created_at   timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
```

Policies, written in the file next to the table per house convention:

- `select` — a user may read **their own row only**.
- `update` — a user may update `display_name` on their own row; a `before
  update` trigger **rejects any change to `role`** unless performed by
  `service_role`. RLS alone cannot protect a single column, so the trigger is
  the actual enforcement — this is the D3 hole closed properly.
- `insert` / `delete` — no policy for `authenticated`. Rows are created by an
  `after insert on auth.users` trigger (`security definer`, `set search_path`).
- Admins read all profiles via a `security definer` helper
  `crossflow_current_role()` rather than a self-referential `profiles` subquery
  in the policy — the naive version causes infinite RLS recursion, which is the
  single most common way this schema gets written wrong.

Deliveries tables are **deliberately not in this phase** — they are WP9. The
file is named for both because WP9 appends to it.

**Acceptance:** applies cleanly in a fresh Supabase project, reruns without
error, and `crossflow_auth_health()` reports `schema_version`, enabled RLS,
`authenticated_can_read=true`, and `authenticated_cannot_insert=true`. The
role-update trigger must separately reject a driver changing `profiles.role`.

### A2 · User-scoped transport · `backend/auth/transport.py`

The implementation keeps the user-scoped transport separate from the
service-role-only transport. It adds `/auth/v1/…` access, an
`apikey: <publishable>` plus `Authorization: Bearer <user token>` header, and
the verified TLS context.
- Access token format validation before any network call — three base64url
   segments, bounded length, no control bytes. A malformed token must never
   reach an outbound header.

**Acceptance:** a unit test proves the module reads no secret-key env var, and
that a crafted token containing CR/LF or a `..` path segment is rejected before
`open` is called.

### A3 · Identity and role resolution · `backend/auth/identity.py`

```python
@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    id: str          # auth.uid()
    role: str        # 'admin' | 'driver', resolved from profiles
    access_token: str  # forwarded to PostgREST so RLS applies downstream
```

- `authenticate(token)` verifies the token through `/auth/v1/user` and reads the
  caller's profile through the user-scoped transport.
- **Verification cache:** keyed by `sha256(token)`, TTL 60 s, capped size,
  successful verifications only, and never cached beyond the token's own `exp`.
  Bounds the network cost of a chatty driver UI without meaningfully extending
  a revoked token's life.
- `_read_profile()` resolves the role **through the caller's JWT**. A user with
  no profile row is a hard `403`, not a defaulted `driver`.
- `require_user()` / `require_admin_user()` are the FastAPI dependencies. Note
  the deliberate name: `require_admin_user` (human, Supabase) is a different
  function from the existing `_require_admin` (machine, shared token) and the
  two must never be merged.
- `not_yours_error()` returns the shared non-disclosing error shape for future
  driver-scoped lookups.

**Acceptance:** role always comes from `profiles`; a token whose
`user_metadata.role = "admin"` still resolves to `driver`.

### A4 · API surface · `main.py`

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /api/auth/session` | any valid token | `{ user_id, role, display_name, expires_at }` |
| `GET /api/auth/status` | public | `{ mode, enabled, configured, project_origin, sign_in, notes }` — reports configuration, not a live reachability probe |

`/api/auth/status` is what makes D4's graceful degradation visible instead of
implicit. Both carry the existing `private, no-store` headers used by the
routing-intelligence endpoints.

No existing endpoint changes behaviour in this phase — freight/deliveries
endpoints get their guards in WP9.

### A5 · Tests · `backend/auth/tests/test_auth.py`

Straight from the handoff's §7 list, backend-testable subset:

1. **RLS is actually active** — same profile query through the user transport
   and the service transport returns different results. *If this passes
   trivially, the boundary is broken.*
2. **Role cannot be self-assigned** — body-declared and `user_metadata`-declared
   `admin` are both ignored.
3. **No existence leak** — unknown-but-valid-shaped id and fictional id produce
   byte-identical responses.
4. **Public routes stay public** — corridor/hotspot/model endpoints answer with
   no `Authorization` header and with `CROSSFLOW_AUTH_MODE=disabled`.
5. **Auth outage degrades, not fails** — transport raises, public endpoints
   still 200, protected endpoints 503 with a stable message.
6. **Machine token is not a user token** — `CROSSFLOW_ADMIN_TOKEN` presented as
   a Bearer token is rejected; a valid admin *user* token does **not** open the
   machine ingestion endpoints. Both directions.
7. Malformed / expired / absent token → 401, never 500.

Test 6 is not in the handoff. It is the test that proves the two mechanisms
stayed separate, which is the guardrail most likely to erode under time
pressure.

### A6 · Frontend sign-in flow · `frontend/src/services/auth.ts`

The React service signs in directly with Supabase, then calls
`GET /api/auth/session` with the returned access token and renders
`{ user_id, role, display_name }` through `SignInPanel`.

Its real job is to be **the executable form of the D2 contract** — it shows the
user exactly where the password goes (Supabase, not us) and exactly what the
backend expects (a Bearer token). It also handles refresh, guest continuation,
project mismatch, and graceful auth outages.

The publishable key is embedded in the page, which is correct and intended —
that key is designed to be public and is useless without a user session and the
RLS policies from A1.

### A7 · Configuration and documentation

- `.env.example` and the README document the server credentials and auth mode;
  frontend builds use `VITE_SUPABASE_URL` and
  `VITE_SUPABASE_PUBLISHABLE_KEY`.
- CI runs the backend auth boundary suite explicitly and the frontend auth tests
  as part of the normal Vitest run.

---

## 6. Suggested order of work

```
A1 schema ──▶ A2 transport ──▶ A3 identity.py ──▶ A4 endpoints ──┬─▶ A6 client ──▶ A7 docs
                    └──────────▶ A5 tests ◀──────────┘       │
                                                              └─▶ live check against the real project (D5)
```

A5's boundary tests (1 and 2) should be written **during** A3, not after — they
are the acceptance criteria for A3, not a follow-up chore. The handoff is
explicit that WP9 must not start until driver isolation is proven, and the
proof lives here.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| **RLS silently bypassed** — the catastrophic one. Everything looks right. | Structural separation (§2) plus test A5.1, which fails if the two transports agree. |
| **Infinite recursion in the admin RLS policy** — a `profiles` policy that queries `profiles`. | `security definer` helper function, never a self-referential subquery. Called out in A1. |
| **Role escalation via `user_metadata`** — users can write their own metadata through the Supabase client. | Role read from `profiles` only; test A5.2. |
| Extracting the transport core breaks the hardened service path. | Signature-preserving refactor; existing tests are the net; do it in its own commit so it can be reverted alone. |
| Live project drifts from the committed SQL. | Tests run against mocked transports so CI stays hermetic; A1 additionally carries a `crossflow_auth_health()` probe so drift is detectable rather than silent. |
| Verification cache extends a revoked token's life. | 60 s TTL, successful-only, never past `exp`. Explicitly documented rather than tuned silently. |

---

## 8. Out of scope

- **Deliveries, stops, journey events** (WP9) and the **driver view** (WP10).
  This roadmap builds the boundary they sit behind and nothing more.
- **Delivery-specific admin scheduling and driver workflows** beyond the current
  sign-in/session boundary.
- **Freight routing** (WP1–WP7) — the other track entirely.
- Password reset, email confirmation, MFA — Supabase Auth features, configured
  in its console, not code we write.

---

## 10. Turning it on

The code path is implemented. The remaining acceptance gate is applying the
schema to a real Supabase project and proving RLS and end-to-end sign-in there.

1. **Apply the schema.** Paste `backend/auth/schema.sql` into the Supabase SQL
   Editor and run it. It is rerunnable.
2. **Configure the backend.** In `.env`:
   ```
   CROSSFLOW_AUTH_MODE=supabase
   SUPABASE_URL=https://<your-ref>.supabase.co
   SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
   ```
   The publishable key is under Project Settings → API Keys. Do **not** use the
   secret key here.
3. **Create two users** in Authentication → Users. Both get a `driver` profile
   automatically. Promote one:
   ```sql
   update public.crossflow_profiles set role = 'admin' where id = '<uuid>';
   ```
   Run it in the SQL Editor, which connects as `postgres` — the role guard
   trigger rejects the same statement from a driver session, which is the point.
4. **Check it end to end.** Put `VITE_SUPABASE_URL` and
   `VITE_SUPABASE_PUBLISHABLE_KEY` in `frontend/.env.local`, start the stack with
   `scripts/dev.sh`, and open `http://localhost:3000`.
   `/api/auth/session` should report the role that came from the database, and
   `/api/auth/admin-check` should return 200 for the admin and 403 for the
   driver.

**The check that matters most.** Signed in as the driver, open the legacy
standalone diagnostic page [`docs/auth-signin-demo.html`](auth-signin-demo.html)
and run this in its browser console. The React dashboard intentionally does not
expose the Supabase client globally.

```js
const { data, error } = await window.crossflow.supabase
  .from('crossflow_profiles').select('*');
```

It must return **exactly one row** — their own. More than one row means the
policies are not doing their job, and no amount of correct-looking API code
compensates for that. Then try promoting themselves:

```js
await window.crossflow.supabase.from('crossflow_profiles')
  .update({ role: 'admin' }).eq('id', '<their own id>');
```

It must fail. If it succeeds, the role-guard trigger did not apply and WP9 must
not start.
