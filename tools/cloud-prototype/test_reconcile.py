"""Reconcile real project report formats using invented identities and bytes only."""

import copy
from collections import Counter
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reconcile import Blocked, LIMITS, load_input, main, reconcile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/cloud-probe"))
from originals import run_downloads, select_resources
import probe
import test_album as album_fixtures


class Response:
    status_code = 200
    headers = {"Content-Type": "application/octet-stream"}

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def iter_content(self, **_kwargs):
        yield self.content

    def close(self):
        pass


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        temporary = tempfile.TemporaryDirectory(prefix="iarchive-reconcile-",
                                                dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        account = "synthetic-account"
        observation = {"assets": [], "masters": [], "relations": []}
        urls, bodies, expectations = {}, {}, {}
        for suffix, variants in (("live", ("resOriginalRes", "resOriginalVidComplRes", "resJPEGFullRes")),
                                 ("photo", ("resOriginalRes",))):
            asset_id, master_id = f"full-asset-{suffix}", f"full-master-{suffix}"
            asset = {"recordType": "CPLAsset", "recordName": asset_id, "recordChangeTag": "asset-tag",
                     "fields": {"masterRef": {"recordName": master_id}}}
            master = {"recordType": "CPLMaster", "recordName": master_id,
                      "recordChangeTag": "master-tag", "fields": {}}
            for field in variants:
                owner = asset if field == "resJPEGFullRes" else master
                content = f"invented {suffix} {field}".encode()
                owner["fields"][field] = {"size": len(content), "fileChecksum": "OPAQUE_NOT_SHA256",
                                          "download_url_present": True}
                owner["fields"][field.removesuffix("Res") + "FileType"] = (
                    "com.apple.quicktime-movie" if field == "resOriginalVidComplRes" else "public.jpeg")
                url = f"https://cvws.icloud-content.com/{suffix}/{field}?token=SECRET_SIGNED_URL"
                urls[(owner["recordType"], owner["recordName"], field)] = url
                bodies[url] = content
            observation["assets"].append(asset)
            observation["masters"].append(master)
            expectations[asset_id] = list(variants)
        # Exercise the actual capture boundary: account and structured zone must
        # come from probe.main(), not a hand-built approximation of its format.
        raw = []
        for record in (*observation["assets"], *observation["masters"]):
            item = {key: record[key] for key in ("recordName", "recordType", "recordChangeTag")}
            item["fields"] = {key: {"value": copy.deepcopy(value)} for key, value in record["fields"].items()}
            for field in record["fields"]:
                if field.endswith("Res"):
                    item["fields"][field]["value"]["downloadURL"] = urls[
                        (record["recordType"], record["recordName"], field)]
            raw.append(item)
        one_pass = [album_fixtures.count_response(2), {"records": raw}, {"records": []},
                    album_fixtures.count_response(2)]
        service = album_fixtures.AlbumTests().service(
            [{"records": [album_fixtures.album_record()]}] + copy.deepcopy(one_pass) * 2)
        service.data = {"dsInfo": {"dsid": account}}
        service.requires_2fa = service.requires_2sa = False
        service._get_webservice_url = lambda _name: album_fixtures.ROOT
        captured = self.root / "observed"
        captured.mkdir()
        output = io.StringIO()

        class Adapter:
            def send(self, *_args, **_kwargs):
                raise AssertionError("Unexpected transport request")

        modules = {"pyicloud_ipd": SimpleNamespace(PyiCloudService=lambda *_args, **_kwargs: service),
                   "requests.adapters": SimpleNamespace(HTTPAdapter=Adapter)}
        with patch.dict(sys.modules, modules), \
                patch.object(sys, "argv", ["probe.py", "album", "--album", album_fixtures.NAME, "--expected-assets", "2"]), \
                patch("probe.version", return_value=probe.PIN), \
                patch("probe.sys.stdin.isatty", return_value=True), \
                patch("probe.sys.stderr.isatty", return_value=True), \
                patch("builtins.input", return_value="READ ONLY ALBUM"), \
                patch("probe.getpass.getpass", return_value="fake@example.invalid"), \
                patch("probe.new_run", return_value=captured), patch("probe.logging.disable"), \
                patch("probe.discover_zones", return_value=({"private": [{
                    "zone_id": album_fixtures.ZONE, "deleted": False}], "shared": []}, [])), \
                redirect_stdout(output):
            self.assertEqual(probe.main(), 0)
        self.assertNotIn(account, output.getvalue())
        self.observation = json.loads((captured / "resource-report-private.json").read_text())
        self.assertEqual(self.observation["zone_id"], album_fixtures.ZONE)
        self.assertEqual(self.observation["account_id"], account)
        self.observation["expected_fields"] = expectations  # Known disposable-fixture expectations only.
        baseline = json.loads((captured / "album-private.json").read_text())
        plans = select_resources(baseline, baseline, urls, account, 2,
                                 {"resOriginalRes": 2, "resOriginalVidComplRes": 1, "resJPEGFullRes": 1}, 1024)
        run_downloads(None, plans, self.root, {}, self.write_json,
                      fetch=lambda _session, url, _offset: Response(bodies[url]))
        self.download = json.loads((self.root / "download-manifest-private.json").read_text())
        exe = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target")) / "debug" / (
            "iarchive.exe" if os.name == "nt" else "iarchive")
        self.assertTrue(exe.is_file(), "Run cargo build --locked -p iarchive first.")
        state, first, second = (self.root / name for name in ("state", "first-reports", "second-reports"))
        first.mkdir()
        second.mkdir()

        def run(*args):
            result = subprocess.run([str(exe), "--json", "--state-dir", str(state), *map(str, args)],
                                    capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        run("import", "--cycle", "synthetic", "--source", self.root / "copy-a")
        report = run("replicate", "--cycle", "synthetic", "--secondary", self.root / "copy-b",
                     "--primary-artifacts", first, "--secondary-artifacts", second, "--allow-unverified-devices")
        self.replica, _ = load_input(Path(report["artifact_bundles"][0]) / "manifest.jsonl", jsonl=True)

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def compare(self):
        return reconcile(self.observation, self.download, self.replica)

    def codes(self):
        return {item["code"] for item in self.compare()["findings"]}

    def test_records_match_without_media(self):
        for item in self.download["resources"]:
            item["zone_id"] = dict(reversed(list(item["zone_id"].items())))
        before = copy.deepcopy((self.observation, self.download, self.replica))
        for name in ("copy-a", "copy-b"):
            (self.root / name).rename(self.root / (name + "-offline"))
        result = self.compare()
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["summary"]["matched_resources"], 4)
        self.assertEqual(dict(Counter(item["resource_field"] for item in result["matches"])), {
            "resOriginalRes": 2, "resOriginalVidComplRes": 1, "resJPEGFullRes": 1})
        self.assertEqual({item["asset_id"] for item in result["matches"]}, {"full-asset-live", "full-asset-photo"})
        self.assertEqual(result["limits"], LIMITS)
        self.assertFalse(result["limits"]["removal_approved"])
        self.assertEqual(before, (self.observation, self.download, self.replica))
        for secret in ("SECRET_SIGNED_URL", "OPAQUE_NOT_SHA256", "synthetic_sha256"):
            self.assertNotIn(secret, json.dumps(result))

    def test_probe_account_gap_is_explicit(self):
        self.observation.pop("account_id")
        result = self.compare()
        self.assertIn("observation_account_unbound", {f["code"] for f in result["findings"]})
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["limits"]["cloud_coverage"], "NOT VERIFIED")

    def test_zone_owner_binding_is_explicit(self):
        original = copy.deepcopy(self.download)
        zone = self.observation["zone_id"]
        for changed in ({**zone, "zoneName": "different-zone"},
                        {**zone, "ownerRecordName": "different-owner"},
                        {**zone, "futureIdentity": "different"}):
            self.download = copy.deepcopy(original)
            self.download["resources"][0]["zone_id"] = changed
            with self.subTest(zone=changed):
                self.assertIn("download_metadata_mismatch", self.codes())
                self.assertEqual(self.compare()["summary"]["matched_resources"], 3)
        self.download = original
        zone.pop("ownerRecordName")
        for item in self.download["resources"]:
            item["zone_id"].pop("ownerRecordName")
        result = self.compare()
        self.assertIn("observation_zone_owner_unbound", {f["code"] for f in result["findings"]})
        self.assertEqual(result["matches"], [])

    def test_provenance_requires_exact_metadata(self):
        original = copy.deepcopy(self.download)
        for field, value, code in (
            *((key, "different", "download_metadata_mismatch") for key in (
                "account_id", "database", "zone_id", "album_id", "asset_id", "asset_change_tag",
                "master_id", "master_change_tag", "resource_record_change_tag", "resource_file_type",
                "provider_checksum_opaque")),
            *((key, "different", "resource_not_downloaded") for key in (
                "resource_record_type", "resource_record_id", "resource_field")),
            ("resource_size", 999, "download_metadata_mismatch"),
            ("resource_size", True, "download_metadata_mismatch"),
        ):
            with self.subTest(field=field):
                self.download = copy.deepcopy(original)
                self.download["resources"][0][field] = value
                result = self.compare()
                self.assertIn(code, {f["code"] for f in result["findings"]})
                self.assertLess(result["summary"]["matched_resources"], 4)
        self.download = copy.deepcopy(original)
        self.download["resources"][0].update(copy_a="copy-a/renamed.jpg", copy_b="copy-b/renamed.jpg")
        self.assertIn("download_not_in_replica", self.codes(), "equal hashes cannot infer a new path")

    def test_recorded_bytes_require_complete_copies(self):
        original = copy.deepcopy(self.download)
        for field, value, code in (
            ("download_complete", False, "resource_download_incomplete"),
            ("copy_verified", False, "resource_download_incomplete"),
            ("downloaded_bytes", 1, "resource_download_incomplete"),
            *((key, "a" * 64, "recorded_replica_bytes_mismatch") for key in (
                "response_sha256", "copy_a_sha256", "copy_b_sha256")),
        ):
            with self.subTest(field=field):
                self.download = copy.deepcopy(original)
                self.download["resources"][0][field] = value
                self.assertIn(code, self.codes())
        self.download = original
        self.download["complete"] = False
        self.assertIn("download_manifest_incomplete", self.codes())
        self.download["complete"] = True
        original_file = copy.deepcopy(self.replica[1])
        for key, value in (("source_sha256", "c" * 64), ("byte_count", 1),
                           ("local_replica", "NOT VERIFIED"), ("role", "unknown")):
            with self.subTest(replica_field=key):
                self.replica[1] = {**original_file, key: value}
                self.assertIn("recorded_replica_bytes_mismatch", self.codes())
        self.replica[1] = original_file
        self.replica[1]["secondary_sha256"] = "b" * 64
        # An opaque provider value cannot substitute for any downloaded/local SHA-256.
        self.download["resources"][0]["provider_checksum_opaque"] = "b" * 64
        key = self.download["resources"][0]["resource_field"]
        owner = self.download["resources"][0]["resource_record_id"]
        next(r for r in self.observation["resources"] if r["record_id"] == owner and r["resource_field"] == key)[
            "provider_checksum_opaque"] = "b" * 64
        self.assertIn("recorded_replica_bytes_mismatch", self.codes())

    def test_missing_variants_and_extras_remain_findings(self):
        motion = next(i for i, r in enumerate(self.observation["resources"])
                      if r["resource_field"] == "resOriginalVidComplRes")
        self.observation["resources"].pop(motion)
        for asset in self.observation["assets"]:
            asset["resource_indices"] = [i - (i > motion) for i in asset["resource_indices"] if i != motion]
        codes = self.codes()
        self.assertTrue({"missing_expected_resource", "unmapped_download", "unmapped_replica_file"} <= codes)
        original = next(r for r in self.observation["resources"] if r["resource_field"] == "resOriginalRes")
        original["resource_field"] = "resFutureRes"
        original["role"] = "unknown"
        self.assertIn("unsupported_resource_metadata", self.codes())
        self.assertIn("missing_original_resource", self.codes())

    def test_observed_ownership_must_agree(self):
        original = copy.deepcopy(self.observation)
        changes = (
            (lambda v: v["resources"][0].update(asset_ids=["full-asset-live", "full-asset-photo"]),
             "ambiguous_resource_owner"),
            (lambda v: v["resources"][0].update(record_id="different-owner"), "resource_owner_mismatch"),
            (lambda v: v["assets"][0].update(resource_indices=[]), "resource_membership_mismatch"),
            (lambda v: v.update(metadata_passes_match=False), "observation_metadata_unstable"),
        )
        for change, code in changes:
            with self.subTest(code=code):
                self.observation = copy.deepcopy(original)
                change(self.observation)
                self.assertIn(code, self.codes())
                self.assertLess(self.compare()["summary"]["matched_resources"], 4)

    def test_ambiguous_mappings_never_merge(self):
        original = copy.deepcopy(self.download)
        self.download["resources"][1]["copy_a"] = self.download["resources"][0]["copy_a"]
        self.download["resources"][1]["copy_b"] = self.download["resources"][0]["copy_b"]
        result = self.compare()
        self.assertIn("ambiguous_download_binding", {f["code"] for f in result["findings"]})
        self.assertLessEqual(result["summary"]["matched_resources"], 2)
        self.download = original
        duplicate = copy.deepcopy(self.replica[1])
        duplicate["relative_path"] = duplicate["relative_path"].upper()
        self.replica.append(duplicate)
        self.assertIn("ambiguous_portable_path", self.codes())
        self.replica.pop()
        resource = copy.deepcopy(self.observation["resources"][0])
        index = len(self.observation["resources"])
        self.observation["resources"].append(resource)
        next(a for a in self.observation["assets"] if a["asset_id"] == resource["asset_ids"][0])[
            "resource_indices"].append(index)
        self.assertIn("ambiguous_resource_identity", self.codes())

    def test_cli_reports_bind_input_bytes(self):
        files = [self.root / name for name in ("observations.json", "downloads.json", "replica.jsonl")]
        self.write_json(files[0], self.observation)
        self.write_json(files[1], self.download)
        files[2].write_text("".join(json.dumps(record) + "\n" for record in self.replica), encoding="utf-8")
        args = ["reconcile.py", "--resource-report", str(files[0]), "--download-manifest", str(files[1]),
                "--replica-manifest", str(files[2])]
        for expected in (0, 2, 1):
            if expected == 2:
                self.observation["metadata_passes_match"] = False
                self.write_json(files[0], self.observation)
            if expected == 1:
                files[0].write_text('{"SECRET_SIGNED_URL":', encoding="utf-8")
            output = io.StringIO()
            with patch.object(sys, "argv", args), redirect_stdout(output):
                self.assertEqual(main(), expected)
            report = json.loads(output.getvalue())
            self.assertNotIn("SECRET_SIGNED_URL", output.getvalue())
            self.assertEqual(report["limits"], LIMITS)
            if expected == 0:
                self.assertEqual(report["input_sha256"], dict(zip(
                    ("resource_report", "download_manifest", "replica_manifest"),
                    (hashlib.sha256(path.read_bytes()).hexdigest() for path in files), strict=True)))

    def test_unsafe_or_malformed_inputs_are_refused(self):
        path = self.root / "invalid.json"
        for content in ('{"schema":1,"schema":1}', '{"value":NaN}', '{"schema":'):
            path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises((Blocked, ValueError)):
                load_input(path)
        path.write_text("{}\n\n", encoding="utf-8")
        with self.assertRaisesRegex(Blocked, "interrupted JSONL"):
            load_input(path, jsonl=True)
        with patch("reconcile.MAX_INPUT_BYTES", 1), self.assertRaisesRegex(Blocked, "bounded research"):
            load_input(path)
        for invalid in ("../escape", "copy-a/../escape", "C:/escape", "copy-a\\file", "copy-a//file"):
            changed = copy.deepcopy(self.download)
            changed["resources"][0]["copy_a"] = invalid
            with self.subTest(path=invalid), self.assertRaises(Blocked):
                reconcile(self.observation, changed, self.replica)
        for primary, secondary in (("copy-a", "copy-a"), ("COPY-A", "copy-a"), ("copy-a", "copy-a/nested")):
            with self.subTest(prefixes=(primary, secondary)), self.assertRaisesRegex(Blocked, "nonoverlapping"):
                reconcile(self.observation, self.download, self.replica, primary, secondary)
        for change in ({"record_type": "archive_summary"}, {"schema_version": True},
                       {"snapshot_type": "local_inventory_only"}):
            replica = copy.deepcopy(self.replica)
            replica[0].update(change)
            with self.subTest(header=change), self.assertRaisesRegex(Blocked, "deep replica manifest"):
                reconcile(self.observation, self.download, replica)
        observation = copy.deepcopy(self.observation)
        observation["zone_id"] = "PrimarySync"
        with self.assertRaisesRegex(Blocked, "JSON object"):
            reconcile(observation, self.download, self.replica)
        target, link = self.root / "evidence", self.root / "linked"
        target.mkdir()
        (target / "input.json").write_text("{}", encoding="utf-8")
        if os.name == "nt":
            subprocess.run(["cmd", "/C", "mklink", "/J", str(link), str(target)],
                           check=True, capture_output=True)
        else:
            link.symlink_to(target, target_is_directory=True)
        try:
            with self.assertRaisesRegex(Blocked, "link or reparse"):
                load_input(link / "input.json")
        finally:
            if os.name == "nt":
                link.rmdir()
            else:
                link.unlink()


if __name__ == "__main__":
    unittest.main()
