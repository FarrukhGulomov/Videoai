"""
Tests for avatar/identity consent attestation, avatar-specific abuse
rate limiting, and the report/auto-unpublish flow -- see
webapp/server.py's _require_identity_consent, _check_avatar_rate_limit,
_check_report_rate_limit, and Handler._report_job.

Run: python3 -m unittest discover -s webapp/tests -v
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class _FakeHandler:
    """Minimal stand-in for server.Handler -- just enough attributes for
    the methods under test (_client_ip, _send) without a real socket."""

    def __init__(self, ip="203.0.113.5"):
        self.headers = {}
        self.client_address = (ip, 54321)
        self.sent = None

    def _send(self, status, body, ctype=None):
        self.sent = (status, body)
        return body


class IdentityConsentTests(unittest.TestCase):
    def test_missing_attestation_rejected(self):
        with self.assertRaises(ValueError):
            server._require_identity_consent({"consent_type": "self"})

    def test_attested_but_no_type_rejected(self):
        with self.assertRaises(ValueError):
            server._require_identity_consent({"consent_attested": True})

    def test_invalid_type_rejected(self):
        with self.assertRaises(ValueError):
            server._require_identity_consent({"consent_attested": True, "consent_type": "someone-elses"})

    def test_self_accepted(self):
        self.assertEqual(
            server._require_identity_consent({"consent_attested": True, "consent_type": "self"}), "self")

    def test_authorized_accepted(self):
        self.assertEqual(
            server._require_identity_consent({"consent_attested": True, "consent_type": "authorized"}),
            "authorized")

    def test_falsy_attestation_rejected(self):
        with self.assertRaises(ValueError):
            server._require_identity_consent({"consent_attested": False, "consent_type": "self"})


class AvatarRateLimitTests(unittest.TestCase):
    def setUp(self):
        server._AVATAR_ATTEMPTS.clear()

    def test_allows_up_to_the_limit(self):
        for _ in range(server.AVATAR_RATE_LIMIT):
            server._check_avatar_rate_limit("user-rate-1")  # must not raise

    def test_rejects_beyond_the_limit(self):
        for _ in range(server.AVATAR_RATE_LIMIT):
            server._check_avatar_rate_limit("user-rate-2")
        with self.assertRaises(ValueError):
            server._check_avatar_rate_limit("user-rate-2")

    def test_different_users_have_independent_limits(self):
        for _ in range(server.AVATAR_RATE_LIMIT):
            server._check_avatar_rate_limit("user-a")
        server._check_avatar_rate_limit("user-b")  # must not raise -- separate bucket


class ReportRateLimitTests(unittest.TestCase):
    def setUp(self):
        server._REPORT_ATTEMPTS.clear()

    def test_allows_up_to_the_limit(self):
        handler = _FakeHandler()
        for _ in range(server.REPORT_RATE_LIMIT):
            server._check_report_rate_limit(handler)

    def test_rejects_beyond_the_limit(self):
        handler = _FakeHandler()
        for _ in range(server.REPORT_RATE_LIMIT):
            server._check_report_rate_limit(handler)
        with self.assertRaises(ValueError):
            server._check_report_rate_limit(handler)

    def test_different_ips_have_independent_limits(self):
        for _ in range(server.REPORT_RATE_LIMIT):
            server._check_report_rate_limit(_FakeHandler(ip="10.1.1.1"))
        server._check_report_rate_limit(_FakeHandler(ip="10.1.1.2"))  # must not raise


class ReportJobTests(unittest.TestCase):
    def setUp(self):
        server._jobs.clear()
        server._idem_index.clear()
        server._REPORT_ATTEMPTS.clear()

    def test_report_requires_a_reason(self):
        job = server._new_job("avatar", "owner-1", status="done",
                               outputs=["/media/avatar/x.mp4"], public=True)
        handler = _FakeHandler()
        with self.assertRaises(ValueError):
            server.Handler._report_job(handler, job["id"], {"reason": "   "})

    def test_report_unknown_job_rejected(self):
        handler = _FakeHandler()
        with self.assertRaises(ValueError):
            server.Handler._report_job(handler, "does-not-exist", {"reason": "bad"})

    def test_report_auto_unpublishes_and_flags(self):
        job = server._new_job("avatar", "owner-2", status="done",
                               outputs=["/media/avatar/y.mp4"], public=True)
        handler = _FakeHandler()
        server.Handler._report_job(handler, job["id"], {"reason": "This is not my likeness"})
        self.assertEqual(handler.sent[0], 200)
        stored = server._jobs[job["id"]]
        self.assertFalse(stored["public"])
        self.assertTrue(stored["reported"])

    def test_report_no_longer_appears_in_gallery_dedup_index(self):
        """A reported (now unpublished) job must not still be servable
        through _media_job_for_path as a 'public' item -- _serve_media's
        gating checks job.get('public'), so flipping it False here is
        what actually removes gallery-level access pending review."""
        job = server._new_job("avatar", "owner-3", status="done",
                               outputs=["/media/avatar/z.mp4"], public=True)
        handler = _FakeHandler()
        server.Handler._report_job(handler, job["id"], {"reason": "consent dispute"})
        found = server._media_job_for_path("avatar/z.mp4")
        self.assertIsNotNone(found)
        self.assertFalse(found["public"])


if __name__ == "__main__":
    unittest.main()
