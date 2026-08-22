-- ============================================================
-- Video Factory — multi-user MVP migration
--
-- Adds on top of 01-schema.sql, does not replace it:
--   - owner_id on projects/characters/locations (auth.users)
--   - a credits + credit_ledger pair for prepaid balance
--   - RLS policies scoping every table to its owner
--
-- Safe to re-run (idempotent). Run in the Supabase SQL editor,
-- or via mcp__Supabase__apply_migration.
-- ============================================================

-- ---------- ownership ----------
-- Nullable on purpose: existing single-user pilot rows (the
-- "25 let" film, the robot-fight project) predate auth and have
-- no owner. New rows from the web app always set this. A NOT NULL
-- constraint can be added once those pilot rows are either
-- assigned an owner or archived.
alter table public.projects   add column if not exists owner_id uuid references auth.users(id) on delete cascade;
alter table public.characters add column if not exists owner_id uuid references auth.users(id) on delete cascade;
alter table public.locations  add column if not exists owner_id uuid references auth.users(id) on delete cascade;

create index if not exists projects_owner_idx   on public.projects   (owner_id);
create index if not exists characters_owner_idx on public.characters (owner_id);
create index if not exists locations_owner_idx  on public.locations  (owner_id);

-- templates stays global by design: prompt skeletons and motion
-- text carry no personal photos or identity data, and the whole
-- point of the table (02-runbook.md) is that a solved shot type
-- compounds value for the *next* video -- sharing that library
-- across users is the intended behaviour, not an oversight.

-- ---------- credits ----------
-- One row per user. Balance only ever changes through the service
-- role (server-side, after a verified top-up or a verified fal
-- generation) -- never directly from a client request.
create table if not exists public.credits (
  user_id     uuid primary key references auth.users(id) on delete cascade,
  balance_usd numeric(10,4) not null default 0 check (balance_usd >= 0),
  updated_at  timestamptz   not null default now()
);

-- Immutable audit trail: every top-up and every spend. balance_usd
-- above is a running cache of sum(delta_usd) for fast reads; this
-- table is the source of truth if the two ever disagree.
create table if not exists public.credit_ledger (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references auth.users(id) on delete cascade,
  delta_usd     numeric(10,4) not null,
  reason        text not null check (reason in ('manual_topup', 'generation', 'refund')),
  generation_id uuid references public.generations(id) on delete set null,
  note          text,
  created_at    timestamptz not null default now()
);

create index if not exists credit_ledger_user_idx on public.credit_ledger (user_id);

alter table public.credits       enable row level security;
alter table public.credit_ledger enable row level security;

-- Users can see their own balance and history, never anyone else's,
-- and never write to either table directly -- the web app's
-- service-role backend is the only writer (see webapp/server.py).
drop policy if exists "own credit balance" on public.credits;
create policy "own credit balance" on public.credits
  for select using (user_id = auth.uid());

drop policy if exists "own credit ledger" on public.credit_ledger;
create policy "own credit ledger" on public.credit_ledger
  for select using (user_id = auth.uid());

-- ---------- RLS: scope everything to its owner ----------
-- 01-schema.sql enabled RLS with zero policies (service role only).
-- These add real per-user policies now that rows carry an owner,
-- without removing the service-role bypass the pipeline still uses.

drop policy if exists "own projects" on public.projects;
create policy "own projects" on public.projects
  for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

drop policy if exists "own characters" on public.characters;
create policy "own characters" on public.characters
  for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

drop policy if exists "own locations" on public.locations;
create policy "own locations" on public.locations
  for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

drop policy if exists "own scenes" on public.scenes;
create policy "own scenes" on public.scenes
  for all using (
    project_id in (select id from public.projects where owner_id = auth.uid())
  );

drop policy if exists "own assets" on public.assets;
create policy "own assets" on public.assets
  for all using (
    project_id in (select id from public.projects where owner_id = auth.uid())
  );

drop policy if exists "own generations" on public.generations;
create policy "own generations" on public.generations
  for all using (
    project_id in (select id from public.projects where owner_id = auth.uid())
  );

-- templates: readable by every authenticated user (shared library),
-- writable by none directly -- only the service role harvests a
-- template after a shot lands in <=3 iterations (02-runbook.md §6).
drop policy if exists "read shared templates" on public.templates;
create policy "read shared templates" on public.templates
  for select using (auth.role() = 'authenticated');
