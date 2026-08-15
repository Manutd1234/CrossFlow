-- CrossFlow human authentication and delivery ownership.
-- Run once in the Supabase SQL Editor. Safe to rerun.
--
-- This schema differs from every other CrossFlow table on one deliberate point.
-- routing_intelligence.sql and ferry_freshness.sql enable RLS and then grant
-- nothing to browser roles, because the backend reaches them only with
-- SUPABASE_SECRET_KEY, which bypasses RLS entirely. Here the opposite is true:
-- admins and drivers read their own rows with their own access tokens, so the
-- policies below are the actual security boundary rather than a formality.
--
-- If any user-scoped read is ever served with the service-role key, every
-- policy in this file becomes silently inert and any driver can read any
-- journey. backend/auth/transport.py exists to make that structurally
-- impossible; see docs/AUTH_BACKEND_ROADMAP.md section 2.

create table if not exists public.crossflow_profiles (
  id           uuid primary key references auth.users(id) on delete cascade,
  role         text not null default 'driver' check (role in ('admin', 'driver')),
  display_name text not null check (length(display_name) between 1 and 120),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create index if not exists crossflow_profiles_role_idx
  on public.crossflow_profiles (role);

-- Resolve the caller's role without reading the table through RLS.
--
-- The obvious way to write the admin policies below is a subquery against
-- crossflow_profiles. That policy would then be evaluated while checking access
-- to crossflow_profiles, which recurses until Postgres aborts with
-- "infinite recursion detected in policy". A security definer function reads
-- the row as the owner, outside RLS, and terminates.
create or replace function public.crossflow_current_role()
returns text
language sql
security definer
set search_path = ''
stable
as $$
  select profile.role
  from public.crossflow_profiles as profile
  where profile.id = (select auth.uid())
$$;

create or replace function public.crossflow_is_admin()
returns boolean
language sql
security definer
set search_path = ''
stable
as $$
  select coalesce(public.crossflow_current_role() = 'admin', false)
$$;

-- Every account gets a profile automatically, always as 'driver'.
--
-- Without this, a signed-up user has a valid token and no row, and any code
-- that defaults a missing profile to a usable role would hand out access to
-- anyone who can reach the sign-up form. The backend treats a missing profile
-- as 403 for the same reason.
create or replace function public.crossflow_handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.crossflow_profiles (id, role, display_name)
  values (
    new.id,
    'driver',
    left(
      coalesce(
        nullif(trim(new.raw_user_meta_data ->> 'display_name'), ''),
        nullif(split_part(coalesce(new.email, ''), '@', 1), ''),
        'CrossFlow user'
      ),
      120
    )
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists crossflow_on_auth_user_created on auth.users;

create trigger crossflow_on_auth_user_created
after insert on auth.users
for each row execute function public.crossflow_handle_new_user();

-- Row-level security cannot protect a single column: an update policy that
-- permits a user to edit their own row permits them to edit every column of it,
-- role included. A row-level policy therefore cannot express "you may rename
-- yourself but not promote yourself". This trigger is where that rule actually
-- lives.
create or replace function public.crossflow_guard_profile_role()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  new.updated_at := now();
  if new.id is distinct from old.id then
    raise exception 'crossflow: profile id is immutable'
      using errcode = 'check_violation';
  end if;
  if new.role is distinct from old.role
     and current_user not in ('postgres', 'service_role', 'supabase_admin')
     and not public.crossflow_is_admin() then
    raise exception 'crossflow: role may only be changed by an administrator'
      using errcode = 'insufficient_privilege';
  end if;
  return new;
end;
$$;

drop trigger if exists crossflow_profiles_guard_role on public.crossflow_profiles;

create trigger crossflow_profiles_guard_role
before update on public.crossflow_profiles
for each row execute function public.crossflow_guard_profile_role();

alter table public.crossflow_profiles enable row level security;

-- Rerunnable policy definitions; create policy has no IF NOT EXISTS form.
drop policy if exists crossflow_profiles_select_own on public.crossflow_profiles;
drop policy if exists crossflow_profiles_select_admin on public.crossflow_profiles;
drop policy if exists crossflow_profiles_update_own on public.crossflow_profiles;
drop policy if exists crossflow_profiles_update_admin on public.crossflow_profiles;

-- auth.uid() is wrapped in a scalar subquery so the planner evaluates it once
-- per statement instead of once per row.
create policy crossflow_profiles_select_own
on public.crossflow_profiles
for select
to authenticated
using (id = (select auth.uid()));

create policy crossflow_profiles_select_admin
on public.crossflow_profiles
for select
to authenticated
using (public.crossflow_is_admin());

-- Self-service updates reach display_name only; the role guard trigger above
-- rejects anything else regardless of what this policy allows.
create policy crossflow_profiles_update_own
on public.crossflow_profiles
for update
to authenticated
using (id = (select auth.uid()))
with check (id = (select auth.uid()));

create policy crossflow_profiles_update_admin
on public.crossflow_profiles
for update
to authenticated
using (public.crossflow_is_admin())
with check (public.crossflow_is_admin());

-- No insert or delete policy is created for authenticated. Profiles appear
-- only through the auth.users trigger and disappear only with the account.

revoke all on table public.crossflow_profiles from anon;
revoke all on table public.crossflow_profiles from authenticated;
grant select, update on table public.crossflow_profiles to authenticated;

revoke all on function public.crossflow_current_role() from public, anon;
revoke all on function public.crossflow_is_admin() from public, anon;
revoke all on function public.crossflow_handle_new_user() from public, anon, authenticated;
revoke all on function public.crossflow_guard_profile_role() from public, anon, authenticated;
grant execute on function public.crossflow_current_role() to authenticated, service_role;
grant execute on function public.crossflow_is_admin() to authenticated, service_role;

-- Deployment drift detector. The committed SQL and the live project can diverge
-- silently; this reports the three facts that actually matter, so a wrong
-- answer is visible instead of merely undetected.
create or replace function public.crossflow_auth_health()
returns table(
  schema_version integer,
  rls_enabled boolean,
  authenticated_can_read boolean,
  authenticated_cannot_insert boolean
)
language sql
security invoker
set search_path = ''
stable
as $$
  select
    1::integer,
    (
      select relrowsecurity
      from pg_catalog.pg_class
      where oid = 'public.crossflow_profiles'::regclass
    ),
    pg_catalog.has_table_privilege(
      'authenticated', 'public.crossflow_profiles', 'SELECT'
    ),
    not pg_catalog.has_table_privilege(
      'authenticated', 'public.crossflow_profiles', 'INSERT'
    )
$$;

revoke all on function public.crossflow_auth_health() from public, anon, authenticated;
grant execute on function public.crossflow_auth_health() to service_role;

comment on table public.crossflow_profiles is
  'Server-resolved identity and role. Never trust a role sent by a client.';
comment on function public.crossflow_current_role() is
  'Security definer role lookup; prevents infinite recursion in profile policies.';
comment on function public.crossflow_guard_profile_role() is
  'Column-level protection for role, which row-level security cannot express.';

-- Delivery tables (deliveries, delivery_stops, journey_events) are appended
-- here by WP9. They are intentionally absent until the driver-isolation test
-- proves this boundary works.
