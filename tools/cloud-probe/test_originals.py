"""Frozen download selection and independently hashed copies; synthetic bytes only."""

import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from album import collect_album
from originals import (DownloadBlocked, hash_file, run_downloads, select_originals,
                       stream_original, valid_media_url)
from probe import PIN, ProbeBlocked, read_baseline, request_guard, save_new
import test_album as fixtures
from test_album import ALBUM, NAME, ROOT, ZONE, album_record, pass_responses


URLS = [f"https://cvws.icloud-content.com/synthetic-original-{number}?token=SECRET_URL" for number in (1, 2)]


def inventory():
    responses = [{"records": [album_record()]}]
    one = pass_responses(2)
    two_pair = copy.deepcopy(one[1]["records"])
    for record in two_pair:
        record["recordName"] = record["recordName"].replace("-1", "-2")
        if "masterRef" in record["fields"]:
            record["fields"]["masterRef"]["value"]["recordName"] = "full-master-2"
    one[1]["records"] += two_pair
    responses += copy.deepcopy(one) + copy.deepcopy(one)
    private, _ = collect_album(fixtures.AlbumTests().service(responses), ROOT,
                               {"private": [{"zone_id": ZONE, "deleted": False}]}, NAME, {})
    urls = {("CPLMaster", f"full-master-{number}"): url for number, url in enumerate(URLS, 1)}
    return private, urls


