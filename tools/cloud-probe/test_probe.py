"""Verify scoped requests and failure reporting without any Apple traffic."""

from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from probe import (AUTH_REQUESTS, ProbeBlocked, allowed_request, authenticated_account,
                   discover_zones, new_run, request_guard)


ROOT = "https://p123-ckdatabasews.icloud.com"
PRIVATE = ROOT + "/database/1/com.apple.photos.cloud/production/private"


class ProbeTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_authentication_route_allowlist(self):
        for method, host, path in AUTH_REQUESTS:
            with self.subTest(path=path):
                self.assertTrue(allowed_request(method, f"https://{host}{path}", False, None))

    def test_zones_require_discovery_authorization(self):
        url = PRIVATE + "/zones/list"
        self.assertFalse(allowed_request("POST", url, False, ROOT))
        self.assertFalse(allowed_request("POST", url, True, None))
        self.assertTrue(allowed_request("POST", url, True, ROOT))
        self.assertFalse(allowed_request("GET", url, True, ROOT))

    def test_transport_requires_tls(self):
        calls = []
        class Adapter:
            def send(self, request, **kwargs):
                calls.append(request.url)
                return "fake-response"

        module = SimpleNamespace(HTTPAdapter=Adapter)
        with patch.dict(sys.modules, {"requests.adapters": module}):
            with request_guard(True) as state:
                adapter = Adapter()
                auth = SimpleNamespace(method="POST", url="https://idmsa.apple.com/appleauth/auth/signin/init")
                self.assertEqual(adapter.send(auth, verify=True), "fake-response")
                for verification in (False, None, ""):
                    with self.subTest(verification=verification), self.assertRaises(ProbeBlocked):
                        adapter.send(auth, verify=verification)
                state["photo_root"] = ROOT
                query = SimpleNamespace(method="POST", url=PRIVATE + "/records/query")
                with self.assertRaises(ProbeBlocked):
                    adapter.send(query, verify=True)
                redirected = SimpleNamespace(method="GET", url="https://evil.example/steal")
                with self.assertRaises(ProbeBlocked):
                    adapter.send(redirected, verify=True)
                self.assertEqual(state["authentication_requests"], 1)
                self.assertEqual(state["zone_requests"], 0)
        self.assertEqual(calls, [auth.url])

    def test_discovery_blocks_media_and_modifications(self):
        for suffix in ("/records/query", "/internal/records/query/batch", "/records/modify",
                       "/records/lookup", "/assets/upload", "/zones/delete"):
            for method in ("POST", "GET", "DELETE", "PUT"):
                with self.subTest(suffix=suffix, method=method):
                    self.assertFalse(allowed_request(method, PRIVATE + suffix, True, ROOT))
        self.assertFalse(allowed_request("GET", "https://p123-content.icloud.com/signed-photo", True, ROOT))

    def test_untrusted_hosts_are_rejected(self):
        for root in ("https://evil.example", "https://icloud.com.evil.example", "http://p123.icloud.com",
                     "https://user:password@p123.icloud.com", "https://p123.icloud.com:444",
                     ROOT + "?token=secret"):
            with self.subTest(root=root):
                self.assertFalse(allowed_request("POST", root + "/database/1/com.apple.photos.cloud/production/private/zones/list", True, root))
        for url in ("http://idmsa.apple.com/appleauth/auth/signin/init",
                    "https://idmsa.apple.com.evil.example/appleauth/auth/signin/init",
                    "https://user:password@idmsa.apple.com/appleauth/auth/signin/init"):
            self.assertFalse(allowed_request("POST", url, False, None))

    def service(self, responses):
        class Response:
            def __init__(self, value):
                self.value = value

            def raise_for_status(self):
                pass

            def json(self):
                return self.value

        class Session:
            def post(self, url, **kwargs):
                database = "private" if "/private/" in url else "shared"
                value = responses[database]
                if isinstance(value, Exception):
                    raise value
                return Response(value)

        return SimpleNamespace(session=Session())

    def test_zone_identity_is_preserved(self):
        identity = {"zoneName": "synthetic-zone", "ownerRecordName": "synthetic-owner", "futureIdentity": "fixture"}
        service = self.service({"private": {"zones": [{"zoneID": identity}]},
                                "shared": {"zones": [{"zoneID": {"zoneName": "shared-zone"}}]}})
        result, errors = discover_zones(service, ROOT)
        self.assertEqual(errors, [])
        self.assertEqual(result["private"][0]["zone_id"], identity)
        self.assertNotIn("ownerRecordName", result["shared"][0]["zone_id"])

    def test_authenticated_account_requires_agreement(self):
        for response, request in ((1234, "1234"), ("1234", 1234), ("synthetic-account", None)):
            service = SimpleNamespace(data={"dsInfo": {"dsid": response}}, params={"dsid": request})
            with self.subTest(response=response):
                self.assertEqual(authenticated_account(service), str(response))
        for response, request in ((None, "1234"), ("1234", "different"), (True, "True"),
                                  (0, "0"), (1.5, "1.5"), (" ", None), ("1234", False)):
            service = SimpleNamespace(data={"dsInfo": {"dsid": response}}, params={"dsid": request})
            with self.subTest(response=response, request=request), self.assertRaises(ProbeBlocked):
                authenticated_account(service)

    def test_library_failure_is_reported(self):
        service = self.service({"private": {"zones": []}, "shared": RuntimeError("SECRET_TOKEN")})
        result, errors = discover_zones(service, ROOT)
        self.assertEqual(result, {"private": []})
        self.assertEqual(errors, ["shared: RuntimeError"])
        self.assertNotIn("SECRET_TOKEN", str(errors))

    def test_invalid_library_responses_are_reported(self):
        for value in ({"serverErrorCode": "CONFLICT", "zones": []}, {"zones": [] , "moreComing": True},
                      {"records": []}, {"zones": [{"zoneID": {}}]},
                      {"zones": [{"zoneID": {"zoneName": "same"}}, {"zoneID": {"zoneName": "same"}}]}):
            service = self.service({"private": value, "shared": {"zones": []}})
            with self.subTest(value=value):
                result, errors = discover_zones(service, ROOT)
                self.assertNotIn("private", result)
                self.assertEqual(errors, ["private: ProbeBlocked"])

    def test_output_is_fresh_and_scoped(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            repo = Path(directory)
            first, second = new_run(repo), new_run(repo)
            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, repo / ".smoke")
            self.assertTrue((first / "cookies").is_dir())

    def test_output_link_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            repo = Path(directory)
            outside = repo / "synthetic-outside"
            outside.mkdir()
            try:
                (repo / ".smoke").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(str(error))
            with self.assertRaises(ProbeBlocked):
                new_run(repo)


if __name__ == "__main__":
    unittest.main()
