"""Exercise the isolated Favorite experiment with a fake Photos wire, never Apple."""

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from pyicloud_ipd.session import PyiCloudSession
from requests import Response, Timeout
from requests.adapters import HTTPAdapter

import conflict
from album import safe_record
from changes import ChangesBlocked, ChangesThrottled
from probe import PIN, save_new
import test_album as fixtures


ROOT, ZONE, NAME = fixtures.ROOT, fixtures.ZONE, fixtures.NAME
TARGET, MASTER = "full-asset-5", "full-master-5"


class ConflictTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        pacing = patch.object(conflict, "INTERVAL", 0)
        pacing.start()
        self.addCleanup(pacing.stop)
        temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve()
        self.baseline_name = "cloud-probe-synthetic"
        directory = self.repo / ".smoke" / self.baseline_name
        directory.mkdir(parents=True)
        self.records = [record for number in range(1, 6) for record in fixtures.pair(str(number))]
        for record in self.records:
            if record["recordType"] == "CPLMaster":
                record["fields"]["resOriginalFileType"] = {"value": "public.jpeg"}
        observation = {"assets": [], "masters": [], "relations": []}
        for record in self.records:
            observation["assets" if record["recordType"] == "CPLAsset" else "masters"].append(safe_record(record))
        self.baseline = {"account_id": "synthetic-account", "database": "private", "zone_id": ZONE,
                         "album_id": fixtures.ALBUM, "passes": [observation, copy.deepcopy(observation)]}
        self.write(directory / "album-private.json", self.baseline)
        self.write(directory / "summary.json", {"schema": 1, "provider_version": PIN, "scope": "album",
                   "phase": "completed", "album_metadata_completed": True, "two_passes_match": True,
                   "observed_assets": 5, "account_binding_recorded": True})
        self.evidence_path = self.repo / ".smoke/evidence-private.json"
        self.evidence = {"schema": 1, "experiment": "new_upload_old_capture_date",
                         "new_id_and_tag_match_delta": True, "saved_baseline_unchanged": True,
                         "account_id": "synthetic-account", "database": "private", "zone_id": ZONE,
                         "album_id": fixtures.ALBUM, "new_asset_id": TARGET, "new_master_id": MASTER,
                         "fixture": {"file": "iarchive-test-" + "a" * 32 + ".jpg", "sha256": "0" * 64,
                                     "capture_unix_ms": 978404645000},
                         "captures_sha256": {self.baseline_name: {
                             path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()}}}
        self.write(self.evidence_path, self.evidence)
        self.binding, _ = conflict.load_experiment(self.repo, self.evidence_path, self.baseline_name)

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def raw_target(self, tag="asset-tag", favorite=0):
        record = copy.deepcopy(next(r for r in self.records if r["recordName"] == TARGET))
        record["recordChangeTag"] = tag
        record["fields"]["isFavorite"] = {"value": favorite}
        return record

    def frozen_wire(self, session):
        service = SimpleNamespace(session=session, data={"dsInfo": {"dsid": "synthetic-account"}},
                                  params={"dsid": "synthetic-account"})
        wire = conflict.Exchange(service, ROOT, self.binding, conflict.Pacer())
        before = conflict.favorite_record({"records": [self.raw_target(favorite=1)]}, self.binding)
        changed = conflict.favorite_record({"records": [self.raw_target("toggled-tag", 1)]}, self.binding)
        wire.freeze_update(before, changed)
        return wire

    def response(self, request, value, status=200, headers=None):
        response = Response()
        response.status_code = status
        response.headers.update({"Content-Type": "application/json", **(headers or {})})
        response.raw = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
        response.request, response.url = request, request.url
        return response

    def test_prepared_mutations_are_blocked(self):
        def changed_body(request, field):
            body = json.loads(request.body)
            record = body["operations"][0]["record"]
            if field == "operation":
                body["operations"][0]["operationType"] = "forceUpdate"
            elif field == "asset":
                record["recordName"] = "another-asset"
            elif field == "tag":
                record["recordChangeTag"] = "fresh-tag"
            elif field == "zone":
                body["zoneID"] = {**ZONE, "ownerRecordName": "another-owner"}
            elif field == "field":
                record["fields"] = {"isDeleted": {"value": 1}}
            else:
                body["operations"].append(copy.deepcopy(body["operations"][0]))
            request.body = json.dumps(body)

        for change in ("operation", "asset", "tag", "zone", "field", "extra", "host", "redirect", "method",
                       "query_flag", "query_missing", "query_extra"):
            with self.subTest(change=change), PyiCloudSession(SimpleNamespace()) as session, \
                    patch.object(HTTPAdapter, "send") as send:
                wire = self.frozen_wire(session)
                prepare = session.prepare_request

                def mutate(request):
                    prepared = prepare(request)
                    if change in ("host", "redirect"):
                        prepared.url = ("https://evil.example/records/modify" if change == "host"
                                        else prepared.url.replace("records/modify", "records/delete"))
                    elif change == "method":
                        prepared.method = "DELETE"
                    elif change.startswith("query_"):
                        parsed = urlsplit(prepared.url)
                        query = parse_qs(parsed.query)
                        if change == "query_flag":
                            query["remapEnums"] = ["False"]
                        elif change == "query_missing":
                            query.pop("getCurrentSyncToken")
                        else:
                            query["unexpected"] = ["True"]
                        prepared.url = parsed._replace(query=urlencode(query, doseq=True)).geturl()
                    else:
                        changed_body(prepared, change)
                    return prepared

                with patch.object(session, "prepare_request", side_effect=mutate), \
                        self.assertRaisesRegex(conflict.ConflictBlocked, "Prepared request changed"):
                    wire.send("modify")
                send.assert_not_called()
                self.assertEqual(wire.writes, 0)

    def test_transport_limits_prevent_retries(self):
        for case in ("timeout", "redirect", "oversized", "malformed", "throttle", "retry_after", "retries", "account"):
            with self.subTest(case=case), PyiCloudSession(SimpleNamespace()) as session:
                wire = self.frozen_wire(session)
                session.request = Mock(side_effect=AssertionError("Upstream request/retry forbidden"))
                if case == "account":
                    wire.service.data["dsInfo"]["dsid"] = "other-account"
                    wire.service.params["dsid"] = "other-account"
                if case == "retries":
                    session.get_adapter(ROOT).max_retries.total = 1

                def send(adapter, request, **kwargs):
                    self.assertIs(kwargs["verify"], True)
                    self.assertTrue(kwargs["stream"])
                    if case == "timeout":
                        raise Timeout("SECRET_ERROR")
                    value, status, headers = {"records": []}, 200, {}
                    if case == "redirect":
                        status = 302
                    elif case == "oversized":
                        value = b"x" * (conflict.MAX_RESPONSE + 1)
                    elif case == "malformed":
                        value = b'{"records":[],"records":[]}'
                    elif case in ("throttle", "retry_after"):
                        status = 429 if case == "throttle" else 200
                        headers = {"Retry-After": "17"}
                    return self.response(request, value, status, headers)

                with patch.object(HTTPAdapter, "send", autospec=True, side_effect=send) as sent:
                    errors = {"timeout": (Timeout, "SECRET_ERROR"),
                              "redirect": (conflict.ConflictBlocked, "Redirect refused"),
                              "oversized": (conflict.ConflictBlocked, "response exceeded"),
                              "malformed": (ChangesBlocked, "duplicate keys"),
                              "throttle": (ChangesThrottled, "requested a pause"),
                              "retry_after": (ChangesThrottled, "requested a pause"),
                              "retries": (conflict.ConflictBlocked, "retries must be disabled"),
                              "account": (conflict.ConflictBlocked, "Authenticated account changed")}
                    with self.assertRaisesRegex(*errors[case]):
                        wire.send("modify")
                    self.assertEqual(sent.call_count, int(case not in ("account", "retries")))
                    if wire.writes:
                        with self.assertRaisesRegex(conflict.ConflictBlocked, "budget exhausted"):
                            wire.send("modify")
                        self.assertEqual(sent.call_count, 1)
                session.request.assert_not_called()

    def test_lookup_and_reply_require_identity(self):
        for change in ("asset", "zone", "type", "favorite", "deleted"):
            record = self.raw_target("toggled-tag", 1)
            if change == "asset":
                record["recordName"] = "other-asset"
            elif change == "zone":
                record["zoneID"] = {**ZONE, "ownerRecordName": "other-owner"}
            elif change == "type":
                record["recordType"] = "CPLMaster"
            elif change == "favorite":
                record["fields"]["isFavorite"]["value"] = True
            else:
                record["deleted"] = True
            with self.subTest(change=change):
                with self.assertRaises(conflict.ConflictBlocked):
                    conflict.favorite_record({"records": [record]}, self.binding)
                self.assertEqual(conflict.classify_reply(200, {"records": [record]}, self.binding, 1), "UNKNOWN")
        exact = {"recordName": TARGET, "serverErrorCode": "CONFLICT"}
        for value, expected in (({"records": [exact]}, "CONFLICT"),
                                ({"records": [exact, exact]}, "UNKNOWN"),
                                ({"records": [{**exact, "recordName": "other"}]}, "UNKNOWN"),
                                ({"records": [{**exact, "recordType": "CPLMaster"}]}, "UNKNOWN"),
                                ({"records": [{**exact, "zoneID": {**ZONE, "ownerRecordName": "other"}}]}, "UNKNOWN"),
                                ({"serverErrorCode": "CONFLICT"}, "UNKNOWN")):
            with self.subTest(value=value):
                self.assertEqual(conflict.classify_reply(200, value, self.binding, 0), expected)

    def run_cli(self, scenario, *, metadata_only=False):
        run = self.repo / ".smoke" / ("cloud-probe-" + scenario.replace("_", "") + ("metadata" if metadata_only else ""))
        run.mkdir()
        (run / "cookies").mkdir()
        state = {"favorite": 0 if scenario == "favorite_off" else 1,
                 "tag": "asset-tag", "calls": [], "writes": 0, "prompts": []}
        service = SimpleNamespace(data={"dsInfo": {"dsid": "synthetic-account"}},
            params={"dsid": "synthetic-account"}, requires_2fa=False, requires_2sa=False,
            password_filter=None, http_timeout=30, session_data={},
            session_path=str(run / "fake-session.json"), cookiejar_path="unused",
            _get_webservice_url=lambda _: ROOT)
        if scenario == "wrong_account":
            service.data["dsInfo"]["dsid"] = "wrong-account"
            service.params["dsid"] = "wrong-account"

        def answer(prompt):
            state["prompts"].append(prompt)
            if "authenticate:" in prompt:
                return "WRONG" if scenario == "scope_refused" else (
                    "READ ONLY FAVORITE" if metadata_only else "FAVORITE CONFLICT TEST")
            if "CYCLED:" in prompt:
                state.update(favorite=0 if scenario == "cycle_ends_off" else 1,
                             tag="cycled-tag" if scenario != "unchanged_tag" else "asset-tag")
                if scenario == "resource_drift":
                    master = next(r for r in self.records if r["recordName"] == MASTER)
                    master["fields"]["resOriginalRes"]["value"]["fileChecksum"] = "changed-checksum"
                return "CYCLED"
            self.assertIn(conflict.CONFIRMATION, prompt)
            return "NO" if scenario == "write_refused" else conflict.CONFIRMATION

        def send(adapter, request, **kwargs):
            path, body = urlsplit(request.url).path, json.loads(request.body)
            if path.endswith(("/lookup", "/modify")):
                query = parse_qs(urlsplit(request.url).query)
                self.assertEqual(query["remapEnums"], ["True"])
                self.assertEqual(query["getCurrentSyncToken"], ["True"])
            state["calls"].append(path)
            if path.endswith("/zones/list"):
                if scenario == "zones_throttled":
                    return self.response(request, {"zones": [], "retryAfter": 17}, 429, {"Retry-After": "5"})
                return self.response(request, {"zones": [{"zoneID": ZONE}]} if "/private/" in path else {"zones": []})
            if path.endswith("/query/batch"):
                if scenario == "album_throttled":
                    return self.response(request, {**fixtures.count_response(5), "retryAfter": 17})
                return self.response(request, fixtures.count_response(5))
            if path.endswith("/query"):
                if body["query"]["recordType"] == "CPLAlbumByPositionLive":
                    return self.response(request, {"records": [fixtures.album_record()]})
                rank = next(f["fieldValue"]["value"] for f in body["query"]["filterBy"] if f["fieldName"] == "startRank")
                records = [self.raw_target(state["tag"], state["favorite"]) if r["recordName"] == TARGET
                           else copy.deepcopy(r) for r in self.records] if rank == 0 else []
                return self.response(request, {"records": records})
            if path.endswith("/lookup"):
                if scenario == "lookup_throttled":
                    return self.response(request, {"serverErrorCode": "THROTTLED", "retryAfter": 17})
                record = self.raw_target(state["tag"], state["favorite"])
                if (scenario == "missing_favorite"
                        or (scenario == "accepted_missing" and state["writes"])
                        or (scenario == "cycle_ends_missing" and len(state["prompts"]) > 1)):
                    record["fields"].pop("isFavorite")
                elif scenario == "string_favorite":
                    record["fields"]["isFavorite"]["value"] = "SECRET_VALUE"
                elif scenario == "missing_master":
                    record["fields"].pop("masterRef")
                return self.response(request, {"records": [record]})
            self.assertTrue(path.endswith("/records/modify"))
            self.assertTrue((run / "write-intent-private.json").is_file())
            self.assertEqual(body["operations"], [{"operationType": "update", "record": {
                "recordName": TARGET, "recordType": "CPLAsset", "recordChangeTag": "asset-tag",
                "fields": {"isFavorite": {"value": 0}}}}])
            state["writes"] += 1
            if scenario == "write_throttled":
                return self.response(request, {}, 429, {"Retry-After": "17"})
            if scenario in ("accepted", "lost_after", "accepted_missing"):
                state.update(favorite=0, tag="accepted-tag")
            if scenario in ("lost_before", "lost_after"):
                raise Timeout("SECRET_ERROR")
            record = ({"recordName": TARGET, "recordType": "CPLAsset", "recordChangeTag": state["tag"],
                       "fields": {"isFavorite": {"value": state["favorite"]}}} if scenario in ("accepted", "accepted_missing") else
                      {"recordName": TARGET, "serverErrorCode": "CONFLICT", "reason": "SECRET_REASON"})
            if scenario == "accepted_missing":
                record["fields"].pop("isFavorite")
            if scenario == "post_drift":
                state.update(favorite=0, tag="changed-after-conflict")
            return self.response(request, {"records": [record]})

        def persist(path, value):
            if scenario == "intent_failure" and path.name == "write-intent-private.json":
                raise OSError("Synthetic intent failure")
            save_new(path, value)

        output = io.StringIO()
        with PyiCloudSession(service) as session, \
                patch("pyicloud_ipd.PyiCloudService", return_value=service) as authenticate, \
                patch.object(HTTPAdapter, "send", autospec=True, side_effect=send), \
                patch.object(conflict, "__file__", str(self.repo / "tools/cloud-probe/conflict.py")), \
                patch.object(conflict, "new_run", return_value=run), \
                patch.object(conflict, "save_new", side_effect=persist), \
                patch.object(conflict, "version", return_value=PIN), \
                patch.object(sys, "argv", ["conflict.py", "--evidence", str(self.evidence_path),
                                          "--baseline", self.baseline_name, "--album", NAME]
                                          + (["--metadata-only"] if metadata_only else [])), \
                patch.object(sys.stdin, "isatty", return_value=True), \
                patch.object(sys.stderr, "isatty", return_value=True), \
                patch("builtins.input", side_effect=answer), \
                patch("conflict.getpass.getpass", return_value="fake@example.invalid"), redirect_stdout(output):
            service.session = session
            session.trust_env = False
            session.cookies.save = Mock()
            result = conflict.main()
        summary_path = run / "summary.json"
        summary = (json.loads(summary_path.read_text()) if summary_path.exists()
                   else json.loads("{" + output.getvalue().split("\n{", 1)[1]))
        self.assertNotIn("SECRET", output.getvalue())
        self.assertNotIn("fake@example.invalid", output.getvalue())
        self.assertNotIn(TARGET, output.getvalue())
        for path in run.glob("*.json"):
            self.assertNotIn("SECRET", path.read_text())
        return result, summary, state, run, authenticate

    def test_trial_records_exact_postconditions(self):
        for scenario in ("conflict", "accepted", "accepted_missing", "lost_before", "lost_after", "write_throttled", "post_drift"):
            with self.subTest(scenario=scenario):
                result, summary, state, run, _ = self.run_cli(scenario)
                self.assertEqual(result, int(scenario != "conflict"), summary)
                self.assertEqual(summary["stale_update_rejected"], scenario == "conflict")
                self.assertEqual(state["writes"], 1)
                self.assertEqual(summary["modification_requests"], 1)
                self.assertTrue((run / "write-intent-private.json").exists())
                if scenario == "write_throttled":
                    self.assertEqual(summary["retry_after_seconds"], 17)
                    self.assertTrue(state["calls"][-1].endswith("/modify"))
                elif scenario == "accepted_missing":
                    self.assertEqual(summary["reply"], "UNKNOWN")
                    self.assertNotIn("post_check_completed", summary)
                    self.assertEqual(summary["lookup_requests"], 3)
                    diagnostics = json.loads((run / "favorite-after-diagnostics.json").read_text())
                    self.assertFalse(diagnostics["favorite_field_present"])
                else:
                    self.assertTrue(summary["post_check_completed"])
                    self.assertEqual(summary["lookup_requests"], 3)
                    self.assertEqual(summary["favorite_unchanged"], scenario not in ("accepted", "lost_after", "post_drift"))
                self.assertFalse(summary["removal_approved"])
                intent = json.loads((run / "write-intent-private.json").read_text())
                flags = {"remapEnums": True, "getCurrentSyncToken": True}
                self.assertEqual(intent["photo_query_flags"], flags)
                self.assertEqual(summary["photo_query_flags"], flags)
                outcome = json.loads((run / "write-outcome-private.json").read_text())
                self.assertEqual(outcome["photo_query_flags"], flags)
                self.assertEqual(intent["before"]["favorite"], 1)
                self.assertEqual(intent["cycled"]["favorite"], 1)
                self.assertNotEqual(intent["before"]["record"]["recordChangeTag"],
                                    intent["cycled"]["record"]["recordChangeTag"])

    def test_preflight_blocks_unapproved_changes(self):
        for scenario in ("scope_refused", "write_refused", "wrong_account", "unchanged_tag", "resource_drift",
                         "lookup_throttled", "zones_throttled", "album_throttled", "intent_failure",
                         "missing_favorite", "string_favorite", "missing_master", "favorite_off",
                         "cycle_ends_off", "cycle_ends_missing"):
            with self.subTest(scenario=scenario):
                # Each scenario starts from the same synthetic saved observation.
                self.records = [copy.deepcopy(r) for r in self.records]
                target_master = next(r for r in self.records if r["recordName"] == MASTER)
                target_master["fields"]["resOriginalRes"]["value"]["fileChecksum"] = "opaque-checksum"
                result, summary, state, run, authenticate = self.run_cli(scenario)
                self.assertEqual(result, 1)
                self.assertEqual(state["writes"], 0)
                self.assertFalse((run / "write-intent-private.json").exists())
                if scenario in ("scope_refused", "wrong_account"):
                    self.assertEqual(state["calls"], [])
                if scenario == "scope_refused":
                    authenticate.assert_not_called()
                if scenario == "lookup_throttled":
                    self.assertEqual(summary["retry_after_seconds"], 17)
                    self.assertTrue(state["calls"][-1].endswith("/lookup"))
                if scenario == "zones_throttled":
                    self.assertEqual(summary["retry_after_seconds"], 17)
                    self.assertEqual(len(state["calls"]), 1)
                if scenario == "album_throttled":
                    self.assertEqual(summary["retry_after_seconds"], 17)
                    self.assertTrue(state["calls"][-1].endswith("/query/batch"))
                if scenario in ("missing_favorite", "string_favorite", "missing_master"):
                    self.assertTrue((run / "favorite-before-diagnostics.json").exists())
                if scenario == "favorite_off":
                    self.assertIn("must be on before this trial", summary["finding"])
                    self.assertEqual(len(state["prompts"]), 1)
                if scenario == "cycle_ends_off":
                    self.assertIn("explicitly on at both captures", summary["finding"])
                    self.assertEqual(summary["lookup_requests"], 2)
                if scenario == "cycle_ends_missing":
                    self.assertIn("Favorite value or master binding is unknown", summary["finding"])
                    self.assertTrue((run / "favorite-cycled-diagnostics.json").exists())

    def test_metadata_mode_stops_after_diagnostics(self):
        for scenario, present, value_type, known, master in (
                ("valid", True, "int", True, True),
                ("missing_favorite", False, "NoneType", False, True),
                ("string_favorite", True, "str", False, True),
                ("missing_master", True, "int", True, False)):
            with self.subTest(scenario=scenario):
                result, summary, state, run, _ = self.run_cli(scenario, metadata_only=True)
                self.assertEqual(result, 0, summary)
                self.assertEqual(summary["scope"], "favorite_metadata")
                self.assertTrue(summary["metadata_diagnostics_completed"])
                self.assertEqual(summary["lookup_requests"], 1)
                self.assertEqual(summary["modification_requests"], 0)
                self.assertEqual(state["writes"], 0)
                self.assertEqual(len(state["prompts"]), 1)
                self.assertFalse((run / "write-intent-private.json").exists())
                self.assertFalse(summary["stale_update_rejected"])
                diagnostics = json.loads((run / "favorite-diagnostics.json").read_text())
                self.assertEqual(diagnostics, summary["diagnostics"])
                self.assertEqual(diagnostics["favorite_field_present"], present)
                self.assertEqual(diagnostics["favorite_value_type"], value_type)
                self.assertEqual(diagnostics["favorite_value_known"], known)
                self.assertEqual(diagnostics["master_reference_matches"], master)

    def test_metadata_wire_refuses_updates(self):
        with PyiCloudSession(SimpleNamespace()) as session, patch.object(HTTPAdapter, "send") as send:
            service = SimpleNamespace(session=session)
            wire = conflict.Exchange(service, ROOT, self.binding, conflict.Pacer(), metadata_only=True)
            with self.assertRaisesRegex(conflict.ConflictBlocked, "forbidden in metadata-only mode"):
                wire.freeze_update({}, {})
            wire.update_body = "{}"
            with self.assertRaisesRegex(conflict.ConflictBlocked, "forbidden in metadata-only mode"):
                wire.send("modify")
            send.assert_not_called()
            self.assertEqual(wire.writes, 0)

    def test_evidence_binding_rejects_drift(self):
        for field, value, message in (("account_id", "other", "scope differ"),
                                      ("new_asset_id", "other", "exact asset"),
                                      ("new_master_id", "other", "exact asset")):
            changed = {**self.evidence, field: value}
            self.write(self.evidence_path, changed)
            with self.subTest(field=field), self.assertRaisesRegex(conflict.ConflictBlocked, message):
                conflict.load_experiment(self.repo, self.evidence_path, self.baseline_name)
        self.write(self.evidence_path, self.evidence)
        self.write(self.repo / ".smoke" / self.baseline_name / "album-private.json", {"changed": True})
        with self.assertRaisesRegex(conflict.ConflictBlocked, "saved baseline bytes"):
            conflict.load_experiment(self.repo, self.evidence_path, self.baseline_name)

    def test_private_inputs_are_bounded(self):
        directory = self.repo / ".smoke"
        path = directory / "bad.json"
        for raw, limit, error, message in ((b"x" * 101, 100, conflict.ConflictBlocked, "bounded regular"),
                                          (b'{"x":1,"x":2}', 100, ChangesBlocked, "duplicate keys")):
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaisesRegex(error, message):
                conflict.read_report(self.repo, path, limit)
        link = directory / "linked.json"
        try:
            link.symlink_to(self.evidence_path)
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaisesRegex(conflict.ConflictBlocked, "link or reparse"):
            conflict.read_report(self.repo, link)

    def test_pacing_counts_slow_responses(self):
        clock, sleeps = [0.0], []
        def sleep(delay):
            sleeps.append(delay)
            clock[0] += delay
        with patch.object(conflict, "INTERVAL", 1), \
                patch("conflict.time.monotonic", side_effect=lambda: clock[0]), \
                patch("conflict.time.sleep", side_effect=sleep):
            pacer = conflict.Pacer()
            pacer.wait()
            clock[0] += 0.25
            pacer.wait()
            clock[0] += 2
            pacer.wait()
        self.assertEqual(sleeps, [0.75])

    def test_read_redirect_is_not_followed(self):
        service = SimpleNamespace(password_filter=None, http_timeout=30)
        with PyiCloudSession(service) as session, \
                patch.object(HTTPAdapter, "send", autospec=True, side_effect=lambda adapter, request, **kwargs:
                    self.response(request, {}, 302, {"Location": "https://idmsa.apple.com/appleauth/auth"})) as send, \
                conflict.read_guard(conflict.Pacer()), \
                self.assertRaisesRegex(conflict.ConflictBlocked, "Redirect refused"):
            session.post("https://idmsa.apple.com/appleauth/auth/signin/init", data="{}")
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