class Response:
    def __init__(self, chunks=None, status=200, headers=None):
        self.chunks = [b"fixture"] if chunks is None else chunks
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/octet-stream"}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("SECRET_HTTP_MESSAGE")

    def iter_content(self, **kwargs):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class OriginalTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temporary.cleanup)
        self.run = Path(self.temporary.name)
        self.private, self.urls = inventory()

    def plans(self):
        return select_originals(self.private, self.private, self.urls, "synthetic-account")

    def test_selection_binds_full_namespace_ids_originals_and_opaque_checksums(self):
        plans = self.plans()
        self.assertEqual(len(plans), 2)
        first = plans[0]["binding"]
        self.assertEqual(first["account_id"], "synthetic-account")
        self.assertEqual(first["database"], "private")
        self.assertEqual(first["zone_id"], ZONE)
        self.assertEqual(first["album_id"], ALBUM)
        self.assertEqual(first["asset_id"], "full-asset-1")
        self.assertEqual(first["master_id"], "full-master-1")
        self.assertEqual(first["resource_field"], "resOriginalRes")
        self.assertEqual(first["provider_checksum_opaque"], "opaque-checksum")
        self.assertNotIn("SECRET", json.dumps(first))

    def test_new_assets_changed_tags_resources_and_wrong_scope_block(self):
        variants = []
        for mutation in (lambda value: value.update(album_id="different-album"),
                         lambda value: value.update(zone_id={"zoneName": "wrong"}),
                         lambda value: value["passes"][-1]["assets"][0].update(recordName="new-upload"),
                         lambda value: value["passes"][-1]["masters"][0].update(recordChangeTag="changed"),
                         lambda value: value["passes"][-1]["masters"][0]["fields"]["resOriginalRes"].update(size=8)):
            current = copy.deepcopy(self.private)
            mutation(current)
            variants.append(current)
        for current in variants:
            with self.subTest(current=current), self.assertRaises(DownloadBlocked):
                select_originals(self.private, current, self.urls, "synthetic-account")

    def test_only_two_assets_and_unambiguous_resource_owners_are_accepted(self):
        for mutation in (lambda value: value["passes"][-1]["assets"].pop(),
                         lambda value: value["passes"][-1]["masters"][0]["fields"].pop("resOriginalRes"),
                         lambda value: value["passes"][-1]["assets"][0]["fields"].update(
                             resOriginalRes=value["passes"][-1]["masters"][0]["fields"]["resOriginalRes"])):
            value = copy.deepcopy(self.private)
            mutation(value)
            with self.assertRaises(DownloadBlocked):
                select_originals(value, value, self.urls, "synthetic-account")
        with self.assertRaises(DownloadBlocked):
            select_originals(self.private, self.private, self.urls, None)

    def test_missing_duplicate_unsafe_and_oversized_resources_block(self):
        for urls in ({}, {key: URLS[0] for key in self.urls},
                     {key: "https://evil.example/download" for key in self.urls}):
            with self.subTest(urls=urls), self.assertRaises(DownloadBlocked):
                select_originals(self.private, self.private, urls, "synthetic-account")
        value = copy.deepcopy(self.private)
        value["passes"][-1]["masters"][0]["fields"]["resOriginalRes"]["size"] = 256 * 1024 * 1024 + 1
        with self.assertRaises(DownloadBlocked):
            select_originals(value, value, self.urls, "synthetic-account")

    def test_media_url_requires_exact_trusted_https_shape(self):
        self.assertTrue(valid_media_url(URLS[0]))
        for url in ("http://cvws.icloud-content.com/a", "https://icloud.com.evil.example/a",
                    "https://user:pass@cvws.icloud-content.com/a", "https://cvws.icloud-content.com:444/a",
                    URLS[0] + "#fragment", URLS[0] + "\n", "https://evil.example/a"):
            self.assertFalse(valid_media_url(url))

    def test_guard_allows_exact_original_once_and_rejects_other_media_and_redirects(self):
        calls = []
        class Adapter:
            def send(self, request, **kwargs):
                calls.append(request.url)
                return "fake-response"
        with patch.dict(sys.modules, {"requests.adapters": SimpleNamespace(HTTPAdapter=Adapter)}):
            with request_guard(True, True, True) as state:
                state["approved_download_urls"] = {URLS[0]}
                adapter = Adapter()
                for url in (URLS[1], URLS[0] + "&redirect=1", "https://cvws.icloud-content.com/preview"):
                    with self.subTest(url=url), self.assertRaises(ProbeBlocked):
                        adapter.send(SimpleNamespace(method="GET", url=url, body=None), verify=True)
                request = SimpleNamespace(method="GET", url=URLS[0], body=None)
                with self.assertRaises(ProbeBlocked):
                    adapter.send(request, verify=False)
                request.method = "POST"
                with self.assertRaises(ProbeBlocked):
                    adapter.send(request, verify=True)
                request.method = "GET"
                self.assertEqual(adapter.send(request, verify=True), "fake-response")
                with self.assertRaises(ProbeBlocked):
                    adapter.send(request, verify=True)
                self.assertEqual(state["original_download_requests"], 1)
            with request_guard(True, True) as state:
                state["approved_download_urls"] = {URLS[0]}
                with self.assertRaises(ProbeBlocked):
                    Adapter().send(request, verify=True)
        self.assertEqual(calls, [URLS[0]])

    def test_success_independently_reads_both_files_and_never_persists_urls(self):
        requests = []
        def fetch(session, url, start):
            requests.append((url, start))
            return Response()
        result = run_downloads(None, self.plans(), self.run, {}, save_new, fetch)
        self.assertEqual(requests, [(url, 0) for url in URLS])
        self.assertEqual(result["downloaded_originals"], 2)
        self.assertEqual(result["downloaded_original_bytes"], 14)
        self.assertTrue(result["independent_local_hashes_match"])
        self.assertEqual(result["physical_drive_independence"], "NOT VERIFIED")
        self.assertEqual(result["provider_checksum_algorithm"], "NOT VERIFIED")
        expected = hashlib.sha256(b"fixture").hexdigest()
        for path in self.run.glob("copy-*/*.bin"):
            self.assertEqual(hash_file(path), (7, expected))
        for path in self.run.glob("*.json"):
            self.assertNotIn("SECRET_URL", path.read_text())

    def test_truncated_oversized_error_documents_and_partial_ranges_block(self):
        responses = [Response([b"short"]), Response([b"too-many-bytes"]),
                     Response(headers={"Content-Type": "application/json"}), Response(status=204),
                     Response(status=206, headers={"Content-Range": "bytes 1-6/7"})]
        for number, response in enumerate(responses):
            progress = []
            with self.subTest(number=number), self.assertRaises(DownloadBlocked):
                stream_original(None, URLS[0], self.run / f"bad-{number}.bin", 7,
                                lambda *args: response, progress.append)
            self.assertTrue(response.closed)
            self.assertFalse(progress[0]["download_complete"])
        good = Response(status=206, headers={"Content-Range": "bytes 0-6/7"})
        self.assertEqual(stream_original(None, URLS[0], self.run / "range.bin", 7, lambda *args: good),
                         hashlib.sha256(b"fixture").hexdigest())

    def test_interruption_retains_bytes_progress_and_invalidates_completion(self):
        response = Response([b"fix", KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            run_downloads(None, self.plans(), self.run, {}, save_new, lambda *args: response)
        self.assertEqual((self.run / "copy-a/original-01.bin").read_bytes(), b"fix")
        progress = json.loads((self.run / "download-01-progress-private.json").read_text())
        self.assertEqual(progress["downloaded_bytes"], 3)
        self.assertFalse(progress["download_complete"])
        self.assertFalse((self.run / "download-manifest-private.json").exists())

    def test_copy_corruption_blocks_success_despite_matching_size(self):
        original_hash = hash_file
        def tampered_hash(path):
            if path.parent.name == "copy-b":
                path.write_bytes(b"changed")
            return original_hash(path)
        with patch("originals.hash_file", side_effect=tampered_hash), self.assertRaises(DownloadBlocked):
            run_downloads(None, self.plans(), self.run, {}, save_new, lambda *args: Response())
        self.assertFalse((self.run / "download-manifest-private.json").exists())

    def test_new_files_are_exclusive_and_links_are_rejected(self):
        path = self.run / "existing.bin"
        path.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            stream_original(None, URLS[0], path, 7, lambda *args: Response())
        self.assertEqual(path.read_bytes(), b"existing")
        try:
            (self.run / "linked.bin").symlink_to(path)
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaises(DownloadBlocked):
            hash_file(self.run / "linked.bin")

    def test_baseline_requires_completed_matching_two_asset_metadata(self):
        run = self.run / ".smoke/cloud-probe-synthetic"
        (run / "cookies").mkdir(parents=True)
        summary = {"schema": 1, "authenticated": True, "provider_version": PIN, "scope": "album",
                   "album_metadata_completed": True, "two_passes_match": True, "observed_assets": 2}
        save_new(run / "summary.json", summary)
        save_new(run / "album-private.json", self.private)
        self.assertEqual(read_baseline(self.run, run.name), self.private)
        summary["album_metadata_completed"] = False
        (run / "summary.json").write_text(json.dumps(summary))
        with self.assertRaises(ProbeBlocked):
            read_baseline(self.run, run.name)

    def test_album_captures_original_urls_only_in_memory(self):
        responses = [{"records": [album_record()]}] + pass_responses() + pass_responses()
        urls = {}
        private, _ = collect_album(fixtures.AlbumTests().service(responses), ROOT,
                                   {"private": [{"zone_id": ZONE, "deleted": False}]}, NAME, {},
                                   original_urls=urls)
        self.assertEqual(urls, {("CPLMaster", "full-master-1"): "https://SECRET_SIGNED_URL"})
        self.assertNotIn("SECRET", json.dumps(private))


if __name__ == "__main__":
    unittest.main()
