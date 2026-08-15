# Handoff — connecting CrossFlow auth to Supabase

> **You are picking up WP8 (authentication) at the point where the code is
> finished and tested, but has never spoken to a real Supabase project.**
> Your job is to connect it and prove the security boundary holds. Everything
> below is a checklist; the two checks in §6 are the only ones that gate the
> next work package.

| | |
|---|---|
| **Branch** | Historical implementation branch; verify the current checkout before applying any branch-specific guidance |
| **Scope done** | Backend auth: schema, transport, identity, endpoints, tests |
| **Scope not done** | Live connection, RLS verification, deliveries (WP9), driver view (WP10) |
| **Time to connect** | ~15 minutes if the project already exists |

---

## 1. What exists now

Everything auth-related lives in [`backend/auth/`](../backend/auth/).

| File | What it is |
|---|---|
| [`schema.sql`](../backend/auth/schema.sql) | `crossflow_profiles` table, RLS policies, role-guard trigger, auto-profile-on-signup trigger. **Not yet applied to any project.** |
| [`transport.py`](../backend/auth/transport.py) | Supabase HTTP calls that carry *the caller's* access token. Physically cannot read a service-role key. |
| [`identity.py`](../backend/auth/identity.py) | Verifies a token against `/auth/v1/user`, caches 60s, resolves role from the database. |
| [`routes.py`](../backend/auth/routes.py) | `GET /api/auth/status` (public), `/api/auth/session`, `/api/auth/admin-check`. |
| [`tests/test_auth.py`](../backend/auth/tests/test_auth.py) | 25 boundary tests, all passing. |

The production frontend now contains the sign-in flow in
[`frontend/src/services/auth.ts`](../frontend/src/services/auth.ts) and
[`frontend/src/components/auth/SignInPanel.tsx`](../frontend/src/components/auth/SignInPanel.tsx).
[`frontend/index.html`](../frontend/index.html) is the Vite application shell,
not a standalone login page. The backend router is mounted from
[`backend/main.py`](../backend/main.py), with configuration in `.env.example`
and a dedicated auth test job in CI.

**How sign-in works.** The client talks to Supabase Auth **directly** and sends
us the resulting access token. This API never receives a password, so there is
deliberately no `/api/auth/login`. Rate limiting, lockout and password reset are
Supabase's job.

---

## 2. Read this before you touch anything

There is exactly one mistake that breaks this system, and it is invisible.

Supabase has two kinds of key. The **secret / service-role** key
(`SUPABASE_SECRET_KEY`) **bypasses row-level security completely**. The
**publishable / anon** key does not.

If a user-scoped read is ever served with the secret key, every policy in
`schema.sql` becomes inert and any driver can read every other driver's journey.
**The application still looks and behaves correctly** — same endpoints, same
responses, correct-looking role in the UI. Nothing fails. You would ship it.

The existing repo already uses the secret key legitimately in
[`services/supabase_server.py`](../backend/services/supabase_server.py) for
backend-owned tables like ferry freshness. That is correct and must stay.

The defence is that `backend/auth/transport.py` never reads a secret key from
the environment at all, and a test asserts the string does not appear in the
module. If you extend the auth code, **do not import `supabase_server` into it**.

| Credential | Env var | Bypasses RLS? |
|---|---|---|
| Secret / service role | `SUPABASE_SECRET_KEY` | **Yes** — machine endpoints only |
| Publishable / anon | `SUPABASE_PUBLISHABLE_KEY` | No — safe in client code |
| The caller's access token | request header | No — **this is the boundary** |

---

## 3. Apply the schema

Supabase Dashboard → **SQL Editor** → New query. Paste all of
[`backend/auth/schema.sql`](../backend/auth/schema.sql) and run it. It is
rerunnable, so running it twice is harmless.

What it creates, and why each piece exists:

- **`crossflow_profiles`** — the server's source of truth for role. Roles are
  never read from the token, because users can write their own
  `user_metadata` through the Supabase client SDK.
