-- CrossFlow routing-intelligence durability (run once in Supabase SQL Editor).
-- Backend-only Data API surface: RLS is on, browser roles have no grants or
-- policies, and every function revokes PostgreSQL's default PUBLIC execute.

create table if not exists public.crossflow_spatial_traffic_observations (
  ingestion_revision bigint generated always as identity unique,
  observation_key text primary key check (observation_key ~ '^[0-9a-f]{64}$'),
  observed_at timestamptz not null,
  corridor_id text not null check (corridor_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
  latitude double precision not null check (latitude >= 0.88 and latitude < 1.215),
  longitude double precision not null check (longitude between 103.75 and 104.30),
  actual_speed_kph double precision not null check (actual_speed_kph > 0 and actual_speed_kph <= 250),
  free_flow_speed_kph double precision not null check (free_flow_speed_kph > 0 and free_flow_speed_kph <= 250),
  source text not null check (source in (
    'loop_sensor', 'probe_gps', 'tomtom_live',
    'verified_traffic_observation', 'reviewed_community_observation',
    'modelled', 'simulated', 'synthetic'
  )),
  provenance text not null,
  confidence double precision not null check (confidence between 0 and 1),
  observed boolean not null,
  reviewed boolean not null,
  road_class text not null check (road_class in (
    'motorway', 'trunk', 'primary', 'secondary', 'tertiary',
    'unclassified', 'residential', 'living_street', 'service', 'track',
    'road', 'ferry'
  )),
  capacity_vph double precision check (capacity_vph > 0 and capacity_vph <= 100000),
  terminal_distance_km double precision check (terminal_distance_km between 0 and 500),
  local_timezone_offset_minutes integer not null check (local_timezone_offset_minutes = 420),
  upstream_event_id text check (
    upstream_event_id is null
    or upstream_event_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
  ),
  immutable_payload jsonb not null check (jsonb_typeof(immutable_payload) = 'object'),
  ingested_at timestamptz not null default now(),
  check (actual_speed_kph / free_flow_speed_kph between 0.05 and 1.50),
  check (
    (source = 'loop_sensor' and provenance = 'verified_sensor' and confidence = 1.00 and observed)
    or (source = 'probe_gps' and provenance = 'verified_gps' and confidence = 0.95 and observed)
    or (source = 'tomtom_live' and provenance = 'verified_provider' and confidence = 0.90 and observed)
    or (source = 'verified_traffic_observation' and provenance = 'verified_observation' and confidence = 0.85 and observed)
    or (source = 'reviewed_community_observation' and provenance = 'reviewed_community' and confidence = 0.55 and observed and reviewed)
    or (source = 'modelled' and provenance = 'modelled' and confidence = 0.25 and not observed)
    or (source = 'simulated' and provenance = 'simulated' and confidence = 0.20 and not observed)
    or (source = 'synthetic' and provenance = 'synthetic' and confidence = 0.15 and not observed)
  )
);

create extension if not exists pgcrypto with schema extensions;

create index if not exists crossflow_spatial_corridor_time_idx
  on public.crossflow_spatial_traffic_observations (corridor_id, observed_at desc);
create index if not exists crossflow_spatial_ingestion_revision_idx
  on public.crossflow_spatial_traffic_observations (ingestion_revision);
create index if not exists crossflow_spatial_training_keyset_idx
  on public.crossflow_spatial_traffic_observations (
    observed_at desc, observation_key desc
  );

create table if not exists public.crossflow_shortcut_review_candidates (
  queue_revision bigint generated always as identity unique,
  override_id text primary key check (override_id ~ '^shortcut-[0-9a-f]{32}$'),
  graph_revision text not null check (graph_revision ~ '^[0-9a-f]{64}$'),
  candidate_payload jsonb not null check (jsonb_typeof(candidate_payload) = 'object'),
  review_state text not null default 'REVIEW_REQUIRED'
    check (review_state in ('REVIEW_REQUIRED', 'APPROVED_ARCHIVED')),
  activation_allowed boolean not null default false check (activation_allowed = false),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists crossflow_shortcut_review_graph_idx
  on public.crossflow_shortcut_review_candidates (graph_revision, updated_at desc);
create index if not exists crossflow_shortcut_review_revision_idx
  on public.crossflow_shortcut_review_candidates (queue_revision);
create index if not exists crossflow_shortcut_pending_revision_idx
  on public.crossflow_shortcut_review_candidates (queue_revision)
  where review_state = 'REVIEW_REQUIRED';

create table if not exists public.crossflow_approved_graph_overrides (
  graph_revision text not null check (graph_revision ~ '^[0-9a-f]{64}$'),
  override_id text not null check (override_id ~ '^shortcut-[0-9a-f]{32}$'),
  override_revision bigint not null check (override_revision > 0),
  approved_payload jsonb not null check (jsonb_typeof(approved_payload) = 'object'),
  candidate_payload jsonb not null check (jsonb_typeof(candidate_payload) = 'object'),
  candidate_sha256 text not null check (candidate_sha256 ~ '^[0-9a-f]{64}$'),
  approved_by text not null check (length(approved_by) between 1 and 128),
  approved_at timestamptz not null,
  primary key (graph_revision, override_id),
  unique (graph_revision, override_revision)
);

-- Upgrade an earlier CrossFlow setup safely. Identity columns cannot be added
-- portably with IF NOT EXISTS, so guarded DDL handles reruns without erasing
-- existing observations or review evidence.
do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_schema = 'public'
      and table_name = 'crossflow_spatial_traffic_observations'
      and column_name = 'ingestion_revision'
  ) then
    alter table public.crossflow_spatial_traffic_observations
      add column ingestion_revision bigint generated always as identity;
    alter table public.crossflow_spatial_traffic_observations
      add constraint crossflow_spatial_ingestion_revision_key
      unique (ingestion_revision);
  end if;
  if not exists (
    select 1 from information_schema.columns
    where table_schema = 'public'
      and table_name = 'crossflow_shortcut_review_candidates'
      and column_name = 'queue_revision'
  ) then
    alter table public.crossflow_shortcut_review_candidates
      add column queue_revision bigint generated always as identity;
    alter table public.crossflow_shortcut_review_candidates
      add constraint crossflow_shortcut_queue_revision_key
      unique (queue_revision);
  end if;
  if not exists (
    select 1 from information_schema.columns
    where table_schema = 'public'
      and table_name = 'crossflow_approved_graph_overrides'
      and column_name = 'candidate_payload'
  ) then
    alter table public.crossflow_approved_graph_overrides
      add column candidate_payload jsonb;
    update public.crossflow_approved_graph_overrides as approved
      set candidate_payload = candidate.candidate_payload
      from public.crossflow_shortcut_review_candidates as candidate
      where approved.graph_revision = candidate.graph_revision
        and approved.override_id = candidate.override_id;
    if exists (
      select 1 from public.crossflow_approved_graph_overrides
      where candidate_payload is null
    ) then
      raise exception 'cannot upgrade approvals without archived candidate payloads';
    end if;
    alter table public.crossflow_approved_graph_overrides
      alter column candidate_payload set not null;
    alter table public.crossflow_approved_graph_overrides
      add constraint crossflow_approved_candidate_payload_object
      check (jsonb_typeof(candidate_payload) = 'object');
  end if;
  -- Earlier versions allowed only REVIEW_REQUIRED. Replace that check before
  -- archived approvals are written by the new RPC.
  alter table public.crossflow_shortcut_review_candidates
    drop constraint if exists crossflow_shortcut_review_candidates_review_state_check;
  alter table public.crossflow_shortcut_review_candidates
    add constraint crossflow_shortcut_review_candidates_review_state_check
    check (review_state in ('REVIEW_REQUIRED', 'APPROVED_ARCHIVED'));
end;
$$;

create index if not exists crossflow_approved_graph_revision_idx
  on public.crossflow_approved_graph_overrides (graph_revision, override_revision desc);
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.crossflow_approved_graph_overrides'::regclass
      and contype = 'u'
      and pg_get_constraintdef(oid)
        = 'UNIQUE (graph_revision, override_revision)'
  ) then
    alter table public.crossflow_approved_graph_overrides
      add constraint crossflow_approved_graph_revision_override_revision_key
      unique (graph_revision, override_revision);
  end if;
