"""
Tests against webapp/server.py's own functions directly (no HTTP layer,
no live Supabase) -- SSRF/private-IP rejection, per-owner media gating,
idempotency-key dedup, output validation before charging, and
quote/generate SKU (pricing_version) parity.

Importing server.py has real side effects (it calls
factory.load_config() and, if FAL_KEY happens to be set in the test
environment, that's fine -- config loading doesn't require it). It does
NOT start listening on a socket at import time; main() does that, and
these tests never call it.

Run: python3 -m unittest discover -s webapp/tests -v
"""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class SsrfTests(unittest.TestCase):
    def test_rejects_non_http_scheme(self):
        with self.assertRaises(ValueError):
            server._require_public_url("file:///etc/passwd", "Video file")

    def test_rejects_relative_path(self):
        with self.assertRaises(ValueError):
            server._require_public_url("../../.env", "Video file")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            server._require_public_url("", "Video file")

    def test_allows_data_uri_only_when_opted_in(self):
        data_uri = "data:image/png;base64,aGVsbG8="
        with self.assertRaises(ValueError):
            server._require_public_url(data_uri, "Reference image", allow_data=False)
        # allow_data=True short-circuits before any DNS resolution happens.
        self.assertEqual(server._require_public_url(data_uri, "Reference image", allow_data=True), data_uri)

    def test_rejects_loopback_ip_literal(self):
        with self.assertRaises(ValueError):
            server._require_public_url("http://127.0.0.1/x.mp4", "Video file")

    def test_rejects_ipv6_loopback_literal(self):
        with self.assertRaises(ValueError):
            server._require_public_url("http://[::1]/x.mp4", "Video file")

    def test_rejects_cloud_metadata_ip(self):
        with self.assertRaises(ValueError):
            server._require_public_url("http://169.254.169.254/latest/meta-data/", "Video file")

    def test_rejects_private_range_literal(self):
        for ip in ("10.0.0.5", "172.16.0.5", "192.168.1.5"):
            with self.assertRaises(ValueError):
                server._require_public_url(f"http://{ip}/x.mp4", "Video file")

    def test_rejects_hostname_resolving_to_loopback(self):
        with mock.patch("server.socket.getaddrinfo") as m:
            m.return_value = [(2, 1, 6, "", ("127.0.0.1", 0))]
            with self.assertRaises(ValueError):
                server._require_public_url("http://internal.example/x.mp4", "Video file")

    def test_rejects_if_any_resolved_address_is_private(self):
        """Multiple A/AAAA records, only one of them private -- must
        still reject (an attacker only needs one bad address to matter,
        and a caller shouldn't have to guess which DNS answer will be
        used at fetch time)."""
        with mock.patch("server.socket.getaddrinfo") as m:
            m.return_value = [
                (2, 1, 6, "", ("93.184.216.34", 0)),   # public
                (2, 1, 6, "", ("10.0.0.1", 0)),        # private
            ]
            with self.assertRaises(ValueError):
                server._require_public_url("http://mixed.example/x.mp4", "Video file")

    def test_allows_genuinely_public_address(self):
        with mock.patch("server.socket.getaddrinfo") as m:
            m.return_value = [(2, 1, 6, "", ("93.184.216.34", 0))]
            self.assertEqual(
                server._require_public_url("http://example.com/x.mp4", "Video file"),
                "http://example.com/x.mp4",
            )

    def test_unresolvable_host_is_rejected_not_crashed(self):
        with mock.patch("server.socket.getaddrinfo", side_effect=server.socket.gaierror("nope")):
            with self.assertRaises(ValueError):
                server._require_public_url("http://does-not-resolve.invalid/x.mp4", "Video file")


class IsPublicIpTests(unittest.TestCase):
    def test_public_v4(self):
        self.assertTrue(server._is_public_ip("8.8.8.8"))

    def test_loopback_v4(self):
        self.assertFalse(server._is_public_ip("127.0.0.1"))

    def test_private_v4_ranges(self):
        for ip in ("10.1.2.3", "172.20.0.1", "192.168.50.1"):
            self.assertFalse(server._is_public_ip(ip))

    def test_link_local_v4(self):
        self.assertFalse(server._is_public_ip("169.254.1.1"))

    def test_unspecified(self):
        self.assertFalse(server._is_public_ip("0.0.0.0"))

    def test_public_v6(self):
        self.assertTrue(server._is_public_ip("2606:4700:4700::1111"))

    def test_private_v6(self):
        self.assertFalse(server._is_public_ip("fc00::1"))

    def test_garbage_is_not_public(self):
        self.assertFalse(server._is_public_ip("not-an-ip"))


