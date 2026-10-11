"""Album scope, pagination and privacy checks using synthetic responses only."""

import base64
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from album import (ALBUM_KEYS, AlbumBlocked, album_count, collect_album, collect_pass,
                   find_album, list_body, query_kind, safe_record)
from probe import PIN, ProbeBlocked, request_guard, reuse_cookies, save_new

ROOT = "https://p123-ckdatabasews.icloud.com"
PRIVATE = ROOT + "/database/1/com.apple.photos.cloud/production/private"
ZONE = {"zoneName": "PrimarySync", "ownerRecordName": "synthetic-owner"}
ALBUM = "synthetic-full-album-id"
NAME = "Synthetic album"


def album_record(name=NAME, identity=ALBUM):
    return {"recordName": identity, "recordType": "CPLAlbum", "recordChangeTag": "album-tag",
            "fields": {"albumNameEnc": {"value": base64.b64encode(name.encode()).decode()}}}


def pair(suffix="1"):
    asset = {"recordType": "CPLAsset", "recordName": "full-asset-" + suffix, "recordChangeTag": "asset-tag",
             "fields": {"masterRef": {"value": {"recordName": "full-master-" + suffix}},
                        "captionEnc": {"value": "SECRET_CAPTION"}}}
    master = {"recordType": "CPLMaster", "recordName": "full-master-" + suffix,
              "recordChangeTag": "master-tag", "fields": {
                  "resOriginalRes": {"value": {"size": 7, "fileChecksum": "opaque-checksum",
                                                "downloadURL": "https://SECRET_SIGNED_URL",
                                                "wrappingKey": "SECRET_KEY"}}}}
    return [asset, master]


def count_response(count=1):
    return {"batch": [{"records": [{"fields": {"itemCount": {"value": count}}}]}]}


def pass_responses(count=1):
    return [count_response(count), {"records": pair(), "syncToken": "opaque-token"},
            {"records": []}, count_response(count)]


class AlbumTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def service(self, values):
        responses = iter(values)
        calls = []
        class Session:
            def post(self, url, **kwargs):
                body = json.loads(kwargs["data"])
                self_kind = query_kind(body, ZONE, ALBUM, url.endswith("/batch"))
                if not self_kind:
                    raise AssertionError("Unscoped synthetic query")
                calls.append(body)
                value = next(responses)
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)
        return SimpleNamespace(session=Session(), params={}, calls=calls)

    def test_album_lookup_is_scoped(self):
        service = self.service([{"records": [album_record("Other", "other"), album_record()]}])
        self.assertEqual(find_album(service, ROOT, ZONE, NAME), ALBUM)
        self.assertEqual(set(service.calls[0]["desiredKeys"]), ALBUM_KEYS)

    def test_invalid_album_lists_are_rejected(self):
        invalid = [{"records": []}, {"records": [album_record(), album_record(identity="other")]},
                   {"records": [album_record()], "moreComing": True},
                   {"records": [{"recordName": "bad", "fields": {"albumNameEnc": {"value": "!"}}}]},
                   {"records": [album_record("Other", str(index)) for index in range(200)]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(AlbumBlocked):
                find_album(self.service([value]), ROOT, ZONE, NAME)

    def test_transport_enforces_album_scope(self):
        calls = []
        class Adapter:
            def send(self, request, **kwargs):
                calls.append(request)
                return "fake-response"
        with patch.dict(sys.modules, {"requests.adapters": SimpleNamespace(HTTPAdapter=Adapter)}):
            with request_guard(True, album=True) as state:
                state.update(photo_root=ROOT, approved_zone=ZONE, approved_album=ALBUM)
                adapter = Adapter()
                body = list_body(ZONE, ALBUM, 0)
                request = SimpleNamespace(method="POST", url=PRIVATE + "/records/query", body=json.dumps(body))
                self.assertEqual(adapter.send(request, verify=True), "fake-response")
                variants = [list_body(ZONE, "different-album", 0), list_body({"zoneName": "PrimarySync"}, ALBUM, 0),
                            list_body(ZONE, ALBUM, 1001), {**body, "desiredKeys": body["desiredKeys"] + ["captionEnc"]},
                            {**body, "query": {"recordType": "CPLAssetByAssetDate"}},
                            {**body, "desiredKeys": [{}]}, {**body, "extra": "unsafe"}]
                for value in variants:
                    request.body = json.dumps(value)
                    with self.subTest(value=value), self.assertRaises(ProbeBlocked):
                        adapter.send(request, verify=True)
                request.body = json.dumps(body)
                for suffix in ("/records/modify", "/records/lookup", "/assets/download", "/zones/delete"):
                    request.url = PRIVATE + suffix
                    with self.subTest(suffix=suffix), self.assertRaises(ProbeBlocked):
                        adapter.send(request, verify=True)
                request.url = "https://p123-content.icloud.com/signed-photo"
                request.method = "GET"
                with self.assertRaises(ProbeBlocked):
                    adapter.send(request, verify=True)
                self.assertEqual(state["asset_metadata_requests"], 1)
        self.assertEqual(len(calls), 1)

    def test_queries_require_metadata_authorization(self):
        body = list_body(ZONE, ALBUM, 0)
        self.assertIsNone(query_kind(body, ZONE, None))
        self.assertIsNone(query_kind(body, None, ALBUM))
        class Adapter:
            def send(self, request, **kwargs):
                raise AssertionError("Unexpected send")
        with patch.dict(sys.modules, {"requests.adapters": SimpleNamespace(HTTPAdapter=Adapter)}):
            with request_guard(True) as state:
                state.update(photo_root=ROOT, approved_zone=ZONE, approved_album=ALBUM)
                with self.assertRaises(ProbeBlocked):
                    Adapter().send(SimpleNamespace(method="POST", url=PRIVATE + "/records/query", body=json.dumps(body)))

    def test_metadata_is_preserved_without_secrets(self):
        value = [safe_record(record) for record in pair()]
        encoded = json.dumps(value)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("captionEnc", encoded)
        self.assertEqual(value[0]["recordName"], "full-asset-1")
        resource = value[1]["fields"]["resOriginalRes"]
        self.assertEqual(resource, {"size": 7, "fileChecksum": "opaque-checksum", "download_url_present": True})

    def test_incomplete_records_are_rejected(self):
        examples = []
        record = pair()[0]
        record.pop("recordChangeTag")
        examples.append(record)
        for key in ("size", "fileChecksum"):
            record = pair()[1]
            record["fields"]["resOriginalRes"]["value"].pop(key)
            examples.append(record)
        examples.append({**pair()[0], "fields": {"masterRef": None}})
        examples.append({**pair()[0], "fields": {"masterRef": {"value": {"recordName": ""}}}})
        for record in examples:
            with self.subTest(record=record), self.assertRaises(AlbumBlocked):
                safe_record(record)

    def test_pagination_retains_progress(self):
        service = self.service(pass_responses())
        progress = []
        result = collect_pass(service, ROOT, ZONE, ALBUM, lambda page, value: progress.append(value))
        self.assertEqual(result["count_before"], 1)
        self.assertEqual(len(progress), 2)
        self.assertFalse(progress[0]["complete"])
        ranks = [body["query"]["filterBy"][0]["fieldValue"]["value"] for body in service.calls if "query" in body]
        self.assertEqual(ranks, [0, 1])
        self.assertNotIn("opaque-token", json.dumps(result))

    def test_inconsistent_inventory_is_rejected(self):
        drift = pass_responses()
        drift[-1] = count_response(2)
        duplicate = [count_response(), {"records": pair()}, {"records": pair()}]
        orphan = pass_responses()
        orphan[1]["records"][0]["fields"]["masterRef"]["value"]["recordName"] = "missing"
        for responses in (drift, duplicate, orphan):
            with self.subTest(responses=responses), self.assertRaises(AlbumBlocked):
                collect_pass(self.service(responses), ROOT, ZONE, ALBUM)

    def test_incomplete_pages_are_rejected(self):
        for records_value in ({"records": [{"serverErrorCode": "CONFLICT"}]},
                              {"records": [], "moreComing": True},
                              {"records": [{"recordType": "Unexpected"}]}):
            with self.subTest(value=records_value), self.assertRaises(AlbumBlocked):
                collect_pass(self.service([count_response(), records_value]), ROOT, ZONE, ALBUM)

    def test_schema_diagnostics_hide_private_values(self):
        response = {"records": [{"recordType": "CPLNewSchema", "recordName": "SECRET_ID"},
                                 {"recordType": "https://SECRET_URL"}]}
        with self.assertRaises(AlbumBlocked) as caught:
            collect_pass(self.service([count_response(), response]), ROOT, ZONE, ALBUM)
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics["record_type_counts"], {"CPLNewSchema": 1, "unrecognized_schema_label": 1})
        self.assertNotIn("SECRET", json.dumps(diagnostics))

    def test_invalid_counts_are_rejected(self):
        for response in (count_response(1001), count_response(True), {"batch": []},
                         {"batch": [{"records": [{"serverErrorCode": "CONFLICT"}]}]}):
            with self.subTest(response=response), self.assertRaises(AlbumBlocked):
                album_count(self.service([response]), ROOT, ZONE, ALBUM)

    def test_matching_passes_do_not_prove_coverage(self):
        responses = [{"records": [album_record()]}] + pass_responses() + pass_responses()
        private, summary = collect_album(self.service(responses), ROOT,
                                         {"private": [{"zone_id": ZONE, "deleted": False}]}, NAME, {})
        self.assertEqual(private["zone_id"], ZONE)
        self.assertTrue(summary["two_passes_match"])
        self.assertEqual(summary["observed_assets"], 1)
        for field in ("inventory_generation_guarantee", "complete_resource_inventory", "content_mapping"):
            self.assertEqual(summary[field], "NOT VERIFIED")

    def test_tag_drift_is_detected(self):
        second = pass_responses()
        second[1]["records"][0]["recordChangeTag"] = "changed"
        responses = [{"records": [album_record()]}] + pass_responses() + second
        _, summary = collect_album(self.service(responses), ROOT,
                                   {"private": [{"zone_id": ZONE, "deleted": None}]}, NAME, {})
        self.assertFalse(summary["two_passes_match"])

    def test_membership_drift_is_detected(self):
        relation = {"recordName": "full-relation", "recordType": "CPLContainerRelation",
                    "recordChangeTag": "relation-tag", "fields": {}}
        first, second = pass_responses(), pass_responses()
        first[1]["records"].append(relation)
        second[1]["records"].append({**relation, "recordChangeTag": "changed-relation"})
        responses = [{"records": [album_record()]}] + first + second
        private, summary = collect_album(self.service(responses), ROOT,
                                         {"private": [{"zone_id": ZONE, "deleted": None}]}, NAME, {})
        self.assertEqual(private["passes"][0]["relations"][0]["recordName"], "full-relation")
        self.assertEqual(summary["observed_album_relations"], 1)
        self.assertFalse(summary["two_passes_match"])

    def test_session_reuse_is_scoped(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as directory:
            repo = Path(directory)
            run = repo / ".smoke" / "cloud-probe-synthetic"
            (run / "cookies").mkdir(parents=True)
            save_new(run / "summary.json", {"schema": 1, "authenticated": True, "provider_version": PIN})
            (run / "cookies" / "sensitive").write_bytes(b"not-json-and-must-not-be-read")
            self.assertEqual(reuse_cookies(repo, run.name), run / "cookies")
            for name in ("../outside", "cloud-probe-../outside", str(run)):
                with self.assertRaises(ProbeBlocked):
                    reuse_cookies(repo, name)
            link = run / "cookies" / "link"
            try:
                link.symlink_to(repo)
            except OSError as error:
                self.skipTest(str(error))
            with self.assertRaises(ProbeBlocked):
                reuse_cookies(repo, run.name)


if __name__ == "__main__":
    unittest.main()