end;
$$;

alter table public.crossflow_spatial_traffic_observations enable row level security;
alter table public.crossflow_shortcut_review_candidates enable row level security;
alter table public.crossflow_approved_graph_overrides enable row level security;

revoke all on table public.crossflow_spatial_traffic_observations from public, anon, authenticated;
revoke all on table public.crossflow_shortcut_review_candidates from public, anon, authenticated;
revoke all on table public.crossflow_approved_graph_overrides from public, anon, authenticated;
-- Supabase may apply broad default privileges to service_role. GRANT is
-- additive, so clear them explicitly before installing the exact read-only
-- Data API surface. All mutations below go through reviewed RPCs.
revoke all on table public.crossflow_spatial_traffic_observations from service_role;
revoke all on table public.crossflow_shortcut_review_candidates from service_role;
revoke all on table public.crossflow_approved_graph_overrides from service_role;
revoke all on sequence public.crossflow_spatial_traffic_observations_ingestion_revision_seq
  from public, anon, authenticated;
revoke all on sequence public.crossflow_shortcut_review_candidates_queue_revision_seq
  from public, anon, authenticated;
revoke all on sequence public.crossflow_spatial_traffic_observations_ingestion_revision_seq
  from service_role;
revoke all on sequence public.crossflow_shortcut_review_candidates_queue_revision_seq
  from service_role;