- **`crossflow_current_role()` / `crossflow_is_admin()`** — `security definer`
  helpers. The admin read policy needs to check the caller's role, but querying
  `crossflow_profiles` from inside a `crossflow_profiles` policy recurses until
  Postgres aborts. These read outside RLS and terminate.
- **Trigger on `auth.users`** — every new account gets a profile automatically,
  always as `driver`.
- **`crossflow_guard_profile_role()`** — RLS is row-level, so a policy that lets
  you edit your own row lets you edit *every column* of it, including `role`.
  A row policy cannot express "rename yourself but don't promote yourself".
  This trigger is where that rule actually lives.

> If the `auth.users` trigger errors on your plan, stop and say so — the rest of
> the flow depends on profiles existing.

---

## 4. Configure and run the backend

`scripts/dev.sh` parses non-empty `KEY=VALUE` entries from the root `.env` and
does not execute the file as shell code. Direct `uvicorn` launches still need
the variables exported in the same shell that starts the server. The Vite
frontend has its own `frontend/.env.local` for `VITE_SUPABASE_URL` and
`VITE_SUPABASE_PUBLISHABLE_KEY`.

PowerShell, from the repo root:

```powershell
$env:CROSSFLOW_AUTH_MODE = "supabase"
$env:SUPABASE_URL = "https://<your-ref>.supabase.co"
$env:SUPABASE_PUBLISHABLE_KEY = "sb_publishable_..."
$env:CROSSFLOW_HISTORY_DB = ":memory:"
cd backend
..\.venv\Scripts\python.exe -m uvicorn main:app --port 8000
```

Both values are in Project Settings → API Keys. Use the **publishable** key
(or the legacy one labelled `anon` `public`). Not the secret one.

**Checkpoint.** In another terminal:

```powershell
curl.exe http://127.0.0.1:8000/api/auth/status
```

Expect `"enabled":true,"configured":true`. If `configured` is `false`, the URL
or key did not reach the process — check you set them in the *same* window.

Until `CROSSFLOW_AUTH_MODE=supabase` is set, auth stays off and every public
corridor, ferry and model endpoint keeps working normally. That is deliberate:
auth is a per-route dependency, never middleware, so an unreachable Supabase
cannot blank the app during a demo.

---

## 5. Create test users and sign in

Authentication → **Users** → Add user → Create new user. Twice:

- `admin@test.local`
- `driver@test.local`

**Tick "Auto Confirm User" on both.** Without it, sign-in fails with "Email not
confirmed" and it looks like the auth code is broken.

Promote one, using the UID shown in that list:

```sql
update public.crossflow_profiles set role = 'admin' where id = '<uuid>';
```

`UPDATE 0` means the signup trigger did not fire — go back to §3.

Note that this statement works because the SQL Editor connects as `postgres`.
The same statement from a driver session is rejected by the role-guard trigger,
which is the whole point.

**Checkpoint.** Start the normal development stack with `scripts/dev.sh`, after
putting `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY` in
`frontend/.env.local`. Open <http://localhost:3000>, then:

| Signed in as | `/api/auth/session` | `/api/auth/admin-check` |
|---|---|---|
| `driver@test.local` | `"role": "driver"` | **403** |
| `admin@test.local` | `"role": "admin"` | **200** |

---

## 6. The two checks that gate WP9

Everything above can pass while the security boundary is broken, because the API
would return the same answers either way. These two read the database directly
as the signed-in user, which is the only way to prove the policies work.

Sign in as the **driver**, open the browser console (F12) on that page:

For these direct RLS checks, use the legacy standalone diagnostic page
[`docs/auth-signin-demo.html`](auth-signin-demo.html), which intentionally
exposes `window.crossflow.supabase`. The production React dashboard keeps its
Supabase client private and does not expose that global.

**Check 1 — can a driver see other people?**

```js
const { data } = await window.crossflow.supabase.from('crossflow_profiles').select('*');
console.log(data);
```

