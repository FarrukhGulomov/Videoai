"""
Tests for webapp/ledger.py's InMemoryLedger -- the same
reserve/capture/release/refund/debit contract SupabaseLedger implements
against Postgres (see 04-atomic-credits.sql), exercised here without a
live database. This is what's actually testable in an environment with
no Supabase credentials; the SQL functions themselves still need a real
run against a Supabase branch/project before going live (see the
top-level checkpoint report for that credential-dependent gap).

Run: python3 -m unittest discover -s webapp/tests -v
(or: python3 -m unittest webapp.tests.test_ledger -v, from the repo root,
with webapp/ importable -- see webapp/tests/__init__.py's sys.path setup
via the shared _bootstrap module below)
"""

import pathlib
import sys
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ledger  # noqa: E402


class ReserveTests(unittest.TestCase):
    def setUp(self):
        self.ledger = ledger.InMemoryLedger()

    def test_reserve_rejects_over_balance(self):
        self.ledger.grant("u1", 5.0)
        with self.assertRaises(ledger.InsufficientFundsError):
            self.ledger.reserve("u1", 5.01, "k1")

    def test_reserve_exact_balance_succeeds(self):
        self.ledger.grant("u1", 5.0)
        r = self.ledger.reserve("u1", 5.0, "k1")
        self.assertEqual(r.status, "reserved")
        # fully reserved -- nothing left to spend
        self.assertEqual(self.ledger.get_balance("u1"), 0.0)

    def test_zero_balance_is_rejected_closed(self):
        with self.assertRaises(ledger.InsufficientFundsError):
            self.ledger.reserve("brand-new-user", 0.01, "k1")

    def test_concurrent_reservations_only_one_wins(self):
        """The P0 requirement this whole module exists for: two
        concurrent requests against a balance that can only cover one of
        them must not both succeed."""
        self.ledger.grant("u1", 10.0)
        outcomes = []
        lock = threading.Lock()

        def attempt(i):
            try:
                self.ledger.reserve("u1", 6.0, f"attempt-{i}")
                with lock:
                    outcomes.append("ok")
            except ledger.InsufficientFundsError:
                with lock:
                    outcomes.append("rejected")

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count("ok"), 1, outcomes)
        self.assertEqual(outcomes.count("rejected"), 7, outcomes)

    def test_concurrent_reservations_across_users_are_independent(self):
        """Different users' reservations must never block or interfere
        with each other -- only same-user contention should serialize."""
        for uid in ("a", "b", "c"):
            self.ledger.grant(uid, 5.0)
        results = {}

        def attempt(uid):
            results[uid] = self.ledger.reserve(uid, 5.0, f"key-{uid}").status

        threads = [threading.Thread(target=attempt, args=(u,)) for u in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, {"a": "reserved", "b": "reserved", "c": "reserved"})


class DuplicateRequestTests(unittest.TestCase):
    """'Duplicate requests cannot create duplicate charges' -- the same
    idempotency key, called any number of times, must reserve/capture at
    most once."""

    def setUp(self):
        self.ledger = ledger.InMemoryLedger()
        self.ledger.grant("u1", 10.0)

    def test_repeated_reserve_does_not_double_hold(self):
        self.ledger.reserve("u1", 6.0, "dup")
        self.ledger.reserve("u1", 6.0, "dup")
        self.ledger.reserve("u1", 6.0, "dup")
        # If this were double-reserving, three $6 holds against $10 would
        # have raised InsufficientFundsError on the second call.
        self.assertEqual(self.ledger.get_balance("u1"), 4.0)

    def test_repeated_capture_charges_once(self):
        self.ledger.reserve("u1", 6.0, "dup")
        self.ledger.capture("dup")
        self.ledger.capture("dup")
        self.ledger.capture("dup")
        self.assertEqual(self.ledger.get_balance("u1"), 4.0)  # not 10 - 18

    def test_repeated_release_is_a_noop(self):
        self.ledger.reserve("u1", 6.0, "dup")
        self.ledger.release("dup")
        self.ledger.release("dup")
        self.assertEqual(self.ledger.get_balance("u1"), 10.0)

    def test_repeated_refund_grants_once(self):
        b1 = self.ledger.refund("u2", 3.0, "refund-key")
        b2 = self.ledger.refund("u2", 3.0, "refund-key")
        b3 = self.ledger.refund("u2", 3.0, "refund-key")
        self.assertEqual(b1, b2, b3)
        self.assertEqual(self.ledger.get_balance("u2"), 3.0)

    def test_repeated_debit_claws_back_once(self):
        self.ledger.grant("u3", 10.0)
        b1 = self.ledger.debit("u3", 4.0, "debit-key")
        b2 = self.ledger.debit("u3", 4.0, "debit-key")
        self.assertEqual(b1, b2)
        self.assertEqual(self.ledger.get_balance("u3"), 6.0)


class ExpiredReservationRetryTests(unittest.TestCase):
    """A failed attempt must be retryable under a fresh reservation --
    see server.py's _reservation_key_for(), which suffixes the key when
    the prior job for the same idempotency key ended in error. This
    tests the ledger half of that contract: a released key must not
    block a brand new reservation under a *different* key for the same
    logical request."""

    def setUp(self):
        self.ledger = ledger.InMemoryLedger()
        self.ledger.grant("u1", 10.0)

    def test_release_then_fresh_key_reserve_succeeds(self):
        self.ledger.reserve("u1", 6.0, "attempt-1")
        self.ledger.release("attempt-1")
        self.assertEqual(self.ledger.get_balance("u1"), 10.0)
        r = self.ledger.reserve("u1", 6.0, "attempt-1#retry-abcd1234")
        self.assertEqual(r.status, "reserved")
        self.assertEqual(self.ledger.get_balance("u1"), 4.0)

    def test_capture_after_release_is_rejected(self):
        self.ledger.reserve("u1", 6.0, "attempt-1")
        self.ledger.release("attempt-1")
        with self.assertRaises(ledger.LedgerError):
            self.ledger.capture("attempt-1")

    def test_capture_unknown_reservation_fails_closed(self):
        with self.assertRaises(ledger.LedgerError):
            self.ledger.capture("never-reserved")

    def test_release_unknown_reservation_fails_closed(self):
        with self.assertRaises(ledger.LedgerError):
            self.ledger.release("never-reserved")


if __name__ == "__main__":
    unittest.main()
