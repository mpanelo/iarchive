"""Test the disposable removal helper with fake transport, never an account."""

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from pyicloud_ipd.session import PyiCloudSession
from requests import Response, Timeout
from requests.adapters import HTTPAdapter

import conflict
from album import safe_record
from probe import PIN, save_new
import test_album as album_fixtures
import test_conflict as favorite_fixtures

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud-cleanup"))
import trial


ROOT, ZONE, NAME = album_fixtures.ROOT, album_fixtures.ZONE, album_fixtures.NAME
TARGET, MASTER = favorite_fixtures.TARGET, favorite_fixtures.MASTER
MEDIA_URL = "https://cvws.icloud-content.com/blue.jpg?token=SECRET_SIGNED_URL"


class CleanupTrialTests(unittest.TestCase):
    def setUp(self):
        # Share fixture construction without inheriting/rerunning Favorite tests.
        self.fixture = favorite_fixtures.ConflictTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        dns = patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden"))
        dns.start()
        self.addCleanup(dns.stop)
        self.repo = self.fixture.repo
        self.payload = b"invented JPEG trial fixture"
        self.records = copy.deepcopy(self.fixture.records)
        for record in self.records:
            if record["recordName"] in (TARGET, MASTER):
                record["fields"]["isDeleted"] = {"value": None}
            if record["recordName"] == MASTER:
                record["fields"]["resOriginalRes"]["value"].update(size=len(self.payload), downloadURL=MEDIA_URL)
            if record["recordName"] == TARGET:
                record["fields"]["isFavorite"] = {"value": 1}
        self.records += [{"recordName": "relation-" + str(n), "recordType": "CPLContainerRelation",
                          "recordChangeTag": "relation-tag", "fields": {
                              "masterRef": {"value": {"recordName": "full-master-" + str(n)}}}} for n in range(1, 6)]
        observed = {key: [] for key in ("assets", "masters", "relations")}
        for record in self.records:
            key = {"CPLAsset": "assets", "CPLMaster": "masters", "CPLContainerRelation": "relations"}[record["recordType"]]
            observed[key].append(safe_record(record))
        self.fixture.baseline["passes"] = [observed, copy.deepcopy(observed)]
        baseline_dir = self.repo / ".smoke" / self.fixture.baseline_name
        self.fixture.write(baseline_dir / "album-private.json", self.fixture.baseline)
        self.fixture.evidence["fixture"]["sha256"] = hashlib.sha256(self.payload).hexdigest()
        self.fixture.evidence["captures_sha256"][self.fixture.baseline_name] = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in baseline_dir.iterdir()}
        self.fixture.write(self.fixture.evidence_path, self.fixture.evidence)
        self.source = self.fixture.evidence_path.parent / self.fixture.evidence["fixture"]["file"]
        self.source.write_bytes(self.payload)

    def response(self, request, value, status=200, headers=None):
        response = Response()
        response.status_code = status
        response.headers.update({"Content-Type": "application/json", **(headers or {})})
        response.raw = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
        response.request, response.url = request, request.url
        return response

    def run_cli(self, case="success", live=True):
        run = self.repo / ".smoke" / ("cloud-probe-trial" + case)
        run.mkdir()
        (run / "cookies").mkdir()
        records = copy.deepcopy(self.records)
        stale_tag_delete = case.startswith("stale")
        state = {"removed": False, "gets": 0, "writes": 0, "calls": [], "lookups": 0}
        service = SimpleNamespace(data={"dsInfo": {"dsid": "synthetic-account"}},
            params={"dsid": "synthetic-account"}, requires_2fa=False, requires_2sa=False,
            password_filter=None, http_timeout=30, session_data={}, session_path=str(run / "session-private.json"),
            cookiejar_path="unused", _get_webservice_url=lambda _: ROOT)
        if case == "wrongaccount":
            service.data["dsInfo"]["dsid"] = service.params["dsid"] = "wrong-account"

        def target_records():
            return [r for r in records if r["recordName"] in (TARGET, MASTER)]

        def answer(prompt):
            self.assertTrue(prompt.endswith(" [y/N] "))
            if prompt.startswith("Start this one-photo trial"):
                return {"scope": "n", "scopeblank": ""}.get(case, "y")
            if prompt.startswith("Have you turned Favorite"):
                if case == "stale_nocycle":
                    return "n"
                target = next(record for record in target_records() if record["recordName"] == TARGET)
                if case != "stale_sametag":
                    target["recordChangeTag"] = "cycled-asset"
                target["fields"]["isFavorite"] = {"value": 0 if case == "stale_badfavorite" else 1}
                return "y"
            if prompt.startswith(("Move this one synthetic photo", "Send one soft-delete request")):
                if case == "localdrift":
                    (run / "recovery-original.jpg").write_bytes(b"changed")
                if case == "planedit":
                    (run / "remove-plan-private.json").write_text("{}")
                if case == "expired":
                    clock.monotonic.return_value = clock.monotonic() + 121
                if case == "refuseinterrupt":
                    raise KeyboardInterrupt
                return {"refuse": "n", "refuseblank": "", "refuseinvalid": "sure"}.get(case, "yes")
            if prompt.startswith("Can you see this synthetic photo"):
                return "n" if case == "seencancel" else "y"
            self.assertTrue(prompt.startswith("Have you restored this photo"))
            if case == "restorecancel":
                return "n"
            state["removed"] = False
            for record in target_records():
                record["fields"]["isDeleted"] = {"value": None}
                record["recordChangeTag"] = "restored-" + record["recordType"]
            if case == "restoredrift":
                next(r for r in target_records() if r["recordName"] == MASTER)["fields"]["resOriginalRes"]["value"]["fileChecksum"] = "changed"
            if case == "restoredotherdrift":
                next(r for r in records if r["recordName"] == "full-asset-1")["recordChangeTag"] = "unrelated-change"
            return "y"

        def send(adapter, request, **kwargs):
            path = urlsplit(request.url).path
            state["calls"].append((request.method, path))
            self.assertIs(kwargs["verify"], True)
            if request.method == "GET":
                self.assertTrue(kwargs["stream"])
                self.assertEqual(request.url, MEDIA_URL)
                self.assertNotIn("Authorization", request.headers)
                self.assertNotIn("Cookie", request.headers)
                state["gets"] += 1
                if case == "getthrottled":
                    return self.response(request, b"", 429, {"Retry-After": "17"})
                if case == "getredirect":
                    return self.response(request, b"", 302, {"Location": "https://evil.example"})
                content = b"wrong same-length original" if case == "bytes" else self.payload
                if case == "getoversized":
                    content = self.payload + b"extra"
                if case == "gettimeout":
                    raise Timeout("SECRET_TIMEOUT")
                if case == "preflightdrift":
                    target_records()[0]["recordChangeTag"] = "changed-during-download"
                return self.response(request, content, headers={"Content-Type": "image/jpeg"})
            body = json.loads(request.body)
            if path.endswith("/zones/list"):
                return self.response(request, {"zones": [{"zoneID": ZONE}]} if "/private/" in path else {"zones": []})
            if path.endswith("/query/batch"):
                return self.response(request, album_fixtures.count_response(4 if state["removed"] else 5))
            if path.endswith("/query"):
                if body["query"]["recordType"] == "CPLAlbumByPositionLive":
                    return self.response(request, {"records": [album_fixtures.album_record()]})
                rank = next(f["fieldValue"]["value"] for f in body["query"]["filterBy"] if f["fieldName"] == "startRank")
                active = [r for r in records if not state["removed"] or r["recordName"] not in (TARGET, MASTER, "relation-5")]
                return self.response(request, {"records": active if rank == 0 else []})
            query = parse_qs(urlsplit(request.url).query)
            self.assertTrue(kwargs["stream"])
            self.assertEqual(query["remapEnums"], ["True"])
            self.assertEqual(query["getCurrentSyncToken"], ["True"])
            if path.endswith("/lookup"):
                state["lookups"] += 1
                self.assertEqual(body["records"], [{"recordName": TARGET}, {"recordName": MASTER}])
                self.assertEqual("isFavorite" in body["desiredKeys"], stale_tag_delete)
                found = copy.deepcopy(target_records())
                if case == "lookupscope":
                    found[0]["zoneID"] = {**ZONE, "ownerRecordName": "wrong"}
                if case == "deletedunknown" and state["removed"]:
                    for record in found:
                        record["fields"].pop("isDeleted")
                return self.response(request, {"records": found})
            self.assertTrue(path.endswith("/records/modify"))
            intent = json.loads((run / "write-intent-private.json").read_text())
            self.assertEqual(body, intent["plan"]["request"])
            self.assertEqual(body["operations"], [{"operationType": "update", "record": {
                "recordName": TARGET, "recordType": "CPLAsset", "recordChangeTag": "asset-tag",
                "fields": {"isDeleted": {"value": 1}}}}])
            self.assertEqual(intent["plan"]["stale_tag_delete"], stale_tag_delete)
            self.assertEqual((run / "recovery-original.jpg").read_bytes(), self.payload)
            self.assertEqual((run / "downloaded-original.jpg").read_bytes(), self.payload)
            state["writes"] += 1
            if case == "writethrottled":
                return self.response(request, {"retryAfter": 27}, 429, {"Retry-After": "17"})
            if case in ("conflict", "stale_conflict"):
                return self.response(request, {"records": [{"recordName": TARGET, "serverErrorCode": "CONFLICT"}]})
            if case in ("lostbefore", "stale_unknown"):
                raise Timeout("SECRET_TIMEOUT")
            state["removed"] = True
            for record in target_records():
                record["fields"]["isDeleted"] = {"value": 1}
                record["recordChangeTag"] = "removed-" + record["recordType"]
            if case == "remainingdrift":
                next(r for r in records if r["recordName"] == "full-asset-1")["recordChangeTag"] = "unexpected-tag"
            if case == "lostafter":
                raise Timeout("SECRET_TIMEOUT")
            result = {"recordName": TARGET, "recordType": "CPLAsset", "recordChangeTag": "removed-CPLAsset",
                      "fields": {"isDeleted": {"value": 1}}}
            if case == "replywrongid":
                result["recordName"] = "wrong-target"
            return self.response(request, {"records": [result]})

        original_save = trial.save_new
        def persist(path, value):
            if case == "intentfailure" and path.name == "write-intent-private.json":
                raise OSError("Synthetic intent write failure")
            original_save(path, value)

        output = io.StringIO()
        argv = ["trial.py", "--evidence", str(self.fixture.evidence_path), "--baseline", self.fixture.baseline_name, "--album", NAME]
        if stale_tag_delete:
            argv.append("--stale-tag-delete")
        with PyiCloudSession(service) as session, \
                patch("pyicloud_ipd.PyiCloudService", return_value=service) as authenticate, \
                patch.object(HTTPAdapter, "send", autospec=True, side_effect=send), \
                patch.object(trial, "__file__", str(self.repo / "tools/cloud-cleanup/trial.py")), \
                patch.object(trial, "new_run", return_value=run), patch.object(trial, "version", return_value=PIN), \
                patch.object(trial, "save_new", side_effect=persist), \
                patch.object(trial, "time", wraps=trial.time) as clock, \
                patch.object(sys, "argv", argv + (["--live"] if live else [])), \
                patch.object(sys.stdin, "isatty", return_value=True), patch.object(sys.stderr, "isatty", return_value=True), \
                patch("builtins.input", side_effect=answer), patch("trial.getpass.getpass", return_value="fake@example.invalid"), \
                redirect_stdout(output):
            service.session = session
            session.trust_env = False
            session.cookies.save = Mock()
            result = trial.main()
        summary = json.loads((run / "summary.json").read_text())
        if summary.get("blocked") == "AssertionError":
            self.fail(output.getvalue())
        self.assertNotIn("SECRET", output.getvalue())
        self.assertNotIn(TARGET, output.getvalue())
        self.assertNotIn("fake@example.invalid", output.getvalue())
        for path in run.glob("*.json"):
            self.assertNotIn("SECRET", path.read_text())
        self.assertEqual(summary["modification_requests"], state["writes"])
        self.assertEqual(summary["media_gets"], state["gets"])
        self.assertLessEqual(state["writes"], 1)
        self.assertLessEqual(state["gets"], 1)
        return result, summary, state, run, authenticate

    def test_preparation_never_authenticates(self):
        result, summary, state, run, authenticate = self.run_cli(live=False)
        self.assertEqual(result, 0)
        self.assertEqual(summary["phase"], "offline_prepared")
        self.assertFalse(summary["authentication_attempted"])
        authenticate.assert_not_called()
        self.assertEqual(state["calls"], [])
        self.assertEqual((run / "recovery-original.jpg").read_bytes(), self.payload)
        self.assertFalse((run / "write-intent-private.json").exists())

    def test_stale_preparation_stays_offline(self):
        result, summary, state, run, authenticate = self.run_cli("stale_offline", live=False)
        plan = json.loads((run / "offline-plan-private.json").read_text())
        self.assertEqual(result, 0)
        self.assertEqual(summary["scope"], "one_stale_tag_delete_trial")
        self.assertEqual(plan["scope"], "one_stale_tag_delete")
        self.assertFalse(summary["authentication_attempted"])
        self.assertFalse(summary["stale_tag_delete_rejected"])
        authenticate.assert_not_called()
        self.assertEqual(state["calls"], [])
        self.assertFalse((run / "write-intent-private.json").exists())

    def test_trial_preserves_and_restores_identity(self):
        for case in ("success", "lostafter", "replywrongid"):
            with self.subTest(case=case):
                result, summary, state, run, _ = self.run_cli(case)
                self.assertEqual(result, 0, summary.get("finding", summary.get("blocked")))
                self.assertEqual(summary["phase"], "completed")
                self.assertEqual(summary["reply"], "ACCEPTED" if case == "success" else "UNKNOWN")
                self.assertEqual((state["writes"], state["gets"], state["lookups"]), (1, 1, 3))
                self.assertFalse(state["removed"])
                self.assertTrue(summary["original_sha256_verified"])
                self.assertTrue(summary["removal_metadata_observed"])
                self.assertTrue(summary["restored_album_membership_verified"])
                self.assertTrue(summary["preserved_originals_unchanged"])
                self.assertEqual(summary["recently_deleted_ui"], "USER REPORTED")
                self.assertEqual(summary["limits"], trial.LIMITS)
                intent = json.loads((run / "write-intent-private.json").read_text())
                self.assertEqual(intent["plan_sha256"], hashlib.sha256(trial.canonical(intent["plan"]).encode()).hexdigest())

    def test_stale_delete_outcomes(self):
        for case in ("stale_conflict", "stale_accepted"):
            with self.subTest(case=case):
                result, summary, state, run, _ = self.run_cli(case)
                self.assertEqual(result, 0, summary.get("finding", summary.get("blocked")))
                self.assertEqual(summary["phase"], "completed")
                self.assertEqual((state["writes"], state["gets"]), (1, 1))
                self.assertEqual(state["lookups"], 4 if case == "stale_conflict" else 5)
                self.assertFalse(state["removed"])
                if case == "stale_conflict":
                    self.assertEqual(summary["reply"], "CONFLICT")
                    self.assertTrue(summary["stale_tag_delete_rejected"])
                    self.assertTrue(summary["stale_delete_postcheck_completed"])
                    self.assertFalse(summary["removal_metadata_observed"])
                else:
                    self.assertEqual(summary["reply"], "ACCEPTED")
                    self.assertFalse(summary["stale_tag_delete_rejected"])
                    self.assertTrue(summary["restored_album_membership_verified"])
                    self.assertTrue(summary["preserved_originals_unchanged"])

    def test_stale_delete_stops_safely(self):
        cases = {"stale_nocycle": ("Trial cancelled", 0, 2),
                 "stale_badfavorite": ("Favorite cycle did not change", 0, 3),
                 "stale_sametag": ("Favorite cycle did not change", 0, 3),
                 "stale_unknown": ("soft-delete metadata", 1, 4)}
        for case, (finding, writes, lookups) in cases.items():
            with self.subTest(case=case):
                result, summary, state, run, _ = self.run_cli(case)
                self.assertEqual(result, 1)
                self.assertIn(finding, summary["finding"])
                self.assertEqual((state["writes"], state["lookups"]), (writes, lookups))
                self.assertEqual(state["gets"], 1)
                self.assertLessEqual(state["writes"], 1)
                self.assertEqual((run / "write-intent-private.json").exists(), bool(writes))

    def test_prewrite_guards_prevent_removal(self):
        cases = {"scope": ("Cancelled", "Trial cancelled"), "scopeblank": ("Cancelled", "Trial cancelled"),
                 "wrongaccount": ("TrialBlocked", "Authenticated account"),
                 "lookupscope": ("TrialBlocked", "Lookup returned"), "bytes": ("TrialBlocked", "Downloaded original differs"),
                 "getoversized": ("TrialBlocked", "download exceeded"), "gettimeout": ("Timeout", None),
                 "getredirect": ("TrialBlocked", "full response"), "getthrottled": ("ChangesThrottled", None),
                 "preflightdrift": ("TrialBlocked", "changed while preserving"),
                 "refuse": ("Cancelled", "Trial cancelled"), "refuseblank": ("Cancelled", "Trial cancelled"),
                 "refuseinvalid": ("Cancelled", "Trial cancelled"), "refuseinterrupt": ("Cancelled", "Trial cancelled"),
                 "expired": ("TrialBlocked", "Removal confirmation expired"),
                 "localdrift": ("TrialBlocked", "preserved original changed"), "planedit": ("TrialBlocked", "removal plan changed"),
                 "intentfailure": ("OSError", None)}
        for case, (error_type, finding) in cases.items():
            with self.subTest(case=case):
                result, summary, state, run, authenticate = self.run_cli(case)
                self.assertEqual(result, 1)
                self.assertEqual(state["writes"], 0)
                self.assertFalse(state["removed"])
                self.assertEqual(summary["blocked"], error_type)
                if finding:
                    self.assertIn(finding, summary["finding"])
                self.assertEqual(state["gets"], int(case not in ("scope", "scopeblank", "wrongaccount", "lookupscope")))
                if case in ("scope", "scopeblank"):
                    authenticate.assert_not_called()
                    self.assertFalse(summary["authentication_attempted"])
                if case == "wrongaccount":
                    self.assertEqual(state["calls"], [])
                if case == "getthrottled":
                    self.assertEqual(summary["retry_after_seconds"], 17)
                    self.assertEqual(state["calls"][-1][0], "GET")
                self.assertFalse((run / "write-intent-private.json").exists())

    def test_uncertain_writes_never_retry(self):
        cases = {"conflict": "Removal was rejected", "lostbefore": "soft-delete metadata", "writethrottled": None,
                 "deletedunknown": "soft-delete metadata", "remainingdrift": "Remaining album metadata",
                 "seencancel": "Trial cancelled", "restorecancel": "Trial cancelled",
                 "restoredrift": "Restored identity", "restoredotherdrift": "Restored identity"}
        for case, finding in cases.items():
            with self.subTest(case=case):
                result, summary, state, run, _ = self.run_cli(case)
                self.assertEqual(result, 1)
                self.assertEqual(state["writes"], 1)
                self.assertFalse(summary.get("restored_album_membership_verified", False))
                if finding:
                    self.assertIn(finding, summary["finding"])
                self.assertTrue((run / "write-intent-private.json").exists())
                self.assertTrue((run / "write-outcome-private.json").exists())
                if case == "writethrottled":
                    self.assertEqual(summary["retry_after_seconds"], 27)
                    self.assertTrue(state["calls"][-1][1].endswith("/modify"))
                if case in ("conflict", "lostbefore"):
                    self.assertFalse(state["removed"])

    def test_request_changes_are_blocked(self):
        run = self.repo / ".smoke/cloud-probe-wire"
        run.mkdir()
        binding, baseline = trial.prepare(self.repo, self.fixture.evidence_path, self.fixture.baseline_name, run)
        for change in ("purge", "force", "target", "zone", "extra", "url", "method"):
            with self.subTest(change=change), PyiCloudSession(SimpleNamespace()) as session, patch.object(HTTPAdapter, "send") as send:
                service = SimpleNamespace(session=session, data={"dsInfo": {"dsid": binding["account_id"]}},
                                          params={"dsid": binding["account_id"]})
                wire = trial.TrialWire(service, ROOT, binding, conflict.Pacer(), {})
                wire.freeze_removal(trial.target_pair(baseline, binding))
                original = session.prepare_request
                def mutate(request):
                    prepared = original(request)
                    body = json.loads(prepared.body)
                    record = body["operations"][0]["record"]
                    if change == "purge":
                        record["fields"] = {"isExpunged": {"value": 1}}
                    elif change == "force":
                        body["operations"][0]["operationType"] = "forceDelete"
                    elif change == "target":
                        record["recordName"] = "another-asset"
                    elif change == "zone":
                        body["zoneID"]["ownerRecordName"] = "another-owner"
                    elif change == "extra":
                        body["operations"].append(copy.deepcopy(body["operations"][0]))
                    elif change == "url":
                        prepared.url = "https://evil.example/records/modify"
                    else:
                        prepared.method = "DELETE"
                    prepared.body = json.dumps(body)
                    return prepared
                with patch.object(session, "prepare_request", side_effect=mutate), \
                        self.assertRaisesRegex(conflict.ConflictBlocked, "Prepared request changed"):
                    wire.send("modify")
                send.assert_not_called()
                self.assertEqual(wire.writes, 0)

    def test_fixture_and_reply_are_exact(self):
        for case in ("hash", "missing", "oversized"):
            with self.subTest(case=case):
                run = self.repo / ".smoke" / ("cloud-probe-file" + case)
                run.mkdir()
                if case == "missing":
                    self.source.unlink()
                else:
                    self.source.write_bytes(b"changed" if case == "hash" else b"x" * (trial.MAX_ORIGINAL + 1))
                with self.assertRaises((trial.TrialBlocked, OSError)):
                    trial.prepare(self.repo, self.fixture.evidence_path, self.fixture.baseline_name, run)
                self.assertFalse((run / "recovery-original.jpg").exists())
                self.source.write_bytes(self.payload)
        binding, _ = conflict.load_experiment(self.repo, self.fixture.evidence_path, self.fixture.baseline_name)
        record = {"recordName": TARGET, "recordType": "CPLAsset", "recordChangeTag": "tag",
                  "fields": {"isDeleted": {"value": 1}}}
        for wrong in ({"recordName": "wrong"}, {"fields": {"isDeleted": {"value": True}}},
                      {"fields": None}, {"zoneID": {**ZONE, "ownerRecordName": "wrong"}}, {"deleted": True}):
            self.assertEqual(trial.reply_kind(200, {"records": [{**record, **wrong}]}, binding), "UNKNOWN")
        self.assertEqual(trial.reply_kind(200, {"records": [record]}, binding), "ACCEPTED")

    def test_unknown_resource_evidence_is_blocked(self):
        run = self.repo / ".smoke/cloud-probe-resources"
        run.mkdir()
        binding, baseline = trial.prepare(self.repo, self.fixture.evidence_path, self.fixture.baseline_name, run)
        valid = trial.target_pair(baseline, binding)
        for change, expected in (("owner", "owner identities"), ("deletion", "unknown value"),
                                 ("motion", "motion or sidecar"), ("unknown", "unknown resource"),
                                 ("edit", "edited item")):
            pair = copy.deepcopy(valid)
            if change == "owner":
                pair["master"]["recordName"] = "wrong-master"
            elif change == "deletion":
                pair["asset"]["fields"]["isDeleted"] = False
            elif change == "motion":
                pair["master"]["fields"]["resOriginalVidComplRes"] = pair["master"]["fields"]["resOriginalRes"]
            elif change == "unknown":
                pair["master"]["fields"]["resFutureRes"] = pair["master"]["fields"]["resOriginalRes"]
            else:
                pair["asset"]["fields"]["adjustmentType"] = 1
            with self.subTest(change=change), self.assertRaisesRegex(trial.TrialBlocked, expected):
                trial.validate_original(pair, binding)
        shared = copy.deepcopy(baseline)
        shared["passes"][-1]["assets"][0]["fields"]["masterRef"] = {"recordName": MASTER}
        with self.assertRaisesRegex(trial.TrialBlocked, "ownership is ambiguous"):
            trial.target_pair(shared, binding)

    def test_linked_originals_are_never_opened(self):
        owned = self.repo / ".smoke/owned"
        owned.mkdir()
        (owned / "image.jpg").write_bytes(self.payload)
        link = self.repo / ".smoke/linked"
        if sys.platform == "win32":
            subprocess.run(["cmd", "/C", "mklink", "/J", str(link), str(owned)], check=True, capture_output=True)
        else:
            link.symlink_to(owned, target_is_directory=True)
        try:
            with patch.object(trial.os, "open") as opened, self.assertRaisesRegex(trial.TrialBlocked, "link or reparse"):
                trial.original_bytes(self.repo, link / "image.jpg")
            opened.assert_not_called()
        finally:
            if sys.platform == "win32":
                link.rmdir()
            else:
                link.unlink()


if __name__ == "__main__":
    unittest.main()
