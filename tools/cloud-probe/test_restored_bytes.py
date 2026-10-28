"""Read-only restoration checks using synthetic trial records and fake transport."""

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from pyicloud_ipd.session import PyiCloudSession
from requests import Timeout
from requests.adapters import HTTPAdapter

import test_cleanup_trial as fixtures
from probe import PIN

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud-cleanup"))
import verify_restored as check


class RestoredByteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CleanupTrialTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        result, _, _, self.completed, _ = self.fixture.run_cli()
        self.assertEqual(result, 0, "Synthetic completed-trial setup failed")
        self.repo = self.fixture.repo
        self.records = copy.deepcopy(self.fixture.records)
        for record in self.records:
            if record["recordName"] in (fixtures.TARGET, fixtures.MASTER):
                record["recordChangeTag"] = "restored-" + record["recordType"]

    def run_cli(self, case="success", live=True):
        run = self.repo / ".smoke" / ("cloud-probe-byte" + case)
        run.mkdir()
        (run / "cookies").mkdir()
        records = copy.deepcopy(self.records)
        state = {"gets": 0, "lookups": 0, "writes": 0, "calls": []}
        target = next(record for record in records if record["recordName"] == fixtures.TARGET)
        if case == "album":
            target["recordChangeTag"] = "changed-before-read"
        service = SimpleNamespace(data={"dsInfo": {"dsid": "synthetic-account"}},
            params={"dsid": "synthetic-account"}, requires_2fa=False, requires_2sa=False,
            password_filter=None, http_timeout=30, session_data={}, session_path=str(run / "session-private.json"),
            cookiejar_path="unused", _get_webservice_url=lambda _: fixtures.ROOT)
        if case == "wrongaccount":
            service.data["dsInfo"]["dsid"] = service.params["dsid"] = "wrong-account"

        def send(adapter, request, **kwargs):
            path = urlsplit(request.url).path
            state["calls"].append((request.method, path))
            self.assertIs(kwargs["verify"], True)
            if path.endswith("/modify"):
                state["writes"] += 1
                raise AssertionError("Cloud mutation reached fake transport")
            if request.method == "GET":
                state["gets"] += 1
                self.assertEqual(request.url, fixtures.MEDIA_URL)
                self.assertNotIn("Cookie", request.headers)
                self.assertNotIn("Authorization", request.headers)
                self.assertTrue(kwargs["stream"])
                if case == "throttle":
                    return self.fixture.response(request, b"", 503, {"Retry-After": "9"})
                if case == "timeout":
                    raise Timeout("SECRET_LOST_REPLY")
                if case == "posttag":
                    target["recordChangeTag"] = "changed-during-read"
                if case == "local":
                    self.fixture.source.write_bytes(b"changed synthetic source")
                if case == "evidence":
                    (self.completed / "summary.json").write_text("{}")
                payload = b"x" * len(self.fixture.payload) if case == "bytes" else self.fixture.payload
                return self.fixture.response(request, payload, headers={"Content-Type": "image/jpeg"})
            body = json.loads(request.body)
            if path.endswith("/zones/list"):
                return self.fixture.response(request, {"zones": [{"zoneID": fixtures.ZONE}]} if "/private/" in path else {"zones": []})
            if path.endswith("/query/batch"):
                return self.fixture.response(request, fixtures.album_fixtures.count_response(5))
            if path.endswith("/query"):
                if body["query"]["recordType"] == "CPLAlbumByPositionLive":
                    return self.fixture.response(request, {"records": [fixtures.album_fixtures.album_record()]})
                rank = next(f["fieldValue"]["value"] for f in body["query"]["filterBy"] if f["fieldName"] == "startRank")
                return self.fixture.response(request, {"records": records if rank == 0 else []})
            self.assertTrue(path.endswith("/lookup"))
            state["lookups"] += 1
            self.assertEqual(body["records"], [{"recordName": fixtures.TARGET}, {"recordName": fixtures.MASTER}])
            found = copy.deepcopy([r for r in records if r["recordName"] in (fixtures.TARGET, fixtures.MASTER)])
            if case == "lookup":
                found[0]["recordChangeTag"] = "different-lookup-tag"
            return self.fixture.response(request, {"records": found})

        output = io.StringIO()
        argv = ["verify_restored.py", "--trial", "../outside" if case == "unsafe" else self.completed.name,
                "--album", fixtures.NAME]
        with PyiCloudSession(service) as session, \
                patch("pyicloud_ipd.PyiCloudService", return_value=service) as authenticate, \
                patch.object(HTTPAdapter, "send", autospec=True, side_effect=send), \
                patch.object(check, "__file__", str(self.repo / "tools/cloud-cleanup/verify_restored.py")), \
                patch.object(check, "new_run", return_value=run), patch.object(check, "version", return_value=PIN), \
                patch.object(sys, "argv", argv + (["--live"] if live else [])), \
                patch.object(sys.stdin, "isatty", return_value=case != "noninteractive"), \
                patch.object(sys.stderr, "isatty", return_value=True), \
                patch("builtins.input", return_value="NO" if case == "scope" else check.SCOPE_PHRASE), \
                patch.object(check.getpass, "getpass", return_value="fake@example.invalid"), redirect_stdout(output):
            service.session = session
            session.trust_env = False
            session.cookies.save = Mock()
            result = check.main()
        summary = json.loads((run / "summary.json").read_text())
        self.assertEqual(state["writes"], 0)
        self.assertEqual(summary["modification_requests"], 0)
        self.assertEqual(summary["media_gets"], state["gets"])
        self.assertEqual(summary["lookup_requests"], state["lookups"])
        for sensitive in ("SECRET", fixtures.TARGET, "fake@example.invalid"):
            self.assertNotIn(sensitive, output.getvalue())
        for path in run.glob("*.json"):
            self.assertNotIn("SECRET", path.read_text())
        self.assertFalse((run / "write-intent-private.json").exists())
        return result, summary, state, run, authenticate

    def test_offline_and_live_scope(self):
        originals = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in [self.fixture.source, *self.completed.glob("*.json"), *self.completed.glob("*.jpg")]}
        for live in (False, True):
            with self.subTest(live=live):
                result, summary, state, run, authenticate = self.run_cli("live" if live else "offline", live)
                self.assertEqual(result, 0, summary)
                self.assertEqual((state["gets"], state["lookups"]), (int(live), int(live)))
                self.assertEqual((run / "recovery-original.jpg").read_bytes(), self.fixture.payload)
                if live:
                    self.assertTrue(summary["restored_original_sha256_verified"])
                    self.assertTrue(summary["preserved_originals_unchanged"])
                    self.assertEqual(summary["limits"]["restored_remote_bytes"], "VERIFIED FOR ONE TEST JPEG")
                    self.assertEqual((run / "downloaded-original.jpg").read_bytes(), self.fixture.payload)
                    result = json.loads((run / "byte-match-private.json").read_text())
                    self.assertEqual(result["restored_sha256"], hashlib.sha256(self.fixture.payload).hexdigest())
                    authenticate.assert_called_once()
                else:
                    authenticate.assert_not_called()
                    self.assertFalse(summary["authentication_attempted"])
                    self.assertEqual(state["calls"], [])
                    self.assertFalse((run / "downloaded-original.jpg").exists())
                self.assertEqual({k: v for k, v in summary["limits"].items() if k != "restored_remote_bytes"},
                                 {k: v for k, v in fixtures.trial.LIMITS.items() if k != "restored_remote_bytes"})
        self.assertEqual(originals, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in originals})

    def test_trial_inputs_block_authentication(self):
        cases = {"incomplete": ("summary.json", "completed removal"),
                 "plan": ("write-intent-private.json", "plan or intent changed"),
                 "resources": ("restored-album-private.json", "membership or resources changed"),
                 "source": (None, "preserved original changed"),
                 "recovery": ("recovery-original.jpg", "preserved original changed")}
        for case, (filename, finding) in cases.items():
            path = self.completed / filename if filename else self.fixture.source
            previous = path.read_bytes()
            try:
                if path.suffix == ".json":
                    value = json.loads(previous)
                    if case == "incomplete":
                        value["phase"] = "interrupted"
                    elif case == "plan":
                        value["plan_sha256"] = "0" * 64
                    else:
                        for observation in value["passes"]:
                            target = next(r for r in observation["masters"] if r["recordName"] == fixtures.MASTER)
                            target["fields"]["resOriginalRes"]["fileChecksum"] = "changed"
                    path.write_text(json.dumps(value))
                else:
                    path.write_bytes(b"changed synthetic original")
                with self.subTest(case=case):
                    result, summary, state, _, authenticate = self.run_cli(case)
                    self.assertEqual(result, 1)
                    self.assertIn(finding, summary["finding"])
                    authenticate.assert_not_called()
                    self.assertEqual(state["calls"], [])
            finally:
                path.write_bytes(previous)
        result, summary, _, _, authenticate = self.run_cli("unsafe")
        self.assertEqual(result, 1)
        self.assertIn("Unsafe completed-trial", summary["finding"])
        authenticate.assert_not_called()

    def test_fresh_reads_fail_closed(self):
        cases = {"scope": (0, 0, "Scope confirmation"), "noninteractive": (0, 0, "interactive Terminal"),
                 "wrongaccount": (0, 0, "Authenticated account"), "album": (0, 0, "since the completed restoration"),
                 "lookup": (0, 1, "differs from the fresh album"), "bytes": (1, 1, "Downloaded original differs"),
                 "throttle": (1, 1, None), "timeout": (1, 1, None),
                 "posttag": (1, 1, "Album changed while reading"), "local": (1, 1, "preserved original changed"),
                 "evidence": (1, 1, "evidence changed during")}
        source = self.fixture.source.read_bytes()
        saved_summary = (self.completed / "summary.json").read_bytes()
        for case, (gets, lookups, finding) in cases.items():
            try:
                with self.subTest(case=case):
                    result, summary, state, run, authenticate = self.run_cli(case)
                    self.assertEqual(result, 1, summary)
                    self.assertEqual((state["gets"], state["lookups"]), (gets, lookups))
                    if finding:
                        self.assertIn(finding, summary["finding"])
                    self.assertFalse(summary.get("restored_original_sha256_verified", False))
                    self.assertEqual(summary["limits"]["restored_remote_bytes"], "NOT VERIFIED")
                    self.assertFalse((run / "byte-match-private.json").exists())
                    if case in ("scope", "noninteractive"):
                        authenticate.assert_not_called()
                    if case in ("throttle", "timeout"):
                        self.assertEqual(state["calls"][-1][0], "GET")
                        self.assertEqual(summary["blocked"], "ChangesThrottled" if case == "throttle" else "Timeout")
                    if case == "throttle":
                        self.assertEqual(summary["retry_after_seconds"], 9)
            finally:
                self.fixture.source.write_bytes(source)
                (self.completed / "summary.json").write_bytes(saved_summary)

    def test_wire_blocks_writes_and_retries(self):
        _, binding, _, _ = check.load_trial(self.repo, self.completed.name)
        with PyiCloudSession(SimpleNamespace()) as session, patch.object(HTTPAdapter, "send") as send:
            service = SimpleNamespace(session=session, data={"dsInfo": {"dsid": binding["account_id"]}},
                                      params={"dsid": binding["account_id"]})
            wire = check.ReadOnlyWire(service, fixtures.ROOT, binding, check.Pacer(), {})
            with self.assertRaisesRegex(fixtures.trial.TrialBlocked, "Removal is forbidden"):
                wire.freeze_removal({})
            # Even an accidentally changed metadata flag cannot enable writes.
            wire.metadata_only = False
            wire.update_body = "{}"
            for kind in ("modify", "delete", "forceUpdate"):
                with self.subTest(kind=kind), self.assertRaisesRegex(fixtures.trial.TrialBlocked, "read-only"):
                    wire.send(kind)
            wire.lookups = 1
            with self.assertRaisesRegex(fixtures.trial.TrialBlocked, "one read-only"):
                wire.send("lookup")
            wire.summary["media_gets"] = 1
            with self.assertRaisesRegex(fixtures.trial.TrialBlocked, "Only one bound original"):
                fixtures.trial.download_original(wire, fixtures.MEDIA_URL, self.completed)
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
