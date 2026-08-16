-- Durable, shared storage for route-solver runs.
-- Run once in the Supabase SQL editor used by the backend server key.
--
-- Without this table the API persists routes only in SQLite, which on Vercel
-- lives in per-invocation /tmp. A driver code issued by one worker is then
-- invisible to the next, so assigned-route retrieval fails even with a valid
-- token. This table makes a run readable from every instance and gives the
-- history view a single shared source.
create table if not exists public.crossflow_route_runs (
  route_id text primary key,
  route_code text not null unique,
  route_kind text not null,
  request jsonb not null,
  response jsonb not null,
  -- Denormalized for the history list so it never has to parse every envelope.
  origin_name text,
  destination_name text,
  vehicle_type text,
  -- The signed-in account that produced the run, when there was one. Guest
  -- driver retrieval carries no identity, so this stays null rather than
  -- inventing an attribution.
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists crossflow_route_runs_created_at_idx
on public.crossflow_route_runs (created_at desc);

create index if not exists crossflow_route_runs_created_by_idx
on public.crossflow_route_runs (created_by, created_at desc);

create or replace function public.crossflow_touch_route_run()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  -- A route_id is content-addressed, so a repeat run is the same route. Keep
  -- the original created_at and let updated_at record that it was replanned.
  if tg_op = 'UPDATE' then
    new.created_at := old.created_at;
  end if;
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists crossflow_route_runs_touch
on public.crossflow_route_runs;

create trigger crossflow_route_runs_touch
before insert or update on public.crossflow_route_runs
for each row execute function public.crossflow_touch_route_run();

alter table public.crossflow_route_runs enable row level security;

-- No anon/authenticated policy is intentionally created. The backend accesses
-- this table only with SUPABASE_SECRET_KEY (or the legacy service-role key),
-- both of which bypass RLS. Route history is served through the API, which
-- applies its own authorization, never by the browser querying this directly.
