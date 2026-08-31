"""
Atomic reserve / capture / release / refund credit ledger.

Replaces the old webapp/supabase_client.py record_spend() -- a
read-balance-then-write-balance pair of HTTP round trips with no
transaction around them, so two concurrent paid requests for the same
user could both read the same starting balance and both proceed. Every
paid endpoint in webapp/server.py now goes through this module instead:

  1. reserve(user_id, cost, idempotency_key, note) BEFORE calling the
     provider -- fails closed (raises InsufficientFundsError) if the
     money isn't there, and holds it so a second concurrent request for
     the same user sees it as unavailable.
  2. capture(idempotency_key, note) AFTER the provider's output is
     fetched and validated -- turns the hold into an actual ledger spend.
  3. release(idempotency_key) instead, if generation fails, the output
     doesn't validate, or the server restarts mid-job -- drops the hold,
     nothing was ever charged.

reserve/capture/release are all idempotent on idempotency_key: calling
any of them twice (a client retry, a resumed job after a restart)
returns the same result instead of erroring or double-applying. See
04-atomic-credits.sql for the Postgres side (SupabaseLedger) and
InMemoryLedger below for the fallback/test double.

Two implementations behind the same interface:
  - SupabaseLedger: calls the Postgres functions in 04-atomic-credits.sql
    via PostgREST RPC with the service role key -- the row lock inside
    each function's transaction is what makes this actually atomic
    across concurrent requests and across process restarts.
  - InMemoryLedger: a spec-equivalent, thread-safe, single-process
    implementation with the same reserve/capture/release/refund
    semantics. Used when Supabase isn't configured (so a bare local
    install still gets correct-under-concurrency accounting instead of a
    silent bypass) and by webapp/tests/ (no live database needed to
    exercise the *interface contract* -- 04-atomic-credits.sql is the
    source of truth for what actually runs against real money; this is a
    same-shape double for testing the Python side that calls it).

Fail-closed throughout: every method raises LedgerError (or the more
specific InsufficientFundsError) on any failure -- network, insufficient
funds, Supabase unreachable -- rather than returning a sentinel a caller
could accidentally treat as success.
"""

import threading

import supabase_client as db


class LedgerError(Exception):
    pass


class InsufficientFundsError(LedgerError):
    pass


class Reservation:
    __slots__ = ("id", "status", "amount_usd", "balance_usd")

    def __init__(self, id, status, amount_usd, balance_usd=None):
        self.id = id
        self.status = status
        self.amount_usd = amount_usd
        self.balance_usd = balance_usd


def _row(rows):
    return rows[0] if isinstance(rows, list) and rows else rows


class SupabaseLedger:
    def get_balance(self, user_id, access_token):
        return db.get_balance(user_id, access_token)

    def reserve(self, user_id, amount_usd, idempotency_key, note=None):
        try:
            row = _row(db.rpc("reserve_credit", {
                "p_user_id": user_id, "p_amount_usd": round(float(amount_usd), 4),
                "p_idempotency_key": idempotency_key, "p_note": note,
            }))
        except db.SupabaseError as exc:
            if "insufficient_funds" in str(exc):
                raise InsufficientFundsError(str(exc)) from None
            raise LedgerError(f"Could not reserve funds: {exc}") from None
        if not row:
            raise LedgerError("reserve_credit returned no row")
        return Reservation(row["id"], row["status"], float(row["amount_usd"]), float(row["balance_usd"]))

    def capture(self, idempotency_key, note=None):
        try:
            row = _row(db.rpc("capture_credit_reservation",
                               {"p_idempotency_key": idempotency_key, "p_note": note}))
        except db.SupabaseError as exc:
            raise LedgerError(f"Could not capture reservation {idempotency_key}: {exc}") from None
        if not row:
            raise LedgerError("capture_credit_reservation returned no row")
        return Reservation(row["id"], row["status"], float(row["amount_usd"]), float(row["balance_usd"]))

    def release(self, idempotency_key):
        try:
            row = _row(db.rpc("release_credit_reservation", {"p_idempotency_key": idempotency_key}))
        except db.SupabaseError as exc:
            raise LedgerError(f"Could not release reservation {idempotency_key}: {exc}") from None
        if not row:
            raise LedgerError("release_credit_reservation returned no row")
        return Reservation(row["id"], row["status"], float(row["amount_usd"]))

    def refund(self, user_id, amount_usd, idempotency_key, note=None):
        try:
            row = _row(db.rpc("refund_credit", {
                "p_user_id": user_id, "p_amount_usd": round(float(amount_usd), 4),
                "p_idempotency_key": idempotency_key, "p_note": note,
            }))
        except db.SupabaseError as exc:
            raise LedgerError(f"Could not grant/refund credit: {exc}") from None
        if not row:
            raise LedgerError("refund_credit returned no row")
        return float(row["balance_usd"])

    def debit(self, user_id, amount_usd, idempotency_key, note=None):
        """Claws back money already granted (e.g. a Payme/Click
        transaction that completed and was later cancelled) -- the
        inverse of refund(), not part of the reserve/capture/release
        cycle either. Clamps at zero rather than going negative."""
        try:
            row = _row(db.rpc("debit_credit", {
                "p_user_id": user_id, "p_amount_usd": round(float(amount_usd), 4),
                "p_idempotency_key": idempotency_key, "p_note": note,
            }))
        except db.SupabaseError as exc:
            raise LedgerError(f"Could not debit credit: {exc}") from None
        if not row:
            raise LedgerError("debit_credit returned no row")
        return float(row["balance_usd"])


