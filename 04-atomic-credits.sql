-- ============================================================
-- Video Factory — atomic credit reservations
--
-- Replaces the read-modify-write pattern in the old
-- webapp/supabase_client.py's record_spend() (two separate HTTP round
-- trips: GET balance, then POST a new value) with a reserve -> capture /
-- release pattern, each step a single Postgres function call so the
-- check-and-update happens inside one transaction with a row lock --
-- no window for two concurrent requests to both read the same balance
-- and both proceed.
--
-- Flow for a paid generation:
--   1. reserve_credit(user, amount, idempotency_key, note)
--      -- fails closed if balance - already-reserved < amount.
--      -- calling again with the same idempotency_key is a no-op: it
--         returns the existing reservation instead of creating a
--         second hold (duplicate-request protection).
--   2a. capture_credit_reservation(idempotency_key, note)
--       -- on a successful, validated generation: moves the hold from
--          "reserved" into an actual ledger spend.
--   2b. release_credit_reservation(idempotency_key)
--       -- on failure/cancellation/restart-interruption: drops the hold,
--          nothing was ever actually charged.
--   Both 2a/2b are idempotent -- calling either one twice (a retried
--   webhook, a restart resuming a job that already finished) is a no-op
--   the second time, returning the same result rather than erroring or
--   double-applying.
--
-- refund_credit(user, amount, idempotency_key, note) is separate: it
-- grants money back after a capture already happened (e.g. a later
-- dispute), not part of the reserve/capture/release cycle.
--
-- Safe to re-run (idempotent DDL). Run in the Supabase SQL editor, or
-- via mcp__Supabase__apply_migration, after 01/02/03.
-- ============================================================

create table if not exists public.credit_reservations (
  id             text primary key,  -- the idempotency key itself
  user_id        uuid not null references auth.users(id) on delete cascade,
  amount_usd     numeric(10,4) not null check (amount_usd > 0),
  status         text not null default 'reserved'
                   check (status in ('reserved', 'captured', 'released')),
  note           text,
  created_at     timestamptz not null default now(),
  resolved_at    timestamptz
);

create index if not exists credit_reservations_user_idx on public.credit_reservations (user_id);
create index if not exists credit_reservations_status_idx on public.credit_reservations (status);

alter table public.credit_reservations enable row level security;

drop policy if exists "own credit reservations" on public.credit_reservations;
create policy "own credit reservations" on public.credit_reservations
  for select using (user_id = auth.uid());

