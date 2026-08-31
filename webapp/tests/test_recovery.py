"""
Tests for restart recovery (_recover_orphaned_jobs) and topup crediting
(_credit_topup) -- both exercised against the real InMemoryLedger
(active by default in this environment since SUPABASE_URL isn't set),
not a mock, so these are real reserve/capture/release calls, just
without a live fal.ai or Supabase behind them.

Run: python3 -m unittest discover -s webapp/tests -v
"""

import pathlib
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ledger  # noqa: E402
import server  # noqa: E402


def _drain_executor():
    """_recover_orphaned_jobs() and the job workers submit work to
    server.JOB_EXECUTOR instead of running it inline -- wait for
    everything currently queued to finish before asserting on it."""
    server.JOB_EXECUTOR.shutdown(wait=True)
    server.JOB_EXECUTOR = server.concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="job-test")


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self):
        server._jobs.clear()
        server._idem_index.clear()
        server.ledger._ledger = ledger.InMemoryLedger()  # fresh ledger per test
        self.ledger = server.ledger.get_ledger()
        self.ledger.grant("user-A", 10.0)

    def test_orphan_with_no_fal_handle_is_released_and_marked_failed(self):
        """Process died before ever reaching fal (or before the handle
        was persisted) -- nothing to reconcile against, so fail closed:
        release the hold, mark the job failed, never charge for it."""
        self.ledger.reserve("user-A", 3.0, "orphan-1")
        job = server._new_job("image", "user-A", status="queued",
                               idempotency_key="orphan-1", reservation_id="orphan-1")
        job_id = job["id"]

        server._recover_orphaned_jobs()
        _drain_executor()

        job = server._jobs[job_id]
        self.assertEqual(job["status"], "error")
        self.assertIn("restart", job["error"].lower())
        # the hold must be released, not silently forgotten
        with self.assertRaises(ledger.LedgerError):
            self.ledger.capture("orphan-1")  # already released -> capture must fail
        self.assertEqual(self.ledger.get_balance("user-A"), 10.0)

    def test_orphan_with_recoverable_fal_handle_completes_and_captures(self):
        """Process died while waiting on fal, but the handle survived on
        disk -- resuming the poll and getting a real result must still
        capture the reservation (the fal spend already happened; the
        customer should get their result and be charged exactly once)."""
        self.ledger.reserve("user-A", 3.0, "orphan-2")
        job = server._new_job(
            "video", "user-A", status="running",
            idempotency_key="orphan-2", reservation_id="orphan-2",
            fal_request_id="req-123", fal_status_url="https://fal.example/status",
            fal_response_url="https://fal.example/response",
        )
        job_id = job["id"]

        with mock.patch("server.factory.fal_poll") as poll:
            poll.return_value = {"videos": [{"url": "https://cdn.example/out.mp4"}], "_request_id": "req-123"}
            server._recover_orphaned_jobs()
            _drain_executor()

        job = server._jobs[job_id]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["outputs"], ["https://cdn.example/out.mp4"])
        self.assertEqual(self.ledger.get_balance("user-A"), 7.0)  # captured, not double-charged

    def test_orphan_whose_fal_job_actually_failed_is_released(self):
        self.ledger.reserve("user-A", 3.0, "orphan-3")
        job = server._new_job(
            "image", "user-A", status="running",
            idempotency_key="orphan-3", reservation_id="orphan-3",
            fal_request_id="req-999", fal_status_url="https://fal.example/status",
            fal_response_url="https://fal.example/response",
        )
        job_id = job["id"]

        with mock.patch("server.factory.fal_poll", side_effect=RuntimeError("fal job FAILED")):
            server._recover_orphaned_jobs()
            _drain_executor()

        job = server._jobs[job_id]
        self.assertEqual(job["status"], "error")
        self.assertEqual(self.ledger.get_balance("user-A"), 10.0)  # released, never charged


class TopupCreditingTests(unittest.TestCase):
    def setUp(self):
        server._topups.clear()
        server.ledger._ledger = ledger.InMemoryLedger()

    def test_credit_topup_grants_once(self):
        topup = server._new_topup("user-Z", 5.0, "stripe")
        r1 = server._credit_topup(topup["id"], note="stripe:evt_1")
        self.assertIsNotNone(r1)
        # second call (duplicated webhook) must be a no-op per the
        # in-memory _topups status check -- see _credit_topup's docstring
        r2 = server._credit_topup(topup["id"], note="stripe:evt_1")
        self.assertIsNone(r2)
        self.assertEqual(server.ledger.get_ledger().get_balance("user-Z"), 5.0)

    def test_credit_topup_is_idempotent_at_the_ledger_layer_too(self):
        """Even if the in-memory _topups status check were bypassed
        (e.g. two processes, no shared memory), the ledger's own
        idempotency key (topup:<id>) must still prevent a double-grant."""
        topup = server._new_topup("user-Y", 7.0, "click")
        server._credit_topup(topup["id"], note="click:1")
        # Simulate a second, independent crediting attempt for the same
        # topup id by calling the ledger directly, bypassing the
        # in-memory guard _credit_topup itself would normally apply.
        server.ledger.get_ledger().refund("user-Y", 7.0, f"topup:{topup['id']}", note="click:1 retried")
        self.assertEqual(server.ledger.get_ledger().get_balance("user-Y"), 7.0)


if __name__ == "__main__":
    unittest.main()
