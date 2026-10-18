"""Exercise actual loopback requests and their selection/confirmation boundary."""

from http.client import HTTPConnection
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from cleanup import Blocked, Fixture, ReviewSession, UnconditionalCloud, confirmation, prepare
from review import MAX_BODY, serve


class ReviewHttpTests(unittest.TestCase):
    def test_loopback_selection_and_request_guards(self):
        # No external DNS or account access, including during server startup.
        original = socket.getaddrinfo
        def loopback(host, *args, **kwargs):
            if host != "127.0.0.1":
                raise AssertionError("External network forbidden")
            return original(host, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary, patch.object(socket, "getaddrinfo", side_effect=loopback):
            fixture = Fixture(Path(temporary).resolve())
            plan = prepare(fixture)
            provider = UnconditionalCloud(fixture.snapshot)
            session = ReviewSession(plan, provider)
            with serve(session) as url:
                address = urlsplit(url)
                self.assertEqual(address.hostname, "127.0.0.1")
                token = address.fragment

                def request(method, path, body=None, **overrides):
                    headers = {"Host": address.netloc, "Authorization": "Bearer " + token}
                    if method == "POST":
                        headers.update(Origin="http://" + address.netloc, **{"Content-Type": "application/json"})
                    headers.update(overrides)
                    headers = {k: v for k, v in headers.items() if v is not None}
                    raw = json.dumps(body) if isinstance(body, dict) else body
                    connection = HTTPConnection("127.0.0.1", address.port, timeout=5)
                    try:
                        connection.request(method, path, raw, headers)
                        response = connection.getresponse()
                        return response.status, dict(response.getheaders()), response.read().decode()
                    finally:
                        connection.close()

                for path, content_type, content in (("/", "text/html", "Choose what to clear."),
                        ("/review.css", "text/css", "card-grid"), ("/review.js", "text/javascript", "/api/draft")):
                    status, headers, text = request("GET", path)
                    self.assertEqual(status, 200)
                    self.assertIn(content_type, headers["Content-Type"])
                    self.assertIn(content, text)
                    self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    # No bound IDs or backup paths in unauthenticated page assets.
                    self.assertNotIn(fixture.assets[0].id, text)
                    self.assertNotIn(str(fixture.roots[0]), text)
                for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"},
                                {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer é"},
                                {"Authorization": None}):
                    with self.subTest(headers=headers):
                        self.assertEqual(request("GET", "/api/state", **headers)[0], 403)
                original_state = json.loads(request("GET", "/api/state")[2])
                self.assertEqual(original_state["count"], 3)
                self.assertEqual(original_state["confirmation"], confirmation(plan))
                for headers in ({"Host": "localhost:" + str(address.port)}, {"Origin": None},
                                {"Origin": "null"}, {"Origin": "https://evil.example"},
                                {"Authorization": "Bearer wrong"}):
                    with self.subTest(headers=headers):
                        self.assertEqual(request("POST", "/api/draft", {"revision": 0, "selected": []}, **headers)[0], 403)
                invalid = ('{"revision":0,"revision":0,"selected":[]}', '{"revision":NaN}', '[]',
                           '{"revision":true,"selected":[]}', '{"revision":0,"selected":[],"execute":true}',
                           "x" * (MAX_BODY + 1))
                for raw in invalid:
                    with self.subTest(body=raw[:60]):
                        self.assertEqual(request("POST", "/api/draft", raw)[0], 400)
                self.assertEqual(request("POST", "/api/draft", {"revision": 0, "selected": []},
                                         **{"Content-Type": "text/plain"})[0], 400)
                for path in ("/api/execute", "/api/../plan.json", "/state/plan.json"):
                    self.assertEqual(request("POST", path, {})[0], 404)
                    self.assertEqual(request("GET", path)[0], 404)
                self.assertEqual(session.state()["revision"], 0)
                selected = [fixture.assets[1].id]
                status, _, raw = request("POST", "/api/draft", {"revision": 0, "selected": selected})
                self.assertEqual(status, 200)
                draft = json.loads(raw)
                self.assertFalse(draft["saved"])
                self.assertIsNone(draft["confirmation"])
                self.assertEqual(request("POST", "/api/save", {"revision": 0})[0], 409)
                with self.assertRaisesRegex(Blocked, "unsaved"):
                    session.execute(original_state["confirmation"])
                status, _, raw = request("POST", "/api/save", {"revision": draft["revision"]})
                self.assertEqual(status, 200)
                saved = json.loads(raw)
                self.assertEqual(saved["selected"], selected)
                self.assertNotEqual(saved["confirmation"], original_state["confirmation"])
                self.assertEqual(provider.calls, [])  # Saving in the page never executes.
                self.assertEqual(session.execute(saved["confirmation"])["simulated_count"], 1)
                self.assertEqual(provider.calls, selected)
                self.assertEqual(set(provider.active), {a.id for a in fixture.assets} - set(selected))
                self.assertEqual(request("POST", "/api/draft", {"revision": saved["revision"], "selected": []})[0], 409)
            session.close()


if __name__ == "__main__":
    unittest.main()