grant usage on schema public to service_role;
grant select on table public.crossflow_spatial_traffic_observations to service_role;
grant select on table public.crossflow_shortcut_review_candidates to service_role;
-- Approvals are append-only and RPC-only: service_role has no direct write.
grant select on table public.crossflow_approved_graph_overrides to service_role;

create or replace function public.crossflow_ingest_spatial_observations(batch jsonb)
returns table(received integer, unique_count integer, inserted integer, duplicates integer)
language plpgsql
security definer
set search_path = ''
as $$
declare
  item jsonb;
  existing jsonb;
  affected integer;
  received_count integer;
  unique_item_count integer;
  inserted_count integer := 0;
begin
  if jsonb_typeof(batch) <> 'array' or jsonb_array_length(batch) < 1
     or jsonb_array_length(batch) > 2000
     or pg_column_size(batch) > 1966080 then
    raise exception 'invalid spatial observation batch';
  end if;
  received_count := jsonb_array_length(batch);

  if exists (
    select 1
    from jsonb_array_elements(batch) as values_by_key(value)
    group by value->>'observation_key'
    having value->>'observation_key' is null or count(distinct value) > 1
  ) then
    raise exception 'spatial observation idempotency conflict inside batch';
  end if;
  select count(distinct value->>'observation_key')
    into unique_item_count from jsonb_array_elements(batch);

  for item in
    select distinct value from jsonb_array_elements(batch) as values_by_key(value)
  loop
    if (item->>'observed_at')::timestamptz < now() - interval '5 years'
       or (item->>'observed_at')::timestamptz > now() + interval '5 minutes' then
      raise exception 'spatial observation outside retention/future boundary';
    end if;
    insert into public.crossflow_spatial_traffic_observations (
      observation_key, observed_at, corridor_id, latitude, longitude,
      actual_speed_kph, free_flow_speed_kph, source, provenance, confidence,
      observed, reviewed, road_class, capacity_vph, terminal_distance_km,
      local_timezone_offset_minutes, upstream_event_id, immutable_payload
    ) values (
      item->>'observation_key', (item->>'observed_at')::timestamptz,
      item->>'corridor_id', (item->>'latitude')::double precision,
      (item->>'longitude')::double precision,
      (item->>'actual_speed_kph')::double precision,
      (item->>'free_flow_speed_kph')::double precision, item->>'source',
      item->>'provenance', (item->>'confidence')::double precision,
      (item->>'observed')::boolean, (item->>'reviewed')::boolean,
      item->>'road_class', (item->>'capacity_vph')::double precision,
      (item->>'terminal_distance_km')::double precision,
      (item->>'local_timezone_offset_minutes')::integer,
      item->>'upstream_event_id', item
    ) on conflict (observation_key) do nothing;
    get diagnostics affected = row_count;
    if affected = 0 then
      select stored.immutable_payload into existing
      from public.crossflow_spatial_traffic_observations as stored
      where stored.observation_key = item->>'observation_key'
      for share;
      if existing is distinct from item then
        raise exception 'spatial observation idempotency conflict';
      end if;
    else
      inserted_count := inserted_count + 1;
    end if;
  end loop;
  return query select received_count, unique_item_count, inserted_count,
    received_count - inserted_count;