- ✅ **Exactly one row** — their own.
- ❌ Two rows: RLS is not filtering. Nothing built on top of this is safe.

**Check 2 — can a driver promote themselves?**

```js
const { data: me } = await window.crossflow.supabase.auth.getUser();
const { error } = await window.crossflow.supabase
  .from('crossflow_profiles').update({ role: 'admin' }).eq('id', me.user.id);
console.log(error ?? 'NO ERROR — THIS IS BAD');
```

- ✅ An error mentioning administrator or privilege.
- ❌ "NO ERROR": the role-guard trigger did not apply. Re-run §3.

**If either check fails, do not start WP9.** The delivery tables inherit this
boundary; building on a broken one means every journey is readable by every
driver, and you will not find out from the UI.

---

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| `"configured": false` | URL or key not set in the shell that started uvicorn |
| 401 on every request | Token is not a JWT — check you are sending the Supabase access token, not the publishable key or `CROSSFLOW_ADMIN_TOKEN` |
| 503 on `/api/auth/session` | Supabase unreachable, or `CROSSFLOW_AUTH_MODE` not set to `supabase` |
| 403 "no CrossFlow profile" | The signup trigger did not create a row; check §3 |
| "Email not confirmed" | Auto Confirm was not ticked when creating the user |
| `ZoneInfoNotFoundError: Asia/Jakarta` | Windows only — see §8 |
| Role changes are ignored | Expected. Role is read from the database, never from the client |

---

## 8. Open items you are inheriting

**Timezone data is now an explicit production dependency.**
[`backend/requirements.txt`](../backend/requirements.txt) pins `tzdata` for all
platforms because `ZoneInfo("Asia/Jakarta")` must also work on Windows and slim
Linux images. The old Windows-only workaround is no longer needed.

**Historical test note.** Earlier Windows runs exposed SQLite cleanup failures
when tests used a connection context manager without closing the connection.
That test-only issue was fixed with `contextlib.closing`; use the current CI
workflow and verification commands below as the source of truth for present
runtime and duration.

**`transport.py` duplicates ~90 lines** of hardened request logic from
`services/supabase_server.py` (timeout controller, redirect refusal, response
size caps) rather than sharing it. That was a deliberate call: it means the
user-scoped module has no import path to the privileged one. The cost is that a
fix to one will not reach the other. Reversible if you would rather extract a
shared core.

---

## 9. What comes next

WP9 (deliveries persistence) appends `deliveries`, `delivery_stops` and
`journey_events` to `backend/auth/schema.sql`. Two rules carry over from the
freight pivot handoff:

- **`journey_events` is append-only.** Corrections are new rows. The audit trail
  is what makes the planned-vs-actual analytics real rather than modelled.
- **Never distinguish "not found" from "not yours."** `identity.not_yours_error()`
  already exists for this; use it for every driver-scoped lookup so a driver
  cannot discover which journey IDs exist by probing.

Full plan: [`docs/AUTH_BACKEND_ROADMAP.md`](AUTH_BACKEND_ROADMAP.md) §10 for the
connection steps in more detail. Delivery-specific persistence and driver
workflow work is outside the current auth boundary and should be documented in
its own current handoff when that work begins.

---

## 10. Verification commands

```powershell
# Auth boundary tests (fast)
.venv\Scripts\python.exe -m unittest discover -s backend/auth/tests -t . -p "test_*.py"

# Existing modular tests (~4 min)
.venv\Scripts\python.exe -m unittest discover -s backend/tests -t . -p "test_*.py"

# Full backend regression runner
.venv\Scripts\python.exe backend\test_backend.py
```

The recorded offline state is 25/25 auth and 105/105 modular tests. Re-run the
commands above after changing the checkout; this document is not a substitute
for current test output.

---

*This handoff describes the offline implementation and the remaining live
Supabase verification. The recorded test counts and branch notes are historical
and should be refreshed when the integration is connected.*
