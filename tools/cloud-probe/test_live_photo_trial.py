"""Disposable Live Photo recovery with synthetic media and socket-free transport."""

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from pyicloud_ipd.session import PyiCloudSession
from requests import Timeout
from requests.adapters import HTTPAdapter

import test_cleanup_trial as fixtures
from album import safe_record
from probe import PIN

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud-cleanup"))
import recovery_trial as live


class LivePhotoTrialTests(unittest.TestCase):
    profile = live.LIVE_PHOTO

    def setUp(self):
        self.fixture = fixtures.CleanupTrialTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.resources = self.profile.resource_map
        self.payloads = {field: b"synthetic original " + filename.encode() for field, (filename, _, _) in self.resources.items()}
        self.urls = {field: "https://cvws.icloud-content.com/" + filename + "?token=SECRET_SIGNED_URL"
                     for field, (filename, _, _) in self.resources.items()}
        self.records = copy.deepcopy(self.fixture.records)
        master = next(r for r in self.records if r["recordName"] == fixtures.MASTER)
        for field, (_, file_type, _) in self.resources.items():
            master["fields"][field] = {"value": {"size": len(self.payloads[field]), "fileChecksum": "opaque-" + field,
                                                "downloadURL": self.urls[field]}}
            master["fields"][field.removesuffix("Res") + "FileType"] = {"value": file_type}
        observation = {key: [] for key in ("assets", "masters", "relations")}
        for record in self.records:
            key = {"CPLAsset": "assets", "CPLMaster": "masters", "CPLContainerRelation": "relations"}[record["recordType"]]
            observation[key].append(safe_record(record))
        self.baseline = {**self.fixture.fixture.baseline, "passes": [observation, copy.deepcopy(observation)]}
        self.baseline_dir = self.repo / ".smoke/cloud-probe-livebaseline"
        self.downloads = self.repo / ".smoke/cloud-probe-livedownloads"
        self.baseline_dir.mkdir()
        self.downloads.mkdir()
        self.write(self.baseline_dir / "album-private.json", self.baseline)
        self.write(self.baseline_dir / "summary.json", {"schema": 1, "provider_version": PIN,
                   "scope": "album", "phase": "completed", "album_metadata_completed": True})
        self.write(self.downloads / "summary.json", {"provider_version": PIN, "scope": "variants",
                   "phase": "completed", "resource_download_completed": True})
        resources = []
        for index, (field, (filename, file_type, _)) in enumerate(self.resources.items(), 1):
            digest = hashlib.sha256(self.payloads[field]).hexdigest()
            item = {key: self.baseline[key] for key in ("account_id", "database", "zone_id", "album_id")}
            item.update(asset_id=fixtures.TARGET, master_id=fixtures.MASTER, resource_field=field,
                        resource_record_id=fixtures.MASTER, resource_record_type="CPLMaster", resource_file_type=file_type,
                        resource_size=len(self.payloads[field]), downloaded_bytes=len(self.payloads[field]),
                        provider_checksum_opaque="opaque-" + field, copy_verified=True, download_complete=True,
                        response_sha256=digest, copy_a_sha256=digest, copy_b_sha256=digest)
            for key, folder in (("copy_a", "copy-a"), ("copy_b", "copy-b")):
                (self.downloads / folder).mkdir(exist_ok=True)
                item[key] = folder + f"/resource-{index:02d}." + filename.split('.')[-1]
                (self.downloads / item[key]).write_bytes(self.payloads[field])
            resources.append(item)
        self.manifest = self.downloads / "download-manifest-private.json"
        self.write(self.manifest, {"schema": 1, "complete": True, "resources": resources})

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def run_cli(self, case="success", execute=True):
        run = self.repo / ".smoke" / ("cloud-probe-live" + case)
        run.mkdir()
        (run / "cookies").mkdir()
        records = copy.deepcopy(self.records)
        state = {"writes": 0, "gets": 0, "lookups": 0, "removed": False, "restored": False, "calls": [], "get_fields": []}
        upper = self.profile.label.upper()
        last_field = next(reversed(self.resources))
        service = SimpleNamespace(data={"dsInfo": {"dsid": "wrong-account" if case == "wrongaccount" else "synthetic-account"}},
            params={"dsid": "wrong-account" if case == "wrongaccount" else "synthetic-account"},
            requires_2fa=False, requires_2sa=False, password_filter=None, http_timeout=30, session_data={},
            session_path=str(run / "session-private.json"), cookiejar_path="unused", _get_webservice_url=lambda _: fixtures.ROOT)

        def selected():
            return [r for r in records if r["recordName"] in (fixtures.TARGET, fixtures.MASTER)]

        def answer(prompt):
            if "authenticate:" in prompt:
                return "NO" if case == "scope" else self.profile.scope_phrase
            if "REMOVE ONE " + upper in prompt:
                if case == "local":
                    (run / ("recovery-" + self.resources[last_field][0])).write_bytes(b"changed recovery")
                if case == "plan":
                    self.write(run / "remove-plan-private.json", {})
                return "NO" if case == "refuse" else prompt[len("Type "):-len(" to send once: ")]
            if "SEEN " + upper in prompt:
                return "SEEN " + upper
            if "RESTORED ONE " + upper in prompt:
                if case == "restorecancel":
                    return "NO"
                state.update(removed=False, restored=True)
                for record in selected():
                    record["fields"]["isDeleted"] = {"value": None}
                    record["recordChangeTag"] = "restored-" + record["recordType"]
                if case == "restoreresource":
                    selected()[1]["fields"][last_field]["value"]["fileChecksum"] = "changed"
                return "RESTORED ONE " + upper
            self.assertIn(upper + " PLAYS", prompt)
            return "NO" if case == "playback" else upper + " PLAYS"

        def send(adapter, request, **kwargs):
            path = urlsplit(request.url).path
            state["calls"].append((request.method, path))
            self.assertTrue(kwargs["verify"])
            if request.method == "GET":
                state["gets"] += 1
                field = next(key for key, url in self.urls.items() if url == request.url)
                state["get_fields"].append(field)
                self.assertNotIn("Cookie", request.headers)
                self.assertNotIn("Authorization", request.headers)
                self.assertTrue(kwargs["stream"])
                if case == "readthrottle":
                    return self.fixture.response(request, b"", 429, {"Retry-After": "7"})
                if case == "readtimeout":
                    raise Timeout("SECRET_LOST_REPLY")
                payload = self.payloads[field]
                if field == last_field:
                    if case == ("restoredbytes" if state["restored"] else "beforebytes"):
                        payload = b"x" * len(payload)
                    if case == "preflight" and not state["restored"]:
                        selected()[0]["recordChangeTag"] = "changed-during-read"
                    if case == "finaldrift" and state["restored"]:
                        records[0]["recordChangeTag"] = "unrelated-change"
                return self.fixture.response(request, payload, headers={"Content-Type": self.resources[field][2]})
            body = json.loads(request.body)
            if path.endswith("/zones/list"):
                return self.fixture.response(request, {"zones": [{"zoneID": fixtures.ZONE}]} if "/private/" in path else {"zones": []})
            if path.endswith("/query/batch"):
                return self.fixture.response(request, fixtures.album_fixtures.count_response(4 if state["removed"] else 5))
            if path.endswith("/query"):
                if body["query"]["recordType"] == "CPLAlbumByPositionLive":
                    return self.fixture.response(request, {"records": [fixtures.album_fixtures.album_record()]})
                rank = next(f["fieldValue"]["value"] for f in body["query"]["filterBy"] if f["fieldName"] == "startRank")
                active = [r for r in records if not state["removed"] or r["recordName"] not in (fixtures.TARGET, fixtures.MASTER, "relation-5")]
                return self.fixture.response(request, {"records": active if rank == 0 else []})
            query = parse_qs(urlsplit(request.url).query)
            self.assertEqual((query["remapEnums"], query["getCurrentSyncToken"]), (["True"], ["True"]))
            if path.endswith("/lookup"):
                state["lookups"] += 1
                self.assertEqual(body["records"], [{"recordName": fixtures.TARGET}, {"recordName": fixtures.MASTER}])
                found = copy.deepcopy(selected())
                if case == "lookupscope":
                    found[0]["zoneID"] = {**fixtures.ZONE, "ownerRecordName": "wrong"}
                if case == "removedunknown" and state["removed"]:
                    found[0]["fields"].pop("isDeleted")
                return self.fixture.response(request, {"records": found})
            self.assertTrue(path.endswith("/modify"))
            intent = json.loads((run / "write-intent-private.json").read_text())
            self.assertEqual(body, intent["plan"]["request"])
            self.assertEqual(body["operations"], [{"operationType": "update", "record": {
                "recordName": fixtures.TARGET, "recordType": "CPLAsset", "recordChangeTag": "asset-tag",
                "fields": {"isDeleted": {"value": 1}}}}])
            for field, (filename, _, _) in self.resources.items():
                self.assertEqual((run / ("before-" + filename)).read_bytes(), self.payloads[field])
                self.assertEqual((run / ("recovery-" + filename)).read_bytes(), self.payloads[field])
            state["writes"] += 1
            if case == "writethrottle":
                return self.fixture.response(request, b"", 503, {"Retry-After": "11"})
            if case == "conflict":
                return self.fixture.response(request, {"records": [{"recordName": fixtures.TARGET, "serverErrorCode": "CONFLICT"}]})
            if case == "lostbefore":
                raise Timeout("SECRET_LOST_REPLY")
            state["removed"] = True
            for record in selected():
                record["fields"]["isDeleted"] = {"value": 1}
                record["recordChangeTag"] = "removed-" + record["recordType"]
            if case == "remaining":
                records[0]["recordChangeTag"] = "unrelated-change"
            if case == "lostafter":
                raise Timeout("SECRET_LOST_REPLY")
            return self.fixture.response(request, {"records": [{"recordName": fixtures.TARGET, "recordType": "CPLAsset",
                "recordChangeTag": "removed-CPLAsset", "fields": {"isDeleted": {"value": 1}}}]})

        persist = live.save_new
        def save(path, value):
            if case == "intent" and path.name == "write-intent-private.json":
                raise OSError("Synthetic persistence failure")
            persist(path, value)
        output = io.StringIO()
        argv = [self.profile.key + "_trial.py", "--baseline", self.baseline_dir.name, "--downloads", self.downloads.name, "--album", fixtures.NAME]
        with PyiCloudSession(service) as session, \
                patch("pyicloud_ipd.PyiCloudService", return_value=service) as authenticate, \
                patch.object(HTTPAdapter, "send", autospec=True, side_effect=send), \
                patch.object(live, "__file__", str(self.repo / "tools/cloud-cleanup/recovery_trial.py")), \
                patch.object(live, "new_run", return_value=run), patch.object(live, "version", return_value=PIN), \
                patch.object(live, "save_new", side_effect=save), \
                patch.object(sys, "argv", argv + (["--live"] if execute else [])), \
                patch.object(sys.stdin, "isatty", return_value=case != "noninteractive"), patch.object(sys.stderr, "isatty", return_value=True), \
                patch("builtins.input", side_effect=answer), patch.object(live.getpass, "getpass", return_value="fake@example.invalid"), \
                redirect_stdout(output):
            service.session = session
            session.trust_env = False
            session.cookies.save = Mock()
            result = live.main(self.profile)
        summary = json.loads((run / "summary.json").read_text())
        self.assertEqual((summary["modification_requests"], summary["media_gets"], summary["lookup_requests"]),
                         (state["writes"], state["gets"], state["lookups"]))
        self.assertLessEqual(state["writes"], 1)
        self.assertLessEqual(state["gets"], self.profile.max_gets)
        self.assertLessEqual(state["lookups"], 3)
        for sensitive in ("SECRET", fixtures.TARGET, "fake@example.invalid"):
            self.assertNotIn(sensitive, output.getvalue())
        for path in run.glob("*.json"):
            self.assertNotIn("SECRET", path.read_text())
        return result, summary, state, run, authenticate

    def test_preparation_is_offline(self):
        result, summary, state, run, authenticate = self.run_cli(execute=False)
        self.assertEqual(result, 0, summary)
        authenticate.assert_not_called()
        self.assertFalse(summary["authentication_attempted"])
        self.assertEqual(state["calls"], [])
        for field, (filename, _, _) in self.resources.items():
            self.assertEqual((run / ("recovery-" + filename)).read_bytes(), self.payloads[field])
        self.assertFalse((run / "write-intent-private.json").exists())

    def test_complete_trial_preserves_pair(self):
        original = {p: p.read_bytes() for p in [*self.downloads.rglob('*'), *self.baseline_dir.iterdir()] if p.is_file()}
        for case in ("success", "lostafter"):
            with self.subTest(case=case):
                result, summary, state, run, _ = self.run_cli(case)
                self.assertEqual(result, 0, summary)
                self.assertEqual(summary["phase"], "completed")
                self.assertEqual(summary["reply"], "UNKNOWN" if case == "lostafter" else "ACCEPTED")
                self.assertEqual((state["writes"], state["gets"], state["lookups"]), (1, self.profile.max_gets, 3))
                self.assertEqual(state["get_fields"], list(self.resources) * 2)
                self.assertFalse(state["removed"])
                self.assertTrue(summary["restored_originals_sha256_verified"])
                self.assertEqual(summary["restored_playback"], "USER REPORTED")
                proof = json.loads((run / "byte-match-private.json").read_text())
                self.assertEqual(proof["restored_resource_hashes"], {key: hashlib.sha256(value).hexdigest() for key, value in self.payloads.items()})
                for field, (filename, _, _) in self.resources.items():
                    self.assertEqual((run / ("restored-" + filename)).read_bytes(), self.payloads[field])
        self.assertEqual(original, {p: p.read_bytes() for p in original})

    def test_input_bindings_fail_closed(self):
        cases = {"path": "Unsafe saved resource path", "scopebinding": "download binding or hashes differ",
                 "copy": "preserved original changed", "oversized": "Exact original resources",
                 "extra": "unknown resources", "owner": "ownership is ambiguous"}
        album_path = self.baseline_dir / "album-private.json"
        copy_path = self.downloads / json.loads(self.manifest.read_text())["resources"][-1]["copy_b"]
        originals = {path: path.read_bytes() for path in (album_path, self.manifest, copy_path)}
        for case, finding in cases.items():
            try:
                if case in ("path", "scopebinding"):
                    value = json.loads(self.manifest.read_text())
                    if case == "path":
                        value["resources"][0]["copy_a"] = "../outside.heic"
                    else:
                        value["resources"][0]["account_id"] = "wrong-account"
                    self.write(self.manifest, value)
                elif case == "copy":
                    copy_path.write_bytes(b"changed synthetic motion")
                else:
                    value = copy.deepcopy(self.baseline)
                    for observation in value["passes"]:
                        master = next(r for r in observation["masters"] if r["recordName"] == fixtures.MASTER)
                        if case == "oversized":
                            master["fields"][next(reversed(self.resources))]["size"] = self.profile.max_resource + 1
                        elif case == "extra":
                            master["fields"]["resMysteryRes"] = {}
                        else:
                            observation["assets"][0]["fields"]["masterRef"] = {"recordName": fixtures.MASTER}
                    self.write(album_path, value)
                with self.subTest(case=case):
                    result, summary, state, _, authenticate = self.run_cli(case)
                    self.assertEqual(result, 1)
                    self.assertIn(finding, summary["finding"])
                    authenticate.assert_not_called()
                    self.assertEqual(state["calls"], [])
            finally:
                for path, value in originals.items():
                    path.write_bytes(value)

    def test_reads_block_removal(self):
        cases = {"scope": (0, "Scope confirmation"), "noninteractive": (0, "interactive Terminal"),
                 "wrongaccount": (0, "Authenticated account"), "lookupscope": (0, "identity or scope changed"),
                 "beforebytes": (2, "differs from the preserved bytes"), "preflight": (2, "Album changed while preserving"),
                 "refuse": (2, "confirmation did not match"), "local": (2, "preserved original changed"),
                 "plan": (2, "plan changed"), "intent": (2, None),
                 "readthrottle": (1, None), "readtimeout": (1, None)}
        for case, (gets, finding) in cases.items():
            with self.subTest(case=case):
                result, summary, state, run, _ = self.run_cli(case)
                self.assertEqual(result, 1, summary)
                self.assertEqual((state["writes"], state["gets"]), (0, gets))
                if finding:
                    self.assertIn(finding, summary["finding"])
                self.assertFalse((run / "write-intent-private.json").exists())
                if case in ("readthrottle", "readtimeout"):
                    self.assertEqual(state["calls"][-1][0], "GET")
                if case == "readthrottle":
                    self.assertEqual(summary["retry_after_seconds"], 7)

    def test_write_failures_never_retry(self):
        cases = {"lostbefore": (2, "removal metadata"), "conflict": (2, "was rejected"),
                 "writethrottle": (2, None), "removedunknown": (2, "removal metadata"),
                 "remaining": (2, "Other album records"), "restorecancel": (2, "restoration was not confirmed"),
                 "restoreresource": (2, "resource metadata changed"), "restoredbytes": (4, "differs from the preserved bytes"),
                 "finaldrift": (4, "Album changed while checking"), "playback": (4, "remains unverified")}
        for case, (gets, finding) in cases.items():
            with self.subTest(case=case):
                result, summary, state, run, _ = self.run_cli(case)
                self.assertEqual(result, 1, summary)
                self.assertEqual((state["writes"], state["gets"]), (1, gets))
                if finding:
                    self.assertIn(finding, summary["finding"])
                self.assertNotEqual(summary["phase"], "completed")
                self.assertEqual(summary["restored_playback"], "NOT VERIFIED")
                self.assertEqual(summary.get("restored_originals_sha256_verified", False), case == "playback")
                self.assertTrue((run / "write-intent-private.json").exists())
                self.assertTrue((run / "write-outcome-private.json").exists())
                if case == "writethrottle":
                    self.assertTrue(state["calls"][-1][1].endswith('/modify'))
                    self.assertEqual(summary["retry_after_seconds"], 11)

    def test_transport_rejects_scope_changes(self):
        run = self.repo / '.smoke/cloud-probe-wire'
        run.mkdir()
        binding, baseline, _ = live.prepare(self.repo, self.baseline_dir.name, self.downloads.name, run, profile=self.profile)
        for change in ("purge", "force", "target", "extra", "url"):
            with self.subTest(change=change), PyiCloudSession(SimpleNamespace()) as session, patch.object(HTTPAdapter, "send") as send:
                service = SimpleNamespace(session=session, data={"dsInfo": {"dsid": binding["account_id"]}}, params={"dsid": binding["account_id"]})
                wire = live.RecoveryWire(service, fixtures.ROOT, binding, live.Pacer(), {}, profile=self.profile)
                wire.freeze_removal(live.trial.target_pair(baseline, binding))
                prepare = session.prepare_request
                def mutate(request):
                    value = prepare(request)
                    body = json.loads(value.body)
                    record = body["operations"][0]["record"]
                    if change == "purge":
                        record["fields"] = {"isExpunged": {"value": 1}}
                    elif change == "force":
                        body["operations"][0]["operationType"] = "forceUpdate"
                    elif change == "target":
                        record["recordName"] = "wrong-asset"
                    elif change == "extra":
                        body["operations"].append(copy.deepcopy(body["operations"][0]))
                    else:
                        value.url = value.url.replace('/private/', '/shared/')
                    value.body = live.canonical(body)
                    return value
                with patch.object(session, "prepare_request", side_effect=mutate), self.assertRaisesRegex(live.ConflictBlocked, "Prepared request changed"):
                    wire.send("modify")
                send.assert_not_called()
        for exhausted in ('total', 'slot'):
            wire.summary['media_gets'] = self.profile.max_gets if exhausted == 'total' else 1
            wire.downloads = set() if exhausted == 'total' else {('before', 'resOriginalRes')}
            with self.subTest(exhausted=exhausted), patch.object(HTTPAdapter, 'send') as send, \
                    self.assertRaisesRegex(live.trial.TrialBlocked, "budget exhausted"):
                live.download_resources(wire, self.urls, run, 'before')
            send.assert_not_called()

    def test_media_reads_are_bounded(self):
        root = self.repo / '.smoke/media'
        root.mkdir()
        path = root / 'original.bin'
        value = b'x' * (live.trial.MAX_ORIGINAL + 1)
        path.write_bytes(value)
        self.assertEqual(live.media_bytes(self.repo, path, profile=self.profile), value)
        with self.assertRaisesRegex(live.trial.TrialBlocked, 'bounded nonempty'):
            live.trial.original_bytes(self.repo, path)
        path.write_bytes(b'x' * (self.profile.max_resource + 1))
        with self.assertRaisesRegex(live.trial.TrialBlocked, 'bounded nonempty'):
            live.media_bytes(self.repo, path, profile=self.profile)
        link = self.repo / '.smoke/linked'
        if sys.platform == 'win32':
            subprocess.run(['cmd', '/C', 'mklink', '/J', str(link), str(root)], check=True, capture_output=True)
        else:
            link.symlink_to(root, target_is_directory=True)
        try:
            with patch.object(live.os, 'open') as opened, self.assertRaisesRegex(live.trial.TrialBlocked, 'link or reparse'):
                live.media_bytes(self.repo, link / path.name, profile=self.profile)
            opened.assert_not_called()
        finally:
            if sys.platform == 'win32':
                link.rmdir()
            else:
                link.unlink()


if __name__ == '__main__':
    unittest.main()
