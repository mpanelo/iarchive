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

    def test_all_variants_keep_owners_types_and_opaque_checksums_without_deduplication(self):
        value = observation(self.records)
        before = copy.deepcopy(value)
        report = build_resource_report(value, self.expectations)
        self.assertEqual(value, before)
        self.assertEqual(len(report["resources"]), 12)
        self.assertEqual(report["summary"]["resource_role_counts"], {
            "original": 4, "original_video_companion": 1, "alternate_original": 1,
            "full_image": 1, "full_video": 1, "sidecar": 1,
            "preview_image": 2, "preview_video": 1,
        })
        self.assertEqual(report["summary"]["expectation_status"], "MATCHED OBSERVED FIELDS")
        raw = next(asset for asset in report["assets"] if asset["asset_id"] == "full-asset-raw")
        resources = [report["resources"][index] for index in raw["resource_indices"]]
        self.assertEqual({item["file_type"] for item in resources}, {"com.adobe.raw-image", "public.jpeg"})
        self.assertTrue(all(item["provider_checksum_opaque"] == "opaque-synthetic-checksum"
                            for item in report["resources"]))
        self.assertTrue(all(item["record_change_tag"] for item in report["resources"]))
        self.assertTrue(all(asset["pair_confidence"] == "unknown" for asset in report["assets"]))
        for key in ("complete_resource_inventory", "content_mapping", "provider_checksum_algorithm"):
            self.assertEqual(report["summary"][key], "NOT VERIFIED")
        self.assertNotIn("SECRET", json.dumps(report))

    def test_missing_motion_raw_companion_render_and_sidecar_are_explicit_findings(self):
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

    def test_previews_cannot_replace_a_missing_original(self):
        self.records[1]["fields"].pop("resOriginalRes")
        report = build_resource_report(observation(self.records))
        self.assertIn("missing_original_resource", report["summary"]["resource_finding_counts"])
        self.assertEqual(report["summary"]["expectation_status"], "NOT PROVIDED")

    def test_raw_and_jpeg_types_can_be_reversed_without_inventing_pair_confidence(self):
        next(record for record in self.records if record["recordName"] == "full-master-raw")["fields"][
            "resOriginalFileType"] = {"value": "public.jpeg"}
        next(record for record in self.records if record["recordName"] == "full-asset-raw")["fields"][
            "resOriginalAltFileType"] = {"value": "com.adobe.raw-image"}
        report = build_resource_report(observation(self.records), self.expectations)
        asset = next(item for item in report["assets"] if item["asset_id"] == "full-asset-raw")
        self.assertEqual(asset["pair_confidence"], "unknown")
        self.assertEqual({report["resources"][index]["file_type"] for index in asset["resource_indices"]},
                         {"public.jpeg", "com.adobe.raw-image"})

    def test_type_or_dimensions_without_companion_resource_are_findings(self):
        self.records[1]["fields"].pop("resOriginalVidComplRes")
        self.records[1]["fields"]["resOriginalVidComplWidth"] = {"value": 1920}
        report = build_resource_report(observation(self.records))
        missing = [item for item in report["findings"] if item["code"] == "metadata_without_resource"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["resource_field"], "resOriginalVidComplRes")
        self.assertEqual(report["summary"]["observed_resources"], 11)

    def test_same_field_on_asset_and_master_is_retained_and_ambiguous(self):
        add_resource(self.records[0], "resOriginal", "public.heic")
        report = build_resource_report(observation(self.records), self.expectations)
        originals = [item for item in report["resources"] if item["resource_field"] == "resOriginalRes"
                     and "full-asset-live" in item["asset_ids"]]
        self.assertEqual({item["record_type"] for item in originals}, {"CPLAsset", "CPLMaster"})
        self.assertIn("ambiguous_resource_ownership", report["summary"]["resource_finding_counts"])
        self.assertEqual(report["summary"]["expectation_status"], "FINDINGS")

    def test_shared_and_unreferenced_masters_are_findings_without_losing_resources(self):
        value = observation(self.records)
        value["assets"][1]["fields"]["masterRef"]["recordName"] = "full-master-live"
        report = build_resource_report(value)
        self.assertEqual(len(report["resources"]), 12)
        self.assertEqual(report["summary"]["resource_finding_counts"]["shared_master"], 2)
        self.assertEqual(report["summary"]["resource_finding_counts"]["unreferenced_master"], 1)
        shared = next(item for item in report["resources"] if item["record_id"] == "full-master-live")
        self.assertEqual(shared["asset_ids"], ["full-asset-live", "full-asset-video"])

    def test_missing_master_is_not_inferred_from_filename_or_checksum(self):
        value = observation(self.records)
        value["assets"][0]["fields"]["masterRef"] = {"recordName": "missing-master"}
        report = build_resource_report(value)
        self.assertIn("missing_master_reference", report["summary"]["resource_finding_counts"])
        self.assertEqual(len(report["resources"]), 12)

    def test_unrequested_resource_is_preserved_with_unknown_role_and_no_secrets(self):
        add_resource(self.records[0], "resFutureVariant", "com.vendor.unknown")
        report = build_resource_report(observation(self.records), self.expectations)
        unknown = next(item for item in report["resources"] if item["resource_field"] == "resFutureVariantRes")
        self.assertEqual(unknown["role"], "unknown")
        self.assertEqual(unknown["file_type"], "com.vendor.unknown")
        self.assertIn("unknown_resource_field", report["summary"]["resource_finding_counts"])
        self.assertIn("unexpected_resource", report["summary"]["resource_finding_counts"])
        self.assertNotIn("SECRET", json.dumps(report))

    def test_zero_size_missing_url_and_missing_type_remain_inventory_entries(self):
        add_resource(self.records[0], "resSidecar", "public.xml", size=0)
        value = self.records[0]["fields"]
        value["resSidecarRes"]["value"].pop("downloadURL")
        value.pop("resSidecarFileType")
        report = build_resource_report(observation(self.records))
        self.assertEqual(len(report["resources"]), 13)
        counts = report["summary"]["resource_finding_counts"]
        self.assertTrue({"empty_resource", "resource_url_unavailable", "missing_file_type"} <= set(counts))

    def test_expectations_require_full_known_ids_and_unique_valid_field_names(self):
        value = observation(self.records)
        for expectations in ([], {"full-asset": ["resOriginalRes"]},
                             {"full-asset-live": []}, {"full-asset-live": "resOriginalRes"},
                             {"full-asset-live": ["resOriginalRes", "resOriginalRes"]},
                             {"full-asset-live": ["../escape"]}):
            with self.subTest(expectations=expectations), self.assertRaises(ResourceReportBlocked):
                build_resource_report(value, expectations)

    def test_duplicate_record_ids_and_missing_tags_block_reporting(self):
        for mutation in (lambda value: value["masters"].append(value["masters"][0]),
                         lambda value: value["assets"][0].pop("recordChangeTag")):
            value = observation(self.records)
            mutation(value)
            with self.assertRaises(ResourceReportBlocked):
                build_resource_report(value)

    def test_matching_expectations_for_one_asset_do_not_cover_the_other_assets(self):
        report = build_resource_report(observation(self.records), {
            "full-asset-live": self.expectations["full-asset-live"]})
        self.assertEqual(report["summary"]["expectation_asset_count"], 1)
        self.assertEqual(report["summary"]["complete_resource_inventory"], "NOT VERIFIED")

    def test_unknown_resource_drift_is_compared_and_only_counts_enter_summary(self):
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

    def test_retaining_unsolicited_fields_does_not_expand_the_query_allowlist(self):
        body = list_body(fixtures.ZONE, fixtures.ALBUM, 0)
        body["desiredKeys"].append("resFutureVariantRes")
        self.assertIsNone(query_kind(body, fixtures.ZONE, fixtures.ALBUM))

    def test_malformed_unrequested_resources_stop_sanitization(self):
        for field, value in (("resFutureRes", {"value": {"size": 7}}),
                             ("res../escapeRes", {"value": resource("public.jpeg")["Res"]["value"]})):
            record = fixtures.pair()[0]
            record["fields"][field] = value
            with self.subTest(field=field), self.assertRaises(AlbumBlocked):
                safe_record(record)

    def test_album_entry_point_writes_private_report_without_downloads_or_public_ids(self):
        one_pass = [fixtures.count_response(4), {"records": self.records},
                    {"records": []}, fixtures.count_response(4)]
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
                    "probe.py", "album", "--album", fixtures.NAME]), \
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
            self.assertEqual(private["database"], "private")
            self.assertEqual(private["zone_id"], fixtures.ZONE)
            self.assertEqual(private["album_id"], fixtures.ALBUM)
            self.assertTrue(private["metadata_passes_match"])
            self.assertEqual(len(private["resources"]), 12)
            self.assertNotIn("SECRET", json.dumps(private))
            summary = json.loads((run / "summary.json").read_text())
            self.assertTrue(summary["album_metadata_completed"])
            self.assertFalse(summary["media_download_requested"])
            self.assertEqual(summary["request_counts"]["original_download_requests"], 0)
            for secret in ("full-asset", "full-master", "SECRET", "synthetic-account", "fake@example.invalid"):
                self.assertNotIn(secret, output.getvalue())


if __name__ == "__main__":
    unittest.main()
