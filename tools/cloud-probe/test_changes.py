"""Exercise bounded change feeds through the pinned session, with sockets forbidden."""

from contextlib import contextmanager, nullcontext, redirect_stdout
import copy
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from requests import Response
from requests.adapters import HTTPAdapter
from pyicloud_ipd.session import PyiCloudSession

import changes
import probe
from changes import ChangesBlocked, ChangesThrottled, canonical, collect_changes, request_body, select_zone


ROOT = "https://p123-ckdatabasews.icloud.com"
URL = ROOT + "/database/1/com.apple.photos.cloud/production/private/changes/zone"
ZONE = {"zoneName": "synthetic-zone", "ownerRecordName": "synthetic-owner", "identityVersion": 1}
ACCOUNT = "synthetic-private-account"


def record(identity="synthetic-asset", tag="v1", kind="CPLAsset"):
    return {"recordName": identity, "recordType": kind, "recordChangeTag": tag}


def page(token="synthetic-token-1", records=(), more=False):
    return {"zones": [{"zoneID": copy.deepcopy(ZONE), "syncToken": token,
                       "moreComing": more, "records": list(records)}]}


class ChangesTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        # Ordinary fixtures need no real waiting; the pacing test uses a clock.
        pacing = patch.object(changes, "MIN_REQUEST_INTERVAL", 0)
        pacing.start()
        self.addCleanup(pacing.stop)

    @contextmanager
    def transport(self, values, after_send=None, guarded=True):
        values, calls, responses = iter(values), [], []
        # The actual pinned session must not eagerly parse the streamed JSON.
        with PyiCloudSession(SimpleNamespace()) as session:
            session.request = Mock(side_effect=AssertionError("Provider would prebuffer JSON"))
            session.headers["X-Synthetic-Session"] = "authenticated-fixture"
            session.cookies.set("synthetic-session", "fixture", domain="p123-ckdatabasews.icloud.com")
            service = SimpleNamespace(session=session, params={"dsid": ACCOUNT},
                                      data={"dsInfo": {"dsid": ACCOUNT}},
                                      requires_2fa=False, requires_2sa=False,
                                      _get_webservice_url=lambda name: ROOT)

            def send(adapter, request, **kwargs):
                calls.append(request)
                self.assertTrue(kwargs["stream"])
                self.assertEqual(kwargs["timeout"], (10, 30))
                self.assertEqual(request.headers["X-Synthetic-Session"], "authenticated-fixture")
                self.assertIn("synthetic-session=fixture", request.headers["Cookie"])
                value = next(values)
                if isinstance(value, BaseException):
                    raise value
                headers = {}
                if isinstance(value, tuple):
                    status, value, *extra = value
                    headers = extra[0] if extra else {}
                else:
                    status = 200
                response = Response()
                response.status_code = status
                response.headers["Content-Type"] = "application/json"
                response.headers["Location"] = URL
                response.headers.update(headers)
                response.raw = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
                response.request, response.url = request, request.url
                response.close = Mock(wraps=response.close)
                responses.append(response)
                if after_send:
                    after_send(service, len(calls))
                return response

            guard = probe.request_guard(True, changes=True) if guarded else nullcontext({})
            with patch.object(HTTPAdapter, "send", send), guard as state:
                state["photo_root"] = ROOT
                yield service, state, calls, responses
            session.request.assert_not_called()

    def capture(self, service, state, progress):
        return collect_changes(service, ROOT, ZONE, state, progress, expected_account=ACCOUNT)

    def test_pages_preserve_change_events(self):
        first = record()
        first["fields"] = {"captionEnc": "SECRET_CAPTION", "resOriginalRes": {
            "downloadURL": "https://SECRET_URL", "wrappingKey": "SECRET_KEY"}}
        values = [page("t1", [first], True), page("t2", [record("master", kind="CPLMaster")]),
                  page("t3", [record(tag="v2"), record("new-old-capture-upload"),
                              {"recordName": "removed-record", "deleted": True},
                              record("future-record", kind="CPLFutureType")], True), page("t4")]
        progress = []
        with self.transport(values) as (service, state, calls, responses):
            private, summary = self.capture(service, state,
                                               lambda n, p: progress.append(p))
            self.assertEqual(state["change_metadata_requests"], 4)
            self.assertIsNone(state["change_request"])
        self.assertEqual([json.loads(c.body)["zones"][0].get("syncToken") for c in calls],
                         [None, "t1", "t2", "t3"])
        self.assertTrue(all("/private/changes/zone?" in c.url for c in calls))
        self.assertEqual(private["final_sync_token"], "t4")
        self.assertEqual(summary["delta_records_observed"], 4)
        self.assertEqual(summary["change_feed_records"], 6)
        self.assertEqual([p["phase"] for p in progress], ["initial", "initial", "delta", "delta"])
        self.assertTrue(all(p["complete"] is False for p in progress))
        self.assertEqual(progress[2]["records"][0]["recordChangeTag"], "v2")
        self.assertEqual(progress[2]["records"][2], {"recordName": "removed-record", "deleted": True})
        self.assertNotIn("SECRET", json.dumps(progress))
        for key in ("inventory_generation_guarantee", "complete_resource_inventory", "atomic_mutation_guard"):
            self.assertEqual(summary[key], "NOT VERIFIED")
        for response in responses:
            response.close.assert_called_once()

    def test_empty_delta_accepts_stable_token(self):
        with self.transport([page(records=[record()]), page()]) as (service, state, calls, _):
            _, summary = self.capture(service, state, lambda n, p: None)
            self.assertTrue(summary["change_feed_completed"])
            self.assertEqual(summary["delta_records_observed"], 0)
            self.assertEqual(len(calls), 2)

    def test_repeated_tokens_stop_pagination(self):
        scenarios = [[page("t1", more=True), page("t1", more=True)],
                     [page("t1", more=True), page("t1", [record()])],
                     [page("t1", more=True), page("t2", more=True), page("t1")],
                     [page("t1", more=True), page("t2"), page("t1")]]
        for values in scenarios:
            with self.subTest(values=values), self.transport(values) as data:
                service, state, calls, _ = data
                with self.assertRaisesRegex(ChangesBlocked, "repeated a token"):
                    self.capture(service, state, lambda n, p: None)
                self.assertEqual(len(calls), len(values))
                self.assertIsNone(state["change_request"])

    def test_account_drift_blocks_next_request(self):
        for when in ("before", "between"):
            with self.subTest(when=when), self.transport([page(more=True)]) as data:
                service, state, calls, _ = data
                def change_account():
                    service.params["dsid"] = "different-account"
                    service.data["dsInfo"]["dsid"] = "different-account"
                if when == "before":
                    change_account()
                with self.assertRaisesRegex(ChangesBlocked, "Authenticated account changed"):
                    self.capture(service, state, lambda n, p: change_account())
                self.assertEqual(len(calls), int(when == "between"))
                self.assertIsNone(state["change_request"])

    def test_incomplete_responses_stop_capture(self):
        expired = {"zones": [{"zoneID": ZONE, "serverErrorCode": "CHANGE_TOKEN_EXPIRED",
                               "reason": "SECRET_DETAIL"}]}
        invalid = [expired, {"serverErrorCode": "SECRET_ERROR"}, {"zones": []},
                   {"zones": [page()["zones"][0], page()["zones"][0]]},
                   b'{"zones":[],"zones":[]}', b'{"zones":NaN}', b'\xff', b'{', (302, b'')]
        for key, value in (("zoneID", {**ZONE, "ownerRecordName": "different-owner"}),
                           ("zoneID", {**ZONE, "identityVersion": True}),
                           ("syncToken", ""), ("syncToken", 1), ("moreComing", 0),
                           ("records", None), ("records", [record()] * 101)):
            response = page()
            response["zones"][0][key] = value
            invalid.append(response)
        missing = page()
        del missing["zones"][0]["moreComing"]
        invalid.append(missing)
        for value in invalid:
            with self.subTest(value=value), self.transport([value]) as (service, state, calls, responses):
                with self.assertRaises(ChangesBlocked) as caught:
                    self.capture(service, state, lambda n, p: self.fail("Unexpected progress"))
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(len(calls), 1)
                self.assertIsNone(state["change_request"])
                responses[0].close.assert_called_once()

    def test_invalid_records_stop_capture(self):
        invalid = [None, {"serverErrorCode": "SECRET"}, {"deleted": True},
                   {"recordName": "tombstone", "deleted": 1}, record(tag=""),
                   record(kind="https://SECRET"), {**record(), "zoneID": {"zoneName": "other"}}]
        missing = record()
        del missing["recordChangeTag"]
        invalid.append(missing)
        for value in invalid:
            with self.subTest(value=value), self.transport([page(records=[value])]) as data:
                service, state, _, _ = data
                with self.assertRaises(ChangesBlocked):
                    self.capture(service, state, lambda n, p: self.fail("Unexpected progress"))

    def test_collection_budgets_stop_capture(self):
        for setting, bound, message in (("MAX_PAGES", 1, "collection bound"),
                                        ("MAX_RECORDS", 1, "collection bound"),
                                        ("MAX_PAGE_BYTES", 2, "byte bound"),
                                        ("MAX_TOTAL_BYTES", 2, "byte bound")):
            with self.subTest(setting=setting), patch.object(changes, setting, bound), \
                    self.transport([page(records=[record()])]) as (service, state, calls, responses):
                with self.assertRaisesRegex(ChangesBlocked, message):
                    self.capture(service, state, lambda n, p: None)
                self.assertEqual(len(calls), 1)
                responses[0].close.assert_called_once()
        with patch("changes.time.monotonic", return_value=0) as clock, \
                self.transport([page()], after_send=lambda s, n: setattr(clock, "return_value", 121)) as data:
            service, state, _, responses = data
            with self.assertRaisesRegex(ChangesBlocked, "time bound"):
                self.capture(service, state, lambda n, p: None)
            responses[0].close.assert_called_once()

    def test_requests_are_paced(self):
        for latency in (0.1, 1.5):
            now, starts, sleeps = [0.0], [], []
            def sent(service, count):
                starts.append(now[0])
                now[0] += latency
            def sleep(seconds):
                sleeps.append(seconds)
                now[0] += seconds
            values = [page("t1", more=True), page("t2", more=True), page("t3"), page("t3")]
            with self.subTest(latency=latency), patch.object(changes, "MIN_REQUEST_INTERVAL", 1), \
                    patch("changes.time.monotonic", side_effect=lambda: now[0]), \
                    patch("changes.time.sleep", side_effect=sleep), self.transport(values, sent) as data:
                service, state, _, _ = data
                self.capture(service, state, lambda n, p: None)
            self.assertEqual(starts[0], 0)
            for start, previous in zip(starts[1:], starts):
                self.assertAlmostEqual(start - previous, max(1, latency))
            self.assertEqual(len(sleeps), 3 if latency < 1 else 0)
        # Pacing must not extend the overall budget or send a request early.
        now = [0.0]
        with patch.object(changes, "MIN_REQUEST_INTERVAL", 1), patch.object(changes, "MAX_SECONDS", 0.5), \
                patch("changes.time.monotonic", side_effect=lambda: now[0]), \
                patch("changes.time.sleep") as sleep, self.transport([page()]) as data:
            service, state, calls, _ = data
            with self.assertRaisesRegex(ChangesBlocked, "time bound"):
                self.capture(service, state, lambda n, p: None)
            self.assertEqual(len(calls), 1)
            sleep.assert_not_called()

    def test_throttling_stops_without_retries(self):
        cases = [(429, b'not-json', {"Retry-After": "7"}, 7),
                 (503, {"retryAfter": 4.2}, {"Retry-After": "2"}, 5),
                 (429, b'', {"Retry-After": "Thu, 01 Jan 1970 00:00:12 GMT"}, 12),
                 (200, {"serverErrorCode": "THROTTLED", "retryAfter": 10}, {}, 10),
                 (200, {"zones": [{"zoneID": ZONE, "serverErrorCode": "THROTTLED",
                                  "retryAfter": 9}]}, {}, 9),
                 (200, page(records=[{"serverErrorCode": "THROTTLED", "retryAfter": 6}]), {}, 6),
                 (429, b'', {}, None),
                 (503, {"retryAfter": -1}, {"Retry-After": "SECRET_INVALID"}, None),
                 (200, {"serverErrorCode": "THROTTLED", "retryAfter": True}, {}, None)]
        for status, body, headers, expected in cases:
            with self.subTest(status=status, expected=expected), patch("changes.time.time", return_value=0), \
                    self.transport([(status, body, headers), page()]) as data:
                service, state, calls, responses = data
                with self.assertRaises(ChangesThrottled) as caught:
                    self.capture(service, state, lambda n, p: self.fail("Unexpected progress"))
                self.assertEqual(caught.exception.retry_after_seconds, expected)
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(len(calls), 1)
                self.assertIsNone(state["change_request"])
                responses[0].close.assert_called_once()

    def test_interruption_retains_partial_progress(self):
        for error in (TimeoutError("SECRET_TIMEOUT"), KeyboardInterrupt()):
            progress = []
            with self.subTest(error=type(error)), self.transport([page(more=True), error]) as data:
                service, state, calls, _ = data
                with self.assertRaises(type(error)):
                    self.capture(service, state, lambda n, p: progress.append(p))
                self.assertEqual(len(progress), 1)
                self.assertFalse(progress[0]["complete"])
                self.assertEqual(len(calls), 2)
                self.assertIsNone(state["change_request"])

    def test_zone_selection_requires_full_identity(self):
        item = {"zone_id": ZONE, "deleted": False}
        result = select_zone({"private": [item]}, ZONE["zoneName"], ZONE["ownerRecordName"])
        self.assertEqual(result, ZONE)
        self.assertIsNot(result, ZONE)
        for zones in ({"shared": [item]}, {"private": [item, item]},
                      {"private": [{**item, "deleted": True}]},
                      {"private": [{"zone_id": {"zoneName": ZONE["zoneName"]}}]}):
            with self.subTest(zones=zones), self.assertRaises(ChangesBlocked):
                select_zone(zones, ZONE["zoneName"], ZONE["ownerRecordName"])
        with self.assertRaises(ChangesBlocked):
            request_body({**ZONE, "zoneName": "_defaultZone"}, None)

    def test_transport_blocks_scope_expansion(self):
        from requests import Request, Session

        with self.transport([page()]) as (service, state, calls, _):
            state["approved_zone"] = ZONE
            body = canonical(request_body(ZONE, None))
            state["change_request"] = body
            variants = [(URL.replace("private", "shared"), body),
                        (URL.replace("changes/zone", "records/modify"), body),
                        (URL.replace("changes/zone", "records/query"), body),
                        (URL.replace("https:", "http:"), body),
                        ("https://evil.example/changes/zone", body)]
            for mutate in (lambda b: b.update(desiredKeys=["captionEnc"]),
                           lambda b: b.update(desiredRecordTypes=["CPLAsset"]),
                           lambda b: b["zones"].append({"zoneID": ZONE}),
                           lambda b: b["zones"][0].update(syncToken="unapproved-token"),
                           lambda b: b["zones"][0].update(zoneID={**ZONE, "identityVersion": True})):
                value = request_body(ZONE, None)
                mutate(value)
                variants.append((URL, canonical(value)))
            for url, raw in variants:
                # Exercise the schema guard even if an upstream caller approved
                # a malformed body. A forged token instead tests the pending-body
                # binding, since opaque token contents have no intrinsic meaning.
                state["change_request"] = (body if json.loads(raw)["zones"][0].get("syncToken") else raw)
                request = Request("POST", url, data=raw).prepare()
                with self.subTest(url=url, raw=raw), self.assertRaises(probe.ProbeBlocked):
                    service.session.get_adapter(url).send(request, verify=True)
            for method, url in (("GET", URL), ("GET", "https://p123-content.icloud.com/media"),
                                ("DELETE", URL.replace("changes/zone", "records/modify"))):
                with self.subTest(method=method), self.assertRaises(probe.ProbeBlocked):
                    service.session.get_adapter(url).send(Request(method, url).prepare(), verify=True)
            request = Request("POST", URL, data=body).prepare()
            adapter = service.session.get_adapter(URL)
            state["change_request"] = body
            with self.assertRaises(probe.ProbeBlocked):
                adapter.send(request, verify=False)
            # A retry-capable adapter must be refused before its first send.
            with patch.object(adapter.max_retries, "total", 1), self.assertRaises(probe.ProbeBlocked):
                adapter.send(request, verify=True)
            self.assertEqual(calls, [])
            state["change_request"] = body
            response = Session.request(service.session, "POST", URL, data=body, stream=True,
                                       allow_redirects=False, timeout=(10, 30))
            response.close()
            with self.assertRaises(probe.ProbeBlocked):
                adapter.send(response.request, verify=True)
            self.assertEqual(len(calls), 1)
        with self.transport([]) as (service, state, _, _), probe.request_guard(True):
            state["approved_zone"], state["change_request"] = ZONE, body
            with self.assertRaises(probe.ProbeBlocked):
                Session.request(service.session, "POST", URL, data=body, stream=True)

    def test_cli_retains_honest_private_results(self):
        for outcome in ("complete", "expired", "account-drift", "interrupted", "throttled"):
            first = page(records=[record()], more=outcome != "complete")
            tail = ({"zones": [{"zoneID": ZONE, "serverErrorCode": "CHANGE_TOKEN_EXPIRED",
                                "reason": "SECRET_REASON"}]} if outcome == "expired" else
                    KeyboardInterrupt() if outcome == "interrupted" else
                    (503, {"serverErrorCode": "THROTTLED", "retryAfter": 30, "reason": "SECRET_REASON"})
                    if outcome == "throttled" else page("final-token"))

            def after_send(service, count):
                if outcome == "account-drift" and count == 2:
                    service.data["dsInfo"]["dsid"] = "different-private-account"

            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory(
                    dir=Path(tempfile.gettempdir()).resolve()) as directory, \
                    self.transport([first, tail], after_send, guarded=False) as data:
                service, _, calls, responses = data
                run, output = Path(directory), io.StringIO()
                with patch.dict(sys.modules, {"pyicloud_ipd": SimpleNamespace(
                        PyiCloudService=lambda *a, **kw: service)}), \
                        patch.object(sys, "argv", ["probe.py", "changes", "--zone-name", ZONE["zoneName"],
                                                   "--zone-owner", ZONE["ownerRecordName"]]), \
                        patch("probe.version", return_value=probe.PIN), \
                        patch("probe.sys.stdin.isatty", return_value=True), \
                        patch("probe.sys.stderr.isatty", return_value=True), \
                        patch("builtins.input", return_value="READ ONLY CHANGES"), \
                        patch("probe.getpass.getpass", return_value="fake@example.invalid"), \
                        patch("probe.new_run", return_value=run), patch("probe.logging.disable"), \
                        patch("probe.discover_zones", return_value=({
                            "private": [{"zone_id": ZONE, "deleted": False}], "shared": []}, [])), \
                        redirect_stdout(output):
                    self.assertEqual(probe.main(), int(outcome != "complete"))
                summary = json.loads((run / "summary.json").read_text())
                self.assertEqual(summary["change_feed_completed"], outcome == "complete")
                self.assertEqual(summary["request_counts"]["change_metadata_requests"], 2)
                self.assertEqual(summary["request_counts"]["original_download_requests"], 0)
                self.assertEqual(summary["cloud_coverage"], "NOT VERIFIED")
                self.assertEqual(summary["removal_feasibility"], "NOT VERIFIED")
                if outcome == "throttled":
                    self.assertTrue(summary["rate_limited"])
                    self.assertEqual(summary["retry_after_seconds"], 30)
                context = json.loads((run / "changes-context-private.json").read_text())
                self.assertEqual(context["account_id"], ACCOUNT)
                self.assertEqual(context["zone_id"], ZONE)
                self.assertFalse(context["complete"])
                self.assertTrue((run / "changes-page-1-private.json").is_file())
                self.assertEqual((run / "changes-private.json").exists(), outcome == "complete")
                if outcome == "complete":
                    private = json.loads((run / "changes-private.json").read_text())
                    self.assertEqual(private["account_id"], ACCOUNT)
                    self.assertEqual(private["final_sync_token"], "final-token")
                for secret in (ACCOUNT, ZONE["zoneName"], ZONE["ownerRecordName"], "synthetic-token",
                               "synthetic-asset", "SECRET", "fake@example.invalid"):
                    self.assertNotIn(secret, output.getvalue())
                self.assertEqual(len(calls), 2)
                for response in responses:
                    response.close.assert_called_once()

    def test_cli_requires_scope_confirmation(self):
        for args in (["changes"], ["changes", "--zone-name", "fixture"],
                     ["libraries", "--zone-owner", "fixture"],
                     ["libraries", "--continue-from", "cloud-probe-example"],
                     ["libraries", "--since-run", "cloud-probe-example"],
                     ["changes", "--since-run", "cloud-probe-example", "--continue-from", "cloud-probe-other"],
                     ["changes", "--since-run", "cloud-probe-example", "--zone-owner", "fixture"],
                     ["changes", "--continue-from", "cloud-probe-example", "--zone-name", "fixture"],
                     ["changes", "--zone-name", "fixture", "--zone-owner", "owner", "--album", "album"]):
            with self.subTest(args=args), patch.object(sys, "argv", ["probe.py", *args]), \
                    patch("probe.new_run") as create, patch("probe.sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    probe.main()
                self.assertEqual(caught.exception.code, 2)
                create.assert_not_called()
        with patch.object(sys, "argv", ["probe.py", "changes", "--zone-name", "fixture",
                                        "--zone-owner", "owner"]), \
                patch("probe.sys.stdin.isatty", return_value=True), \
                patch("probe.sys.stderr.isatty", return_value=True), \
                patch("probe.version", return_value=probe.PIN), \
                patch("builtins.input", return_value="READ ONLY ALBUM"), \
                patch("probe.new_run") as create, patch("probe.getpass.getpass") as credentials, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(probe.main(), 2)
            create.assert_not_called()
            credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