end;
$$;

drop function if exists public.crossflow_upsert_shortcut_candidates(jsonb);
create or replace function public.crossflow_upsert_shortcut_candidates(batch jsonb)
returns table(
  received integer,
  inserted_count integer,
  existing_count integer,
  first_queue_revision bigint,
  last_queue_revision bigint,
  results jsonb
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  item jsonb;
  existing jsonb;
  merged jsonb;
  merged_provenance jsonb;
  affected integer;
  received_total integer;
  inserted_total integer := 0;
  first_revision bigint;
  last_revision bigint;
  stored_revision bigint;
  stored_state text;
  result_items jsonb := '[]'::jsonb;
begin
  if jsonb_typeof(batch) <> 'array' or jsonb_array_length(batch) < 1
     or jsonb_array_length(batch) > 2000
     or pg_column_size(batch) > 1966080 then
    raise exception 'invalid shortcut candidate batch';
  end if;
  received_total := jsonb_array_length(batch);
  if exists (
    select 1
    from jsonb_array_elements(batch) as duplicate_ids(value)
    group by value->>'override_id'
    having value->>'override_id' is null or count(*) > 1
  ) then
    raise exception 'shortcut candidate batch identities must be unique';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('crossflow-review-queue', 0));
  if (
    (select count(*) from public.crossflow_shortcut_review_candidates
      where review_state = 'REVIEW_REQUIRED')
    + (
      select count(distinct item->>'override_id')
      from jsonb_array_elements(batch) as pending(item)
      where not exists (
        select 1
        from public.crossflow_shortcut_review_candidates as stored
        where stored.override_id = item->>'override_id'
      )
    )
  ) > 2000 then
    raise exception 'shortcut review queue capacity exceeded';
  end if;
  for item in
    select value from jsonb_array_elements(batch) with ordinality
    order by ordinality
  loop
    stored_revision := null;
    stored_state := null;
    if item->>'review_state' <> 'REVIEW_REQUIRED'
       or coalesce((item->>'activation_allowed')::boolean, true)
       or item->>'override_id' !~ '^shortcut-[0-9a-f]{32}$'
       or item->>'graph_revision' !~ '^[0-9a-f]{64}$' then
      raise exception 'candidate is not a valid inactive review record';
    end if;
    if jsonb_typeof(item->'provenance') <> 'array'
       or jsonb_array_length(item->'provenance') not between 1 and 32
       or pg_column_size(item) > 65536 then
      raise exception 'candidate provenance/resource bound exceeded';
    end if;
    insert into public.crossflow_shortcut_review_candidates (
      override_id, graph_revision, candidate_payload,
      review_state, activation_allowed
    ) values (
      item->>'override_id', item->>'graph_revision', item,
      'REVIEW_REQUIRED', false
    ) on conflict (override_id) do nothing
      returning queue_revision, review_state
      into stored_revision, stored_state;
    get diagnostics affected = row_count;
    if affected = 1 then
      merged := item;
      inserted_total := inserted_total + 1;
    else
      select stored.candidate_payload, stored.queue_revision, stored.review_state
        into existing, stored_revision, stored_state
      from public.crossflow_shortcut_review_candidates as stored
      where stored.override_id = item->>'override_id'
      for update;
      if not found then
        raise exception 'shortcut candidate row disappeared';
      end if;
      if exists (
        select 1 from public.crossflow_approved_graph_overrides as approved_row
        where approved_row.graph_revision = item->>'graph_revision'
          and approved_row.override_id = item->>'override_id'
      ) then
        -- A recurring scrape may contribute newer mutable evidence for an
        -- already-approved geometric identity. Keep the frozen approval and
        -- archived candidate unchanged, but do not roll back unrelated new
        -- candidates in the same atomic batch. Immutable identity drift is
        -- still rejected.
        if (existing - 'provenance' - 'claimed_distance_m'
            - 'claimed_duration_s' - 'road_quality'
            - 'road_quality_is_default' - 'confidence')
           is distinct from
           (item - 'provenance' - 'claimed_distance_m'
            - 'claimed_duration_s' - 'road_quality'
            - 'road_quality_is_default' - 'confidence') then
          raise exception 'approved shortcut candidate identity conflict';
        end if;
        merged := existing;
        result_items := result_items || jsonb_build_array(jsonb_build_object(
          'override_id', item->>'override_id',
          'inserted', false,
          'queue_revision', stored_revision,
          'review_state', stored_state,
          'candidate_sha256', pg_catalog.encode(
            extensions.digest(merged::text, 'sha256'), 'hex'
          )
        ));
        continue;
      end if;
      if (existing - 'provenance' - 'claimed_distance_m'
          - 'claimed_duration_s' - 'road_quality'
          - 'road_quality_is_default' - 'confidence')
         is distinct from
         (item - 'provenance' - 'claimed_distance_m'
          - 'claimed_duration_s' - 'road_quality'
          - 'road_quality_is_default' - 'confidence') then
        raise exception 'shortcut candidate identity conflict';
      end if;
      select coalesce(jsonb_agg(value order by
          value->>'source_id', value->>'source_url', value->>'document_id',
          value->>'retrieved_at', value->>'content_sha256',
          value->>'source_tip_id', value->>'parser',
          value->>'excerpt_sha256', value->>'excerpt'
        ), '[]'::jsonb)
        into merged_provenance
      from (
        select distinct value
        from jsonb_array_elements(
          coalesce(existing->'provenance', '[]'::jsonb)
          || coalesce(item->'provenance', '[]'::jsonb)
        ) as all_provenance(value)
      ) as unique_provenance;
      if jsonb_array_length(merged_provenance) > 32 then
        raise exception 'shortcut candidate provenance capacity exceeded';
      end if;
      merged := existing || jsonb_build_object(
        'claimed_distance_m', case
          when existing->'claimed_distance_m' = 'null'::jsonb
               and item->'claimed_distance_m' = 'null'::jsonb then null
          else greatest(
            coalesce((existing->>'claimed_distance_m')::double precision, 0),
            coalesce((item->>'claimed_distance_m')::double precision, 0)
          ) end,
        'claimed_duration_s', case
          when existing->'claimed_duration_s' = 'null'::jsonb
               and item->'claimed_duration_s' = 'null'::jsonb then null
          else greatest(
            coalesce((existing->>'claimed_duration_s')::double precision, 0),
            coalesce((item->>'claimed_duration_s')::double precision, 0)
          ) end,
        'road_quality', least(
          (existing->>'road_quality')::double precision,
          (item->>'road_quality')::double precision
        ),
        'road_quality_is_default',
          (existing->>'road_quality_is_default')::boolean
          or (item->>'road_quality_is_default')::boolean,
        'confidence', greatest(
          (existing->>'confidence')::double precision,
          (item->>'confidence')::double precision
        ),
        'provenance', merged_provenance
      );
      if pg_column_size(merged) > 65536 then
        raise exception 'shortcut candidate resource bound exceeded';
      end if;
      update public.crossflow_shortcut_review_candidates as stored
      set candidate_payload = merged, updated_at = now()
      where stored.override_id = item->>'override_id';
    end if;
    result_items := result_items || jsonb_build_array(jsonb_build_object(
      'override_id', item->>'override_id',
      'inserted', affected = 1,
      'queue_revision', stored_revision,
      'review_state', stored_state,
      'candidate_sha256', pg_catalog.encode(
        extensions.digest(merged::text, 'sha256'), 'hex'
      )
    ));
  end loop;
  select min(stored.queue_revision), max(stored.queue_revision)
    into first_revision, last_revision
  from public.crossflow_shortcut_review_candidates as stored
  where stored.override_id in (
    select value->>'override_id' from jsonb_array_elements(batch)
  );
  return query select received_total, inserted_total,
    received_total - inserted_total,
    first_revision, last_revision, result_items;