class InMemoryLedger:
    """Thread-safe, single-process, spec-equivalent to SupabaseLedger."""

    def __init__(self):
        self._lock = threading.Lock()
        self._balances = {}       # user_id -> {"balance": float, "reserved": float}
        self._reservations = {}   # idempotency_key -> {user_id, amount, status, note}
        self._refund_keys = set()

    def _acct(self, user_id):
        return self._balances.setdefault(user_id, {"balance": 0.0, "reserved": 0.0})

    def get_balance(self, user_id, access_token=None):
        with self._lock:
            acct = self._acct(user_id)
            return acct["balance"] - acct["reserved"]

    def grant(self, user_id, amount_usd):
        """Test/local-dev convenience with no SupabaseLedger equivalent --
        real top-ups go through refund(), which is idempotent; this isn't,
        and exists only to seed a balance in tests."""
        with self._lock:
            self._acct(user_id)["balance"] += amount_usd

    def reserve(self, user_id, amount_usd, idempotency_key, note=None):
        if amount_usd <= 0:
            raise LedgerError("amount_usd must be positive")
        with self._lock:
            existing = self._reservations.get(idempotency_key)
            if existing:
                return Reservation(idempotency_key, existing["status"], existing["amount"],
                                    self._acct(existing["user_id"])["balance"])
            acct = self._acct(user_id)
            if acct["balance"] - acct["reserved"] < amount_usd:
                raise InsufficientFundsError(
                    f"insufficient_funds: balance {acct['balance']} reserved {acct['reserved']} "
                    f"requested {amount_usd}")
            acct["reserved"] += amount_usd
            self._reservations[idempotency_key] = {
                "user_id": user_id, "amount": amount_usd, "status": "reserved", "note": note,
            }
            return Reservation(idempotency_key, "reserved", amount_usd, acct["balance"])

    def capture(self, idempotency_key, note=None):
        with self._lock:
            res = self._reservations.get(idempotency_key)
            if not res:
                raise LedgerError(f"no such reservation: {idempotency_key}")
            acct = self._acct(res["user_id"])
            if res["status"] == "captured":
                return Reservation(idempotency_key, "captured", res["amount"], acct["balance"])
            if res["status"] == "released":
                raise LedgerError(f"reservation already released, cannot capture: {idempotency_key}")
            acct["balance"] -= res["amount"]
            acct["reserved"] -= res["amount"]
            res["status"] = "captured"
            return Reservation(idempotency_key, "captured", res["amount"], acct["balance"])

    def release(self, idempotency_key):
        with self._lock:
            res = self._reservations.get(idempotency_key)
            if not res:
                raise LedgerError(f"no such reservation: {idempotency_key}")
            if res["status"] in ("released", "captured"):
                return Reservation(idempotency_key, res["status"], res["amount"])
            self._acct(res["user_id"])["reserved"] -= res["amount"]
            res["status"] = "released"
            return Reservation(idempotency_key, "released", res["amount"])

    def refund(self, user_id, amount_usd, idempotency_key, note=None):
        if amount_usd <= 0:
            raise LedgerError("amount_usd must be positive")
        with self._lock:
            if idempotency_key in self._refund_keys:
                return self._acct(user_id)["balance"]
            self._refund_keys.add(idempotency_key)
            acct = self._acct(user_id)
            acct["balance"] += amount_usd
            return acct["balance"]

    def debit(self, user_id, amount_usd, idempotency_key, note=None):
        if amount_usd <= 0:
            raise LedgerError("amount_usd must be positive")
        with self._lock:
            if idempotency_key in self._refund_keys:
                return self._acct(user_id)["balance"]
            self._refund_keys.add(idempotency_key)
            acct = self._acct(user_id)
            acct["balance"] = max(0.0, acct["balance"] - amount_usd)
            return acct["balance"]


_ledger = None
_ledger_lock = threading.Lock()


def get_ledger():
    """The one ledger instance this process uses -- SupabaseLedger once
    Supabase is configured, InMemoryLedger otherwise (matching the same
    configured()-gated fallback db.py itself already uses everywhere
    else). Built lazily so it always reflects the env vars actually
    present at call time, not just at import time."""
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            _ledger = SupabaseLedger() if db.configured() else InMemoryLedger()
        return _ledger
