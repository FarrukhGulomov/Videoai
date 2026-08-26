-- ============================================================
-- Video Factory — self-hosted Postgres schema (no Supabase)
--
-- Run 01-schema.sql first (repo root) -- it creates projects,
-- characters, locations, templates, scenes, assets, generations
-- plus the economics views, and has no Supabase-specific
-- dependency (no reference to auth.users anywhere in it), so it
-- is safe to use as-is on a plain self-hosted Postgres server.
--
-- Then run this file. It is the self-hosted equivalent of
-- 02-multiuser-schema.sql, with one structural difference:
-- 02-multiuser-schema.sql references Supabase's auth.users table
-- (created by Supabase's GoTrue auth service, which does not
-- exist on a plain Postgres install). This file creates a local
-- `users` table instead and points owner_id / credits /
-- credit_ledger at that, so the exact same downstream tables
-- (projects, credits, ...) work without Supabase running at all.
--
-- Run ONE of {02-multiuser-schema.sql, 03-selfhosted-schema.sql}
-- depending on which backend webapp/server.py is configured for
-- (SUPABASE_URL set -> 02; DATABASE_URL set -> this file) -- never
-- both against the same database.
--
-- Safe to re-run (idempotent).
-- ============================================================

create extension if not exists pgcrypto;  -- gen_random_uuid(); harmless no-op if already core (PG13+)

-- ---------- users ----------
-- Replaces Supabase's auth.users for self-hosted mode. Password
-- hashing and session issuance live in webapp/pg_client.py, not
-- here -- this table only stores what that module writes.
create table if not exists public.users (
  id              uuid primary key default gen_random_uuid(),
  email           text        not null unique,
  password_hash   text        not null,  -- "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>"
  created_at      timestamptz not null default now()
);

-- ---------- sessions ----------
-- One row per signed-in session. token_hash is sha256(bearer
-- token) -- the raw token is never stored, matching how a
-- password itself is never stored in plain text; a stolen copy of
-- this table cannot be replayed as a session on its own. Expired
-- rows are opportunistically deleted on the next login
-- (webapp/pg_client.py) rather than needing a cron job.
create table if not exists public.sessions (
  token_hash  text        primary key,
  user_id     uuid        not null references public.users(id) on delete cascade,
  expires_at  timestamptz not null,
  created_at  timestamptz not null default now()
);

create index if not exists sessions_user_idx    on public.sessions (user_id);
create index if not exists sessions_expires_idx on public.sessions (expires_at);

-- ---------- ownership ----------
-- Same nullable-on-purpose reasoning as 02-multiuser-schema.sql:
-- existing rows created before auth existed have no owner.
alter table public.projects   add column if not exists owner_id uuid references public.users(id) on delete cascade;
alter table public.characters add column if not exists owner_id uuid references public.users(id) on delete cascade;
alter table public.locations  add column if not exists owner_id uuid references public.users(id) on delete cascade;

create index if not exists projects_owner_idx   on public.projects   (owner_id);
create index if not exists characters_owner_idx on public.characters (owner_id);
create index if not exists locations_owner_idx  on public.locations  (owner_id);

-- ---------- credits ----------
create table if not exists public.credits (
  user_id     uuid primary key references public.users(id) on delete cascade,
  balance_usd numeric(10,4) not null default 0 check (balance_usd >= 0),
  updated_at  timestamptz   not null default now()
);

create table if not exists public.credit_ledger (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.users(id) on delete cascade,
  delta_usd     numeric(10,4) not null,
  reason        text not null check (reason in ('manual_topup', 'generation', 'refund')),
  generation_id uuid references public.generations(id) on delete set null,
  note          text,
  created_at    timestamptz not null default now()
);

create index if not exists credit_ledger_user_idx on public.credit_ledger (user_id);

-- ---------- no RLS here, by design ----------
-- 02-multiuser-schema.sql's RLS policies exist because Supabase's
-- PostgREST layer lets the browser talk to Postgres directly with
-- a user-scoped JWT (auth.uid()), so the database itself has to
-- enforce ownership. Self-hosted mode has no PostgREST: the
-- browser only ever talks to webapp/server.py, which is the sole
-- holder of the DATABASE_URL credential, and every query
-- webapp/pg_client.py issues is already filtered by the caller's
-- authenticated owner_id in Python before it reaches Postgres --
-- see _owner_id() in webapp/server.py and the WHERE clauses in
-- pg_client.py. Enabling RLS with no policies here would only add
-- overhead with nothing to enforce, since the single connecting
-- role always needs full access to do that filtering itself.
