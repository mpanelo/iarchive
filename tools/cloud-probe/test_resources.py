"""Resource observations and explicit expectations; invented records only."""

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from album import AlbumBlocked, collect_album, list_body, query_kind, safe_record
from resources import ResourceReportBlocked, build_resource_report
import probe
import test_album as fixtures


def resource(file_type, size=7):
    return {"Res": {"value": {"size": size, "fileChecksum": "opaque-synthetic-checksum",
                              "downloadURL": "https://SECRET_SIGNED_URL", "wrappingKey": "SECRET_KEY"}},
            "FileType": {"value": file_type}}


def add_resource(record, prefix, file_type, size=7):
    record["fields"].update({prefix + suffix: value
                             for suffix, value in resource(file_type, size).items()})


def variant_records():
    definitions = (
        ("live", "public.heic", (("resOriginalVidCompl", "com.apple.quicktime-movie"),),
         (("resJPEGMed", "public.jpeg"), ("resJPEGThumb", "public.jpeg"))),
        ("video", "com.apple.quicktime-movie", (),
         (("resVidFull", "com.apple.quicktime-movie"), ("resVidMed", "com.apple.quicktime-movie"))),
        ("edited", "public.jpeg", (), (("resJPEGFull", "public.jpeg"), ("resSidecar", "public.xml"))),
        ("raw", "com.adobe.raw-image", (), (("resOriginalAlt", "public.jpeg"),)),
    )
    records, expectations = [], {}
    for name, original_type, master_variants, asset_variants in definitions:
        asset, master = fixtures.pair(name)
        master["fields"] = {"itemType": {"value": original_type},
                            "filenameEnc": {"value": "SECRET_DUPLICATE_FILENAME"}}
        add_resource(master, "resOriginal", original_type)
        for owner, variants in ((master, master_variants), (asset, asset_variants)):
            for prefix, file_type in variants:
                add_resource(owner, prefix, file_type)
        records.extend((asset, master))
        expectations[asset["recordName"]] = ["resOriginalRes", *[
            prefix + "Res" for prefix, _ in (*master_variants, *asset_variants)]]
    return records, expectations


def observation(records):
    cleaned = [safe_record(record) for record in records]
    return {"assets": [record for record in cleaned if record["recordType"] == "CPLAsset"],
            "masters": [record for record in cleaned if record["recordType"] == "CPLMaster"],
            "relations": []}


class ResourceTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.records, self.expectations = variant_records()

    def test_resource_variants_remain_distinct(self):
        for original_type, alternate_type in (("com.adobe.raw-image", "public.jpeg"),
                                               ("public.jpeg", "com.adobe.raw-image")):
            with self.subTest(original_type=original_type):
                records = copy.deepcopy(self.records)
                for identity, field, file_type in (
                        ("full-master-raw", "resOriginalFileType", original_type),
                        ("full-asset-raw", "resOriginalAltFileType", alternate_type)):
                    next(record for record in records if record["recordName"] == identity)["fields"][
                        field] = {"value": file_type}
                expected = {
                    (record["recordType"], record["recordName"], field): (
                        record["recordChangeTag"],
                        record["fields"][field.removesuffix("Res") + "FileType"]["value"],
                        resource["value"]["size"], resource["value"]["fileChecksum"])
                    for record in records for field, resource in record["fields"].items()
                    if field.endswith("Res")
                }
                value = observation(records)
                before = copy.deepcopy(value)
                report = build_resource_report(value, self.expectations)
                actual = {
                    (item["record_type"], item["record_id"], item["resource_field"]): (
                        item["record_change_tag"], item["file_type"], item["size"],
                        item["provider_checksum_opaque"])
                    for item in report["resources"]
                }
                self.assertEqual(actual, expected)
                references = {record["recordName"]: record["fields"]["masterRef"]["value"]["recordName"]
                              for record in records if record["recordType"] == "CPLAsset"}
                self.assertEqual({asset["asset_id"] for asset in report["assets"]}, set(references))
                for asset in report["assets"]:
                    linked = {key for key in expected
                              if key[1] in (asset["asset_id"], references[asset["asset_id"]])}
                    observed = {tuple(report["resources"][index][key]
                                      for key in ("record_type", "record_id", "resource_field"))
                                for index in asset["resource_indices"]}
                    self.assertEqual(observed, linked)
                self.assertEqual(value, before)
                self.assertEqual(len(report["resources"]), 12)
                self.assertEqual(report["summary"]["resource_role_counts"], {
                    "original": 4, "original_video_companion": 1, "alternate_original": 1,
                    "full_image": 1, "full_video": 1, "sidecar": 1,
                    "preview_image": 2, "preview_video": 1,
                })
                self.assertEqual(report["summary"]["expectation_status"], "MATCHED OBSERVED FIELDS")
                self.assertTrue(all(asset["pair_confidence"] == "unknown" for asset in report["assets"]))
                for key in ("complete_resource_inventory", "content_mapping", "provider_checksum_algorithm"):
                    self.assertEqual(report["summary"][key], "NOT VERIFIED")
                self.assertNotIn("SECRET", json.dumps(report))

    def test_missing_variants_are_reported(self):
        for owner_id, field in (("full-master-live", "resOriginalVidComplRes"),
                                ("full-asset-raw", "resOriginalAltRes"),
                                ("full-asset-edited", "resJPEGFullRes"),
                                ("full-asset-edited", "resSidecarRes")):
            records = copy.deepcopy(self.records)
            next(record for record in records if record["recordName"] == owner_id)["fields"].pop(field)
            report = build_resource_report(observation(records), self.expectations)
            with self.subTest(field=field):
                missing = [item for item in report["findings"] if item["code"] == "missing_expected_resource"]
                self.assertEqual([item["resource_field"] for item in missing], [field])
                self.assertEqual(report["summary"]["expectation_status"], "FINDINGS")

    def test_previews_cannot_replace_originals(self):
        self.records[1]["fields"].pop("resOriginalRes")
        report = build_resource_report(observation(self.records))
        self.assertIn("missing_original_resource", report["summary"]["resource_finding_counts"])
        self.assertEqual(report["summary"]["expectation_status"], "NOT PROVIDED")

    def test_orphan_resource_metadata_is_reported(self):
        self.records[1]["fields"].pop("resOriginalVidComplRes")
        self.records[1]["fields"]["resOriginalVidComplWidth"] = {"value": 1920}
        report = build_resource_report(observation(self.records))
        missing = [item for item in report["findings"] if item["code"] == "metadata_without_resource"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["resource_field"], "resOriginalVidComplRes")
        self.assertEqual(report["summary"]["observed_resources"], 11)

    def test_ambiguous_owners_preserve_resources(self):
        add_resource(self.records[0], "resOriginal", "public.heic")
        report = build_resource_report(observation(self.records), self.expectations)
        originals = [item for item in report["resources"] if item["resource_field"] == "resOriginalRes"
                     and "full-asset-live" in item["asset_ids"]]
        self.assertEqual({item["record_type"] for item in originals}, {"CPLAsset", "CPLMaster"})
        self.assertIn("ambiguous_resource_ownership", report["summary"]["resource_finding_counts"])
        self.assertEqual(report["summary"]["expectation_status"], "FINDINGS")

    def test_master_relationship_findings(self):
        value = observation(self.records)
        value["assets"][1]["fields"]["masterRef"]["recordName"] = "full-master-live"
        report = build_resource_report(value)
        self.assertEqual(len(report["resources"]), 12)
        self.assertEqual(report["summary"]["resource_finding_counts"]["shared_master"], 2)
        self.assertEqual(report["summary"]["resource_finding_counts"]["unreferenced_master"], 1)
        shared = next(item for item in report["resources"] if item["record_id"] == "full-master-live")
        self.assertEqual(shared["asset_ids"], ["full-asset-live", "full-asset-video"])

    def test_missing_master_is_not_inferred(self):
        value = observation(self.records)
        value["assets"][0]["fields"]["masterRef"] = {"recordName": "missing-master"}
        report = build_resource_report(value)
        self.assertIn("missing_master_reference", report["summary"]["resource_finding_counts"])
        self.assertEqual(len(report["resources"]), 12)

    def test_unknown_variants_are_preserved(self):
        add_resource(self.records[0], "resFutureVariant", "com.vendor.unknown")
        report = build_resource_report(observation(self.records), self.expectations)
        unknown = next(item for item in report["resources"] if item["resource_field"] == "resFutureVariantRes")
        self.assertEqual(unknown["role"], "unknown")
        self.assertEqual(unknown["file_type"], "com.vendor.unknown")
        self.assertIn("unknown_resource_field", report["summary"]["resource_finding_counts"])
        self.assertIn("unexpected_resource", report["summary"]["resource_finding_counts"])
        self.assertNotIn("SECRET", json.dumps(report))

    def test_incomplete_resources_are_preserved(self):
        add_resource(self.records[0], "resSidecar", "public.xml", size=0)
        value = self.records[0]["fields"]
        value["resSidecarRes"]["value"].pop("downloadURL")
        value.pop("resSidecarFileType")
        report = build_resource_report(observation(self.records))
        self.assertEqual(len(report["resources"]), 13)
        counts = report["summary"]["resource_finding_counts"]
        self.assertTrue({"empty_resource", "resource_url_unavailable", "missing_file_type"} <= set(counts))

    def test_expectation_scope_and_shape(self):
        value = observation(self.records)
        report = build_resource_report(value, {"full-asset-live": self.expectations["full-asset-live"]})
        self.assertEqual(report["summary"]["expectation_asset_count"], 1)
        self.assertEqual(report["summary"]["expectation_status"], "MATCHED OBSERVED FIELDS")
        self.assertEqual(report["summary"]["complete_resource_inventory"], "NOT VERIFIED")
        for expectations in ([], {"full-asset": ["resOriginalRes"]},
                             {"full-asset-live": []}, {"full-asset-live": "resOriginalRes"},
                             {"full-asset-live": ["resOriginalRes", "resOriginalRes"]},
                             {"full-asset-live": ["../escape"]}):
            with self.subTest(expectations=expectations), self.assertRaises(ResourceReportBlocked):
                build_resource_report(value, expectations)

    def test_invalid_record_identity_blocks_reporting(self):
        for mutation in (lambda value: value["masters"].append(value["masters"][0]),
                         lambda value: value["assets"][0].pop("recordChangeTag")):
            value = observation(self.records)
            mutation(value)
            with self.assertRaises(ResourceReportBlocked):
                build_resource_report(value)

    def test_unknown_drift_summary_hides_details(self):
        first, second = fixtures.pass_responses(), fixtures.pass_responses()
        add_resource(second[1]["records"][0], "resFutureVariant", "com.vendor.unknown")
        responses = [{"records": [fixtures.album_record()]}] + first + second
        private, summary = collect_album(fixtures.AlbumTests().service(responses), fixtures.ROOT,
                                         {"private": [{"zone_id": fixtures.ZONE, "deleted": False}]},
                                         fixtures.NAME, {})
        self.assertFalse(summary["two_passes_match"])
        self.assertEqual(summary["unrecognized_resource_fields"], 1)
        self.assertIn("resource_report", private)
        encoded = json.dumps(summary)
        for secret in ("full-asset", "full-master", "resFutureVariant", "opaque-synthetic-checksum", "SECRET"):
            self.assertNotIn(secret, encoded)

    def test_query_allowlist_stays_fixed(self):
        body = list_body(fixtures.ZONE, fixtures.ALBUM, 0)
        body["desiredKeys"].append("resFutureVariantRes")
        self.assertIsNone(query_kind(body, fixtures.ZONE, fixtures.ALBUM))

    def test_malformed_unknown_resources_are_rejected(self):
        for field, value in (("resFutureRes", {"value": {"size": 7}}),
                             ("res../escapeRes", {"value": resource("public.jpeg")["Res"]["value"]})):
            record = fixtures.pair()[0]
            record["fields"][field] = value
            with self.subTest(field=field), self.assertRaises(AlbumBlocked):
                safe_record(record)

    def test_album_report_round_trip(self):
        for count in (4, 5):
            with self.subTest(count=count):
                self.album_report_round_trip(count)

    def album_report_round_trip(self, count):
        records = copy.deepcopy(self.records)
        if count == 5:
            records.extend(fixtures.pair("new-upload"))
        one_pass = [fixtures.count_response(count), {"records": records},
                    {"records": []}, fixtures.count_response(count)]
        responses = [{"records": [fixtures.album_record()]}] + copy.deepcopy(one_pass) + copy.deepcopy(one_pass)
        service = fixtures.AlbumTests().service(responses)
        service.data = {"dsInfo": {"dsid": "synthetic-account"}}
        service.requires_2fa = service.requires_2sa = False
        service._get_webservice_url = lambda name: fixtures.ROOT

        class Adapter:
            def send(self, *args, **kwargs):
                raise AssertionError("Unexpected transport request")

        modules = {"pyicloud_ipd": SimpleNamespace(PyiCloudService=lambda *args, **kwargs: service),
                   "requests.adapters": SimpleNamespace(HTTPAdapter=Adapter)}
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            run, output = Path(directory), io.StringIO()
            with patch.dict(sys.modules, modules), patch.object(sys, "argv", [
                    "probe.py", "album", "--album", fixtures.NAME, "--expected-assets", str(count)]), \
                    patch("probe.version", return_value=probe.PIN), \
                    patch("probe.sys.stdin.isatty", return_value=True), \
                    patch("probe.sys.stderr.isatty", return_value=True), \
                    patch("builtins.input", return_value="READ ONLY ALBUM"), \
                    patch("probe.getpass.getpass", return_value="fake@example.invalid"), \
                    patch("probe.new_run", return_value=run), \
                    patch("probe.logging.disable"), \
                    patch("probe.discover_zones", return_value=({
                        "private": [{"zone_id": fixtures.ZONE, "deleted": False}], "shared": []}, [])), \
                    redirect_stdout(output):
                self.assertEqual(probe.main(), 0)
            private = json.loads((run / "resource-report-private.json").read_text())
            self.assertEqual(private["account_id"], "synthetic-account")
            baseline = json.loads((run / "album-private.json").read_text())
            self.assertEqual(baseline["account_id"], private["account_id"])
            self.assertEqual(private["database"], "private")
            self.assertEqual(private["zone_id"], fixtures.ZONE)
            self.assertEqual(private["album_id"], fixtures.ALBUM)
            self.assertTrue(private["metadata_passes_match"])
            self.assertEqual(len(private["resources"]), 12 + (count == 5))
            if count == 5:
                added = next(asset for asset in baseline["passes"][-1]["assets"]
                             if asset["recordName"] == "full-asset-new-upload")
                self.assertEqual(added["fields"]["assetDate"], 978404645000)
                self.assertEqual(added["fields"]["addedDate"], 1792108800000)
            self.assertNotIn("SECRET", json.dumps(private))
            summary = json.loads((run / "summary.json").read_text())
            self.assertTrue(summary["account_binding_recorded"])
            self.assertNotIn("account_id", summary)
            self.assertTrue(summary["album_metadata_completed"])
            self.assertEqual(summary["observed_assets"], count)
            self.assertFalse(summary["media_download_requested"])
            self.assertEqual(summary["request_counts"]["original_download_requests"], 0)
            for secret in ("full-asset", "full-master", "SECRET", "synthetic-account", "fake@example.invalid"):
                self.assertNotIn(secret, output.getvalue())


if __name__ == "__main__":
    unittest.main()