class MediaOwnershipTests(unittest.TestCase):
    """Two-tenant isolation for /media/: a job's output must only be
    resolvable to its own owner (or an explicitly published one)."""

    def setUp(self):
        server._jobs.clear()
        server._idem_index.clear()

    def test_finds_owner_of_a_known_path(self):
        job = server._new_job("image", "user-A", outputs=["/media/a.png"], status="done")
        found = server._media_job_for_path("a.png")
        self.assertIsNotNone(found)
        self.assertEqual(found["owner_id"], "user-A")

    def test_unknown_path_returns_none(self):
        self.assertIsNone(server._media_job_for_path("never-generated.png"))

    def test_two_tenants_do_not_collide(self):
        server._new_job("image", "user-A", outputs=["/media/a.png"], status="done")
        server._new_job("image", "user-B", outputs=["/media/b.png"], status="done")
        self.assertEqual(server._media_job_for_path("a.png")["owner_id"], "user-A")
        self.assertEqual(server._media_job_for_path("b.png")["owner_id"], "user-B")

    def test_public_flag_is_visible_on_the_lookup(self):
        server._new_job("image", "user-B", outputs=["/media/pub.png"], status="done", public=True)
        found = server._media_job_for_path("pub.png")
        self.assertTrue(found["public"])


class IdempotencyKeyTests(unittest.TestCase):
    def test_same_body_same_user_same_key(self):
        body = {"prompt": "a cat", "count": 3}
        k1 = server._idempotency_key("image", "user-A", body)
        k2 = server._idempotency_key("image", "user-A", dict(body))
        self.assertEqual(k1, k2)

    def test_different_user_different_key(self):
        body = {"prompt": "a cat", "count": 3}
        k1 = server._idempotency_key("image", "user-A", body)
        k2 = server._idempotency_key("image", "user-B", body)
        self.assertNotEqual(k1, k2)

    def test_different_body_different_key(self):
        k1 = server._idempotency_key("image", "user-A", {"prompt": "a cat"})
        k2 = server._idempotency_key("image", "user-A", {"prompt": "a dog"})
        self.assertNotEqual(k1, k2)

    def test_approved_cost_excluded_from_key(self):
        """approved_cost describes approval, not the request -- a retry
        that only differs by echoing a (possibly re-quoted) cost back
        must still dedupe to the same job."""
        k1 = server._idempotency_key("video", "user-A", {"prompt": "x", "approved_cost": 1.23})
        k2 = server._idempotency_key("video", "user-A", {"prompt": "x", "approved_cost": 4.56})
        self.assertEqual(k1, k2)

    def test_explicit_client_key_is_honored(self):
        k1 = server._idempotency_key("video", "user-A", {"prompt": "x", "idempotency_key": "req-1"})
        k2 = server._idempotency_key("video", "user-A", {"prompt": "y", "idempotency_key": "req-1"})
        self.assertEqual(k1, k2)  # explicit key wins over differing bodies


class DedupAndRetryTests(unittest.TestCase):
    def setUp(self):
        server._jobs.clear()
        server._idem_index.clear()

    def test_pending_job_is_returned_without_creating_a_new_one(self):
        job = server._new_job("image", "user-A", idempotency_key="key-1", status="running")
        found = server._dedup_lookup("key-1")
        self.assertEqual(found["id"], job["id"])

    def test_done_job_is_returned(self):
        job = server._new_job("image", "user-A", idempotency_key="key-1", status="done")
        found = server._dedup_lookup("key-1")
        self.assertEqual(found["id"], job["id"])

    def test_errored_job_is_not_returned_as_a_dedup_hit(self):
        server._new_job("image", "user-A", idempotency_key="key-1", status="error")
        self.assertIsNone(server._dedup_lookup("key-1"))

    def test_reservation_key_is_base_key_on_first_attempt(self):
        self.assertEqual(server._reservation_key_for("key-1"), "key-1")

    def test_reservation_key_gets_fresh_suffix_after_error(self):
        server._new_job("image", "user-A", idempotency_key="key-1", status="error")
        retry_key = server._reservation_key_for("key-1")
        self.assertNotEqual(retry_key, "key-1")
        self.assertTrue(retry_key.startswith("key-1#retry-"))


class ValidateOutputsTests(unittest.TestCase):
    """Nothing gets captured (charged) for an empty/missing output."""

    def test_empty_list_rejected(self):
        with self.assertRaises(RuntimeError):
            server._validate_outputs([], kind="image")

    def test_list_of_none_rejected(self):
        with self.assertRaises(RuntimeError):
            server._validate_outputs([None], kind="video")

    def test_none_rejected(self):
        with self.assertRaises(RuntimeError):
            server._validate_outputs(None, kind="video")

    def test_valid_url_accepted(self):
        server._validate_outputs(["https://cdn.example/out.mp4"], kind="video")  # must not raise


class PricingVersionParityTests(unittest.TestCase):
    """A quote's pricing_version must match at generate time, or the
    request is refused rather than silently priced under a rule the
    quote never showed the customer."""

    def test_matching_version_is_accepted(self):
        handler = server.Handler.__new__(server.Handler)
        handler._require_pricing_version({"pricing_version": server.PRICING_VERSION})  # must not raise

    def test_missing_version_is_backward_compatible(self):
        handler = server.Handler.__new__(server.Handler)
        handler._require_pricing_version({})  # no error -- older/simple callers don't send it

    def test_mismatched_version_is_rejected(self):
        handler = server.Handler.__new__(server.Handler)
        with self.assertRaises(ValueError):
            handler._require_pricing_version({"pricing_version": (server.PRICING_VERSION or 0) + 999})


if __name__ == "__main__":
    unittest.main()
