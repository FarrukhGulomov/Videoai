-- ============================================================
-- Video Factory — fix characters.name uniqueness for multi-user mode
--
-- 01-schema.sql's `characters.name` is `unique` globally -- correct for
-- the original single-tenant CLI pipeline (one person, one character
-- library), wrong now that 02-multiuser-schema.sql gave characters an
-- owner_id: two different users both naming a character "Alice" would
-- collide on the second signup, not just the second use.
--
-- Run this after 02-multiuser-schema.sql. Safe to re-run (idempotent).
-- ============================================================

alter table public.characters drop constraint if exists characters_name_key;

-- Global rows with a null owner_id (the pre-auth CLI pilot data --
-- see 02-multiuser-schema.sql's comment on why owner_id is nullable)
-- still can't collide with each other under this index, since two nulls
-- never compare equal in a unique index -- Postgres treats NULL as
-- distinct from every other NULL for uniqueness purposes.
create unique index if not exists characters_owner_name_key
  on public.characters (owner_id, name);
