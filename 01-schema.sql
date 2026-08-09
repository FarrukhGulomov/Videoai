-- ============================================================
-- Video Factory — Supabase schema
-- Rebuilt from 02-runbook.md (the original 01-schema.sql was
-- not present in the project folder).
--
-- Creates: projects, characters, locations, templates, scenes,
--          assets, generations
-- Plus views: project_economics, scene_economics
-- Safe to re-run (idempotent).
-- ============================================================

-- ---------- 1. projects ----------
create table if not exists public.projects (
  id            uuid primary key default gen_random_uuid(),
  name          text        not null,
  brief         text,
  status        text        not null default 'active'
                check (status in ('active', 'paused', 'done', 'abandoned')),
  budget_usd    numeric(10,2) default 25.00,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- ---------- 2. characters (identity locks) ----------
create table if not exists public.characters (
  id            uuid primary key default gen_random_uuid(),
  name          text        not null unique,
  lock_text     text        not null,
  reference_urls text[]     not null default '{}',
  notes         text,
  created_at    timestamptz not null default now()
);

-- ---------- 3. locations (place locks) ----------
create table if not exists public.locations (
  id            uuid primary key default gen_random_uuid(),
  name          text        not null unique,
  lock_text     text        not null,
  notes         text,
  created_at    timestamptz not null default now()
);

-- ---------- 4. templates (the compounding asset) ----------
create table if not exists public.templates (
  id              uuid primary key default gen_random_uuid(),
  name            text        not null,
  category        text,
  prompt_skeleton text        not null,
  motion          text,
  times_used      integer     not null default 0,
  created_at      timestamptz not null default now()
);

create unique index if not exists templates_name_key on public.templates (name);

-- ---------- 5. scenes ----------
create table if not exists public.scenes (
  id               uuid primary key default gen_random_uuid(),
  project_id       uuid not null references public.projects (id) on delete cascade,
  scene_number     integer not null,
  title            text    not null,
  description      text,
  character_id     uuid references public.characters (id) on delete set null,
  location_id      uuid references public.locations  (id) on delete set null,
  template_id      uuid references public.templates  (id) on delete set null,
  has_face         boolean not null default true,
  prompt           text,
  motion           text,
  duration_seconds integer not null default 5,
  -- draft -> still_approved -> approved (motion ok) -> final -> rejected
  status           text    not null default 'draft'
                   check (status in ('draft','still_approved','approved','final','rejected')),
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (project_id, scene_number)
);

create index if not exists scenes_project_idx on public.scenes (project_id);
create index if not exists scenes_status_idx  on public.scenes (status);

-- ---------- 6. assets (approved outputs) ----------
create table if not exists public.assets (
  id             uuid primary key default gen_random_uuid(),
  scene_id       uuid references public.scenes (id) on delete cascade,
  project_id     uuid references public.projects (id) on delete cascade,
  kind           text not null default 'still'
                 check (kind in ('still','clip','voice','final','reference')),
  url            text,
  local_path     text,
  rung           integer,
  approved       boolean not null default false,
  is_start_frame boolean not null default false,
  notes          text,
  created_at     timestamptz not null default now()
);

create index if not exists assets_scene_idx on public.assets (scene_id);
create index if not exists assets_start_frame_idx
  on public.assets (scene_id) where is_start_frame;

-- ---------- 7. generations (every attempt, including failures) ----------
create table if not exists public.generations (
  id               uuid primary key default gen_random_uuid(),
  scene_id         uuid references public.scenes (id) on delete cascade,
  project_id       uuid references public.projects (id) on delete cascade,
  rung             integer not null check (rung in (1, 2, 3)),
  model            text    not null,
  prompt           text,
  start_frame_url  text,
  status           text    not null default 'pending'
                   check (status in ('pending','success','failed','rejected')),
  cost_usd         numeric(10,4) not null default 0,
  duration_seconds numeric(6,2),
  output_url       text,
  error            text,
  created_at       timestamptz not null default now()
);

create index if not exists generations_scene_idx   on public.generations (scene_id);
create index if not exists generations_project_idx on public.generations (project_id);
create index if not exists generations_rung_idx    on public.generations (rung);

-- ============================================================
-- Economics views
-- ============================================================

create or replace view public.scene_economics as
select
  s.project_id,
  s.id            as scene_id,
  s.scene_number,
  s.title,
  s.status,
  count(g.id)                          as total_generations,
  count(g.id) filter (where g.status = 'failed') as failed_generations,
  coalesce(sum(g.cost_usd), 0)::numeric(10,4)    as total_cost_usd
from public.scenes s
left join public.generations g on g.scene_id = s.id
group by s.project_id, s.id, s.scene_number, s.title, s.status;

create or replace view public.project_economics as
select
  p.id            as project_id,
  p.name,
  p.budget_usd,
  count(distinct s.id)                        as scene_count,
  count(g.id)                                 as total_generations,
  coalesce(sum(g.cost_usd), 0)::numeric(10,4) as total_cost_usd,
  case when count(distinct s.id) = 0 then 0
       else (coalesce(sum(g.cost_usd), 0) / count(distinct s.id))::numeric(10,4)
  end                                         as cost_per_scene,
  case when coalesce(p.budget_usd, 0) = 0 then 0
       else round(100 * coalesce(sum(g.cost_usd), 0) / p.budget_usd, 1)
  end                                         as budget_used_pct
from public.projects p
left join public.scenes      s on s.project_id = p.id
left join public.generations g on g.project_id = p.id
group by p.id, p.name, p.budget_usd;

-- ============================================================
-- Row Level Security
-- Enabled with no public policies: the anon/authenticated API
-- keys can read nothing. All pipeline work goes through the
-- service role, which bypasses RLS.
-- ============================================================

alter table public.projects    enable row level security;
alter table public.characters  enable row level security;
alter table public.locations   enable row level security;
alter table public.templates   enable row level security;
alter table public.scenes      enable row level security;
alter table public.assets      enable row level security;
alter table public.generations enable row level security;