-- `credits.balance_usd` keeps meaning "spendable now" (unreserved), as
-- every existing read of it already assumes (webapp/server.py's balance
-- display, _reserve_funds's insufficient-funds check). A reservation
-- moves money out of balance_usd into reserved_usd immediately, not just
-- at capture time -- that's what actually prevents a second concurrent
-- request from also passing the "enough balance" check.
alter table public.credits add column if not exists reserved_usd numeric(10,4) not null default 0 check (reserved_usd >= 0);

-- credit_ledger already restricted `reason` to
-- ('manual_topup', 'generation', 'refund') in 02-multiuser-schema.sql;
-- widen it to record reservation lifecycle events too, for the same
-- immutable-audit-trail reason every other balance change goes through
-- this table.
alter table public.credit_ledger drop constraint if exists credit_ledger_reason_check;
alter table public.credit_ledger add constraint credit_ledger_reason_check
  check (reason in ('manual_topup', 'generation', 'refund', 'reserve', 'release'));

-- A dedicated dedupe column for refund_credit()/grant-style ledger rows
-- (reserve/capture/release already get their idempotency guard for free
-- from credit_reservations.id -- this is only for the grant path, which
-- has no backing reservation row). Nullable + a partial unique index so
-- older rows and non-idempotent-key inserts are unaffected.
alter table public.credit_ledger add column if not exists idempotency_key text;
create unique index if not exists credit_ledger_idempotency_key_idx
  on public.credit_ledger (idempotency_key) where idempotency_key is not null;

-- ---------- reserve_credit ----------
create or replace function public.reserve_credit(
  p_user_id uuid,
  p_amount_usd numeric,
  p_idempotency_key text,
  p_note text default null
) returns table (id text, status text, amount_usd numeric, balance_usd numeric, reserved_usd numeric)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_existing record;
  v_balance numeric;
  v_reserved numeric;
begin
  if p_amount_usd <= 0 then
    raise exception 'amount_usd must be positive';
  end if;

  -- Idempotency: a repeat call with the same key returns the existing
  -- reservation untouched, whatever its current status -- this is what
  -- makes a client-retried "generate" request safe to call twice.
  select cr.id, cr.status, cr.amount_usd into v_existing
    from public.credit_reservations cr where cr.id = p_idempotency_key;
  if found then
    select c.balance_usd, c.reserved_usd into v_balance, v_reserved
      from public.credits c where c.user_id = p_user_id;
    return query select v_existing.id, v_existing.status, v_existing.amount_usd,
                        coalesce(v_balance, 0), coalesce(v_reserved, 0);
    return;
  end if;

  -- Row lock on this user's credits row for the rest of this
  -- transaction -- a second concurrent reserve_credit() for the same
  -- user blocks here until this one commits, so the balance check below
  -- can never race against another reservation.
  insert into public.credits (user_id) values (p_user_id)
    on conflict (user_id) do nothing;
  select c.balance_usd, c.reserved_usd into v_balance, v_reserved
    from public.credits c where c.user_id = p_user_id for update;

  if v_balance - v_reserved < p_amount_usd then
    raise exception 'insufficient_funds: balance % reserved % requested %',
      v_balance, v_reserved, p_amount_usd using errcode = 'P0001';
  end if;

  insert into public.credit_reservations (id, user_id, amount_usd, status, note)
    values (p_idempotency_key, p_user_id, p_amount_usd, 'reserved', p_note);

  update public.credits set reserved_usd = reserved_usd + p_amount_usd, updated_at = now()
    where user_id = p_user_id
    returning reserved_usd into v_reserved;

  insert into public.credit_ledger (user_id, delta_usd, reason, note)
    values (p_user_id, 0, 'reserve', coalesce(p_note, p_idempotency_key));

  return query select p_idempotency_key, 'reserved'::text, p_amount_usd, v_balance, v_reserved;
end;
$$;

-- ---------- capture_credit_reservation ----------
create or replace function public.capture_credit_reservation(
  p_idempotency_key text,
  p_note text default null
) returns table (id text, status text, amount_usd numeric, balance_usd numeric)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_res record;
  v_balance numeric;
begin
  select cr.* into v_res from public.credit_reservations cr
    where cr.id = p_idempotency_key for update;
  if not found then
    raise exception 'no such reservation: %', p_idempotency_key using errcode = 'P0002';
  end if;

  if v_res.status = 'captured' then
    select c.balance_usd into v_balance from public.credits c where c.user_id = v_res.user_id;
    return query select v_res.id, v_res.status, v_res.amount_usd, coalesce(v_balance, 0);
    return;
  end if;
  if v_res.status = 'released' then
    raise exception 'reservation already released, cannot capture: %', p_idempotency_key using errcode = 'P0003';
  end if;

  update public.credits
    set balance_usd = balance_usd - v_res.amount_usd,
        reserved_usd = reserved_usd - v_res.amount_usd,
        updated_at = now()
    where user_id = v_res.user_id
    returning balance_usd into v_balance;

  update public.credit_reservations set status = 'captured', resolved_at = now()
    where id = p_idempotency_key;

  insert into public.credit_ledger (user_id, delta_usd, reason, note)
    values (v_res.user_id, -v_res.amount_usd, 'generation', coalesce(p_note, v_res.note));

  return query select v_res.id, 'captured'::text, v_res.amount_usd, v_balance;
end;
$$;

-- ---------- release_credit_reservation ----------
create or replace function public.release_credit_reservation(
  p_idempotency_key text
) returns table (id text, status text, amount_usd numeric)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_res record;
begin
  select cr.* into v_res from public.credit_reservations cr
    where cr.id = p_idempotency_key for update;
  if not found then
    raise exception 'no such reservation: %', p_idempotency_key using errcode = 'P0002';
  end if;

  if v_res.status in ('released', 'captured') then
    return query select v_res.id, v_res.status, v_res.amount_usd;
    return;
  end if;

  update public.credits set reserved_usd = reserved_usd - v_res.amount_usd, updated_at = now()
    where user_id = v_res.user_id;

  update public.credit_reservations set status = 'released', resolved_at = now()
    where id = p_idempotency_key;

  insert into public.credit_ledger (user_id, delta_usd, reason, note)
    values (v_res.user_id, 0, 'release', v_res.note);

  return query select v_res.id, 'released'::text, v_res.amount_usd;
end;
$$;

-- ---------- refund_credit ----------
-- Independent of the reserve/capture cycle -- grants money back after a
-- capture already landed (e.g. a top-up webhook credit, or reversing a
-- generation after the fact). p_idempotency_key still guards against a
-- retried refund call double-granting.
create or replace function public.refund_credit(
  p_user_id uuid,
  p_amount_usd numeric,
  p_idempotency_key text,
  p_note text default null
) returns table (balance_usd numeric)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_balance numeric;
  v_reason text;
begin
  if p_amount_usd <= 0 then
    raise exception 'amount_usd must be positive';
  end if;

  if exists (select 1 from public.credit_ledger where idempotency_key = p_idempotency_key) then
    select c.balance_usd into v_balance from public.credits c where c.user_id = p_user_id;
    return query select coalesce(v_balance, 0);
    return;
  end if;

  insert into public.credits (user_id) values (p_user_id)
    on conflict (user_id) do nothing;

  update public.credits set balance_usd = balance_usd + p_amount_usd, updated_at = now()
    where user_id = p_user_id
    returning balance_usd into v_balance;

  v_reason := case when p_note like 'topup:%' then 'manual_topup' else 'refund' end;
  insert into public.credit_ledger (user_id, delta_usd, reason, note, idempotency_key)
    values (p_user_id, p_amount_usd, v_reason, p_note, p_idempotency_key);

  return query select v_balance;
end;
$$;

-- ---------- debit_credit ----------
-- Claws money back that was already granted -- a Payme/Click transaction
-- that completed and was later cancelled (a chargeback/fraud reversal),
-- not part of the reserve/capture/release cycle either. Clamps at zero
-- rather than letting balance_usd go negative, matching this table's
-- existing check (balance_usd >= 0) and the pre-ledger record_spend()
-- behaviour it replaces.
create or replace function public.debit_credit(
  p_user_id uuid,
  p_amount_usd numeric,
  p_idempotency_key text,
  p_note text default null
) returns table (balance_usd numeric)
language plpgsql
security definer
set search_path = public
as $$
declare
  v_balance numeric;
begin
  if p_amount_usd <= 0 then
    raise exception 'amount_usd must be positive';
  end if;

  if exists (select 1 from public.credit_ledger where idempotency_key = p_idempotency_key) then
    select c.balance_usd into v_balance from public.credits c where c.user_id = p_user_id;
    return query select coalesce(v_balance, 0);
    return;
  end if;

  insert into public.credits (user_id) values (p_user_id)
    on conflict (user_id) do nothing;

  update public.credits set balance_usd = greatest(0, balance_usd - p_amount_usd), updated_at = now()
    where user_id = p_user_id
    returning balance_usd into v_balance;

  insert into public.credit_ledger (user_id, delta_usd, reason, note, idempotency_key)
    values (p_user_id, -p_amount_usd, 'refund', p_note, p_idempotency_key);

  return query select v_balance;
end;
$$;

grant execute on function public.reserve_credit(uuid, numeric, text, text) to service_role;
grant execute on function public.capture_credit_reservation(text, text) to service_role;
grant execute on function public.release_credit_reservation(text) to service_role;
grant execute on function public.refund_credit(uuid, numeric, text, text) to service_role;
grant execute on function public.debit_credit(uuid, numeric, text, text) to service_role;
