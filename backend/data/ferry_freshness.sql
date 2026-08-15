-- Optional durable freshness state for Vercel/multi-instance deployments.
-- Run once in the Supabase SQL editor used by the backend server key.
create table if not exists public.crossflow_ferry_freshness (
  snapshot_id text primary key,
  latest_checked_at timestamptz not null,
  last_verified_at timestamptz not null,
  updated_at timestamptz not null default now()
);

alter table public.crossflow_ferry_freshness
drop constraint if exists crossflow_ferry_freshness_time_order;

alter table public.crossflow_ferry_freshness
add constraint crossflow_ferry_freshness_time_order
check (latest_checked_at >= last_verified_at);

create or replace function public.crossflow_keep_ferry_freshness_monotonic()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if tg_op = 'UPDATE' then
    new.latest_checked_at := greatest(old.latest_checked_at, new.latest_checked_at);
    new.last_verified_at := greatest(old.last_verified_at, new.last_verified_at);
  end if;
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists crossflow_ferry_freshness_monotonic
on public.crossflow_ferry_freshness;

create trigger crossflow_ferry_freshness_monotonic
before insert or update on public.crossflow_ferry_freshness
for each row execute function public.crossflow_keep_ferry_freshness_monotonic();

alter table public.crossflow_ferry_freshness enable row level security;

-- No anon/authenticated policy is intentionally created. The backend accesses
-- this table only with SUPABASE_SECRET_KEY (or the legacy service-role key),
-- both of which bypass RLS.