end;
$$;

create or replace function public.crossflow_approve_shortcut_candidate(
  candidate_id text, expected_candidate jsonb, approved jsonb
)
returns table(
  graph_revision text, override_id text, override_revision bigint,
  approved_payload jsonb, candidate_payload jsonb, candidate_sha256 text,
  approved_by text, approved_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  stored_candidate jsonb;
  active_graph text;
  next_revision bigint;
  existing public.crossflow_approved_graph_overrides%rowtype;
begin
  if pg_column_size(expected_candidate) > 65536
     or pg_column_size(approved) > 16384 then
    raise exception 'shortcut approval resource bound exceeded';
  end if;
  active_graph := approved->>'graph_revision';
  perform pg_advisory_xact_lock(hashtextextended(active_graph, 0));
  select stored.* into existing
  from public.crossflow_approved_graph_overrides as stored
  where stored.graph_revision = active_graph and stored.override_id = candidate_id;
  if found then
    if existing.approved_by <> trim(approved->>'approved_by')
       or existing.candidate_payload is distinct from expected_candidate
       or existing.candidate_sha256 <> approved->>'candidate_sha256' then
      raise exception 'existing approval does not match retry';
    end if;
    return query select existing.graph_revision, existing.override_id,
      existing.override_revision, existing.approved_payload,
      existing.candidate_payload,
      existing.candidate_sha256, existing.approved_by, existing.approved_at;
    return;
  end if;
  select stored.candidate_payload into stored_candidate
  from public.crossflow_shortcut_review_candidates as stored
  where stored.override_id = candidate_id
    and stored.review_state = 'REVIEW_REQUIRED'
    and not stored.activation_allowed
  for update;
  if not found or stored_candidate is distinct from expected_candidate then
    raise exception 'candidate changed or is not reviewable';
  end if;
  if approved->>'override_id' <> candidate_id
     or active_graph <> stored_candidate->>'graph_revision'
     or approved->'source_node' is distinct from stored_candidate->'source_node'
     or approved->'target_node' is distinct from stored_candidate->'target_node'
     or approved->'geometry' is distinct from stored_candidate->'geometry'
     or approved->'applicable_vehicle_modes'
        is distinct from stored_candidate->'applicable_vehicle_modes'
     or approved->'road_quality' is distinct from stored_candidate->'road_quality'
     or approved->'duration_s' is distinct from stored_candidate->'claimed_duration_s'
     or approved->>'candidate_sha256' !~ '^[0-9a-f]{64}$'
     or length(trim(approved->>'approved_by')) not between 1 and 128 then
    raise exception 'approval does not derive from the stored candidate';
  end if;
  if (approved->>'distance_m')::double precision is distinct from coalesce(
       (stored_candidate->>'claimed_distance_m')::double precision,
       (stored_candidate->>'geometry_distance_m')::double precision
     ) then
    raise exception 'approved distance does not derive from candidate';
  end if;

  select coalesce(max(stored.override_revision), 0) + 1 into next_revision
  from public.crossflow_approved_graph_overrides as stored
  where stored.graph_revision = active_graph;
  if next_revision > 10000 then
    raise exception 'approved graph override capacity exceeded';
  end if;
  insert into public.crossflow_approved_graph_overrides (
    graph_revision, override_id, override_revision, approved_payload,
    candidate_payload,
    candidate_sha256, approved_by, approved_at
  ) values (
    active_graph, candidate_id, next_revision, approved, stored_candidate,
    approved->>'candidate_sha256', trim(approved->>'approved_by'),
    (approved->>'approved_at')::timestamptz
  );
  update public.crossflow_shortcut_review_candidates as stored
  set review_state = 'APPROVED_ARCHIVED', updated_at = now()
  where stored.override_id = candidate_id;
  return query select active_graph, candidate_id, next_revision, approved,
    stored_candidate,
    approved->>'candidate_sha256', trim(approved->>'approved_by'),
    (approved->>'approved_at')::timestamptz;
end;
$$;

create or replace function public.crossflow_spatial_training_snapshot()
returns table(snapshot_revision bigint, cutoff_observed_at timestamptz)
language sql
security invoker
set search_path = ''
stable
as $$
  select
    coalesce(max(stored.ingestion_revision), 0)::bigint,
    pg_catalog.now() - interval '5 years'
  from public.crossflow_spatial_traffic_observations as stored
$$;

drop function if exists public.crossflow_read_spatial_training_page(
  bigint, bigint, integer
);
create or replace function public.crossflow_read_spatial_training_page(
  p_snapshot_revision bigint,
  p_cutoff_observed_at timestamptz,
  p_before_observed_at timestamptz,
  p_before_observation_key text,
  p_page_limit integer
)
returns table(
  ingestion_revision bigint,
  observed_at timestamptz,
  observation_key text,
  immutable_payload jsonb
)
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if p_snapshot_revision is null or p_snapshot_revision < 0
     or p_page_limit is null or p_page_limit not between 1 and 2000 then
    raise exception 'invalid spatial training page request';
  end if;
  if p_cutoff_observed_at is null
     or (p_before_observed_at is null) <> (p_before_observation_key is null)
     or (
       p_before_observation_key is not null
       and p_before_observation_key !~ '^[0-9a-f]{64}$'
     )
     or p_cutoff_observed_at < pg_catalog.now() - interval '5 years 1 day'
     or p_cutoff_observed_at > pg_catalog.now() - interval '5 years' then
    raise exception 'invalid spatial training snapshot cursor';
  end if;
  return query
    select stored.ingestion_revision, stored.observed_at,
      stored.observation_key, stored.immutable_payload
    from public.crossflow_spatial_traffic_observations as stored
    where stored.ingestion_revision <= p_snapshot_revision
      and stored.observed_at >= p_cutoff_observed_at
      and (
        p_before_observed_at is null
        or (stored.observed_at, stored.observation_key)
           < (p_before_observed_at, p_before_observation_key)
      )
    order by stored.observed_at desc, stored.observation_key desc
    limit p_page_limit;
end;
$$;

create or replace function public.crossflow_routing_intelligence_health()
returns table(schema_version integer)
language sql
security invoker
set search_path = ''
stable
as $$
  select 1::integer
  where pg_catalog.has_table_privilege(
    'service_role',
    'public.crossflow_spatial_traffic_observations',
    'SELECT'
  )
  and pg_catalog.has_table_privilege(
    'service_role',
    'public.crossflow_shortcut_review_candidates',
    'SELECT'
  )
  and pg_catalog.has_table_privilege(
    'service_role',
    'public.crossflow_approved_graph_overrides',
    'SELECT'
  )
  and not pg_catalog.has_table_privilege(
    'service_role',
    'public.crossflow_approved_graph_overrides',
    'INSERT'
  )
  and not pg_catalog.has_table_privilege(
    'service_role',
    'public.crossflow_approved_graph_overrides',
    'UPDATE'
  )
  and not pg_catalog.has_table_privilege(
    'service_role',
    'public.crossflow_approved_graph_overrides',
    'DELETE'
  )
$$;

revoke all on function public.crossflow_ingest_spatial_observations(jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.crossflow_upsert_shortcut_candidates(jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.crossflow_approve_shortcut_candidate(text, jsonb, jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.crossflow_spatial_training_snapshot()
  from public, anon, authenticated, service_role;
revoke all on function public.crossflow_read_spatial_training_page(
  bigint, timestamptz, timestamptz, text, integer
)
  from public, anon, authenticated, service_role;
revoke all on function public.crossflow_routing_intelligence_health()
  from public, anon, authenticated, service_role;
grant execute on function public.crossflow_ingest_spatial_observations(jsonb)
  to service_role;
grant execute on function public.crossflow_upsert_shortcut_candidates(jsonb)
  to service_role;
grant execute on function public.crossflow_approve_shortcut_candidate(text, jsonb, jsonb)
  to service_role;
grant execute on function public.crossflow_spatial_training_snapshot()
  to service_role;
grant execute on function public.crossflow_read_spatial_training_page(
  bigint, timestamptz, timestamptz, text, integer
)
  to service_role;
grant execute on function public.crossflow_routing_intelligence_health()
  to service_role;

comment on table public.crossflow_shortcut_review_candidates is
  'Inactive crowd-sourced candidates; ingestion cannot activate them.';
comment on table public.crossflow_approved_graph_overrides is
  'Append-only human approvals derived atomically from exact persisted candidates.';
