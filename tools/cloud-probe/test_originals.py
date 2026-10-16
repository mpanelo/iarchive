"""Frozen download selection and independently hashed copies; synthetic bytes only."""

import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from album import collect_album
from originals import (DownloadBlocked, VARIANT_COUNTS, hash_file, run_downloads,
                       select_originals, select_variants, stream_resource, valid_media_url)
from probe import PIN, ProbeBlocked, read_baseline, request_guard, save_new
import probe
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
    for record in one[1]["records"]:
        if record["recordType"] == "CPLMaster":
            record["fields"]["resOriginalFileType"] = {"value": "public.heic"}
    responses += copy.deepcopy(one) + copy.deepcopy(one)
    private, _ = collect_album(fixtures.AlbumTests().service(responses), ROOT,
                               {"private": [{"zone_id": ZONE, "deleted": False}]}, NAME, {})
    private.update(account_id="synthetic-account", database="private")
    urls = {("CPLMaster", f"full-master-{number}", "resOriginalRes"): url for number, url in enumerate(URLS, 1)}
    return private, urls


def variant_inventory():
    from test_resources import add_resource, variant_records
    records, _ = variant_records()
    # Preserve a preview ownership finding while selecting other exact fields.
    add_resource(records[1], "resJPEGThumb", "public.jpeg")
    for number, record in enumerate(records):
        for field, entry in record["fields"].items():
            if field.endswith("Res"):
                entry["value"]["downloadURL"] = f"https://cvws.icloud-content.com/fixture-{number}-{field}"
    one = [fixtures.count_response(4), {"records": records}, {"records": []}, fixtures.count_response(4)]
    responses = [{"records": [album_record()]}] + copy.deepcopy(one) + copy.deepcopy(one)
    urls = {}
    private, _ = collect_album(fixtures.AlbumTests().service(responses), ROOT,
                               {"private": [{"zone_id": ZONE, "deleted": False}]}, NAME, {},
                               resource_urls=urls, download_fields=tuple(VARIANT_COUNTS), expected_count=4)
    private.update(account_id="synthetic-account", database="private")
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

    def test_selection_preserves_resource_bindings(self):
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

    def test_variant_selection_preserves_bindings(self):
        private, urls = variant_inventory()
        plans = select_variants(private, private, urls, "synthetic-account")
        self.assertEqual(len(plans), 6)
        self.assertEqual(len(urls), 6)
        owners = {(r['recordType'], r['recordName']): r for r in
                  (*private['passes'][-1]['assets'], *private['passes'][-1]['masters'])}
        for plan in plans:
            binding = plan['binding']
            owner = owners[(binding['resource_record_type'], binding['resource_record_id'])]
            field = binding['resource_field']
            self.assertEqual(plan['url'], urls[(owner['recordType'], owner['recordName'], field)])
            self.assertEqual(binding['resource_record_change_tag'], owner['recordChangeTag'])
            self.assertEqual(binding['resource_file_type'], owner['fields'][field.removesuffix('Res') + 'FileType'])
            self.assertEqual(binding['resource_size'], owner['fields'][field]['size'])
            self.assertEqual(binding['provider_checksum_opaque'], owner['fields'][field]['fileChecksum'])
        self.assertEqual(private['resource_report']['summary']['resource_finding_counts'],
                         {'ambiguous_resource_ownership': 1})

    def test_variant_scope_rejects_changes(self):
        private, urls = variant_inventory()
        for field in ('resOriginalVidComplRes', 'resJPEGFullRes'):
            for duplicate in (False, True):
                value = copy.deepcopy(private)
                now = value['passes'][-1]
                owner = next(r for r in (*now['assets'], *now['masters']) if field in r['fields'])
                if duplicate:
                    other = (next(r for r in now['assets'] if r['fields']['masterRef']['recordName'] == owner['recordName'])
                             if owner['recordType'] == 'CPLMaster' else
                             next(r for r in now['masters'] if r['recordName'] == owner['fields']['masterRef']['recordName']))
                    other['fields'][field] = copy.deepcopy(owner['fields'][field])
                else:
                    del owner['fields'][field]
                with self.subTest(field=field, duplicate=duplicate), self.assertRaisesRegex(
                        DownloadBlocked, 'ownership|counts'):
                    select_variants(value, value, urls, 'synthetic-account')
        for size, message in ((32 * 1024 * 1024 + 1, 'Resource size'), (16 * 1024 * 1024, 'total test byte')):
            value = copy.deepcopy(private)
            for r in (*value['passes'][-1]['assets'], *value['passes'][-1]['masters']):
                for field in VARIANT_COUNTS:
                    if field in r['fields']:
                        r['fields'][field]['size'] = size
            with self.subTest(size=size), self.assertRaisesRegex(DownloadBlocked, message):
                select_variants(value, value, urls, 'synthetic-account')

    def test_variant_download_round_trip(self):
        from requests import Response as HttpResponse, Session
        from requests.adapters import HTTPAdapter

        private, urls = variant_inventory()
        baseline = self.run / '.smoke/cloud-probe-baseline'
        (baseline / 'cookies').mkdir(parents=True)
        save_new(baseline / 'summary.json', {'schema': 1, 'authenticated': True, 'provider_version': PIN,
                 'scope': 'album', 'album_metadata_completed': True, 'two_passes_match': True,
                 'account_binding_recorded': True, 'observed_assets': 4})
        save_new(baseline / 'album-private.json', private)
        with self.assertRaisesRegex(ProbeBlocked, 'required asset count'):
            read_baseline(self.run, baseline.name)
        self.assertEqual(read_baseline(self.run, baseline.name, 4), private)
        raw_records = []
        for record in (*private['passes'][-1]['assets'], *private['passes'][-1]['masters']):
            raw = {key: record[key] for key in ('recordType', 'recordName', 'recordChangeTag')}
            raw['fields'] = {field: {'value': copy.deepcopy(value)} for field, value in record['fields'].items()}
            for field in VARIANT_COUNTS:
                key = (record['recordType'], record['recordName'], field)
                if key in urls:
                    raw['fields'][field]['value']['downloadURL'] = urls[key]
            # Unselected previews still advertise URLs in both observations.
            for field, entry in raw['fields'].items():
                if field.endswith('Res'):
                    entry['value'].setdefault('downloadURL', 'https://cvws.icloud-content.com/unapproved-preview')
            raw_records.append(raw)
        one = [fixtures.count_response(4), {'records': raw_records}, {'records': []}, fixtures.count_response(4)]
        for scenario in ('complete', 'drift', 'wrong-account'):
            drift = scenario == 'drift'
            with self.subTest(scenario=scenario), Session() as session:
                final = copy.deepcopy(one)
                if drift:
                    final[1]['records'][0]['recordChangeTag'] = 'post-download-change'
                service = fixtures.AlbumTests().service(
                    [{'records': [album_record()]}] + copy.deepcopy(one) * 2 + final)
                session.post = service.session.post
                service.session = session
                service.data = {'dsInfo': {'dsid': 'different-account' if scenario == 'wrong-account' else 'synthetic-account'}}
                service.requires_2fa = service.requires_2sa = False
                service._get_webservice_url = lambda name: ROOT
                run = self.run / (scenario + '-run')
                run.mkdir()
                output, calls = io.StringIO(), []

                def send(adapter, request, **kwargs):
                    calls.append(request.url)
                    response = HttpResponse()
                    response.status_code = 200
                    response.headers['Content-Type'] = 'application/octet-stream'
                    response.raw = io.BytesIO(b'fixture')
                    response.request, response.url = request, request.url
                    return response

                modules = {'pyicloud_ipd': SimpleNamespace(PyiCloudService=lambda *args, **kwargs: service)}
                with patch.dict(sys.modules, modules), patch.object(HTTPAdapter, 'send', send), \
                        patch.object(sys, 'argv', ['probe.py', 'variants', '--album', NAME, '--baseline', baseline.name]), \
                        patch('probe.version', return_value=PIN), \
                        patch('probe.sys.stdin.isatty', return_value=True), \
                        patch('probe.sys.stderr.isatty', return_value=True), \
                        patch('builtins.input', return_value='DOWNLOAD SIX RESOURCES'), \
                        patch('probe.getpass.getpass', return_value='fake@example.invalid'), \
                        patch('probe.new_run', return_value=run), patch('probe.logging.disable'), \
                        patch('probe.read_baseline', side_effect=lambda repo, name, count: read_baseline(self.run, name, count)), \
                        patch('probe.discover_zones', return_value=({'private': [{'zone_id': ZONE, 'deleted': False}], 'shared': []}, [])) as discovery, \
                        redirect_stdout(output):
                    self.assertEqual(probe.main(), int(scenario != 'complete'))
                summary = json.loads((run / 'summary.json').read_text())
                if scenario == 'wrong-account':
                    discovery.assert_not_called()
                    self.assertEqual(service.calls, [])
                    self.assertEqual(calls, [])
                    self.assertIn('different authenticated account', summary['finding'])
                    self.assertFalse((run / 'album-private.json').exists())
                    self.assertNotIn('different-account', output.getvalue())
                    continue
                self.assertEqual(set(calls), set(urls.values()))
                self.assertEqual(len(calls), 6)
                self.assertEqual(summary['request_counts']['original_download_requests'], 6)
                self.assertEqual(summary['downloaded_resources'], 6)
                self.assertEqual(summary['downloaded_resource_bytes'], 42)
                self.assertEqual(summary['downloaded_originals'], 4)
                self.assertEqual(summary['resource_download_completed'], not drift)
                self.assertFalse(summary['original_download_completed'])
                if drift:
                    self.assertEqual(summary['finding'], 'Album metadata changed during the resource download test')
                for secret in ('full-asset', 'full-master', 'synthetic-account', 'fake@example.invalid'):
                    self.assertNotIn(secret, output.getvalue())

    def test_normalized_urls_cannot_collide(self):
        plans = self.plans()
        plans[0]['url'] = 'https://cvws.icloud-content.com/fixture%7e'
        plans[1]['url'] = 'https://cvws.icloud-content.com/fixture~'
        with patch('pyicloud_ipd.services.photos.download_asset') as fetch, \
                self.assertRaisesRegex(DownloadBlocked, 'collide after request normalization'):
            run_downloads(None, plans, self.run, {}, save_new)
        fetch.assert_not_called()

    def test_baseline_drift_blocks_selection(self):
        mutations = (lambda value: value.update(album_id="different-album"),
                         lambda value: value.update(zone_id={"zoneName": "wrong"}),
                         lambda value: value["zone_id"].update(ownerRecordName="different-owner"),
                         lambda value: value["passes"][-1]["assets"][0].update(recordName="new-upload"),
                         lambda value: value["passes"][-1]["assets"][0].update(recordChangeTag="changed"),
                         lambda value: value["passes"][-1]["masters"][0].update(recordChangeTag="changed"),
                         lambda value: value["passes"][-1]["masters"][0]["fields"]["resOriginalRes"].update(size=8))
        for factory, select in ((inventory, select_originals), (variant_inventory, select_variants)):
            private, urls = factory()
            for number, mutation in enumerate(mutations):
                current = copy.deepcopy(private)
                mutation(current)
                with self.subTest(scope=select.__name__, mutation=number), self.assertRaisesRegex(
                        DownloadBlocked, 'Approved identities or resource metadata changed'):
                    select(private, current, urls, "synthetic-account")

    def test_account_binding_blocks_selection(self):
        for field, value in (("account_id", None), ("account_id", "different-account"),
                             ("database", "shared")):
            for side in ("baseline", "current"):
                baseline, current = copy.deepcopy(self.private), copy.deepcopy(self.private)
                (baseline if side == "baseline" else current)[field] = value
                with self.subTest(side=side, field=field), self.assertRaisesRegex(DownloadBlocked, "account binding"):
                    select_originals(baseline, current, self.urls, "synthetic-account")

    def test_selection_requires_unambiguous_owners(self):
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

    def test_invalid_resources_block_selection(self):
        for urls in ({}, {key: URLS[0] for key in self.urls},
                     {key: "https://evil.example/download" for key in self.urls}):
            with self.subTest(urls=urls), self.assertRaises(DownloadBlocked):
                select_originals(self.private, self.private, urls, "synthetic-account")
        mutations = (
            lambda fields: fields['resOriginalRes'].update(size=256 * 1024 * 1024 + 1),
            lambda fields: fields['resOriginalRes'].update(size=0),
            lambda fields: fields['resOriginalRes'].update(size=True),
            lambda fields: fields['resOriginalRes'].update(fileChecksum=''),
            lambda fields: fields.pop('resOriginalFileType'),
        )
        for number, mutation in enumerate(mutations):
            value = copy.deepcopy(self.private)
            mutation(value['passes'][-1]['masters'][0]['fields'])
            with self.subTest(resource_mutation=number), self.assertRaisesRegex(
                    DownloadBlocked, 'Resource size|type or opaque checksum'):
                select_originals(value, value, self.urls, "synthetic-account")

    def test_media_url_validation(self):
        self.assertTrue(valid_media_url(URLS[0]))
        for url in ("http://cvws.icloud-content.com/a", "https://icloud.com.evil.example/a",
                    "https://user:pass@cvws.icloud-content.com/a", "https://cvws.icloud-content.com:444/a",
                    URLS[0] + "#fragment", URLS[0] + "\n", "https://evil.example/a"):
            self.assertFalse(valid_media_url(url))

    def test_download_allowlist_is_single_use(self):
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

    def test_download_copies_are_independently_verified(self):
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
        copies = list(self.run.glob("copy-*/*"))
        self.assertEqual(len(copies), 4)
        for path in copies:
            self.assertEqual(hash_file(path), (7, expected))
        for path in self.run.glob("*.json"):
            self.assertNotIn("SECRET_URL", path.read_text())

    def test_download_response_validation(self):
        responses = [Response([b"short"]), Response([b"too-many-bytes"]),
                     Response(headers={"Content-Type": "application/json"}), Response(status=204),
                     Response(status=206, headers={"Content-Range": "bytes 1-6/7"})]
        for number, response in enumerate(responses):
            progress = []
            with self.subTest(number=number), self.assertRaises(DownloadBlocked):
                stream_resource(None, URLS[0], self.run / f"bad-{number}.bin", 7,
                                lambda *args: response, progress.append)
            self.assertTrue(response.closed)
            self.assertFalse(progress[0]["download_complete"])
        good = Response(status=206, headers={"Content-Range": "bytes 0-6/7"})
        self.assertEqual(stream_resource(None, URLS[0], self.run / "range.bin", 7, lambda *args: good),
                         hashlib.sha256(b"fixture").hexdigest())

    def test_interrupted_download_retains_progress(self):
        response = Response([b"fix", KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            run_downloads(None, self.plans(), self.run, {}, save_new, lambda *args: response)
        self.assertEqual((self.run / "copy-a/resource-01.heic").read_bytes(), b"fix")
        progress = json.loads((self.run / "download-01-progress-private.json").read_text())
        self.assertEqual(progress["downloaded_bytes"], 3)
        self.assertFalse(progress["download_complete"])
        self.assertFalse((self.run / "download-manifest-private.json").exists())

    def test_copy_corruption_blocks_completion(self):
        original_hash = hash_file
        def tampered_hash(path):
            if path.parent.name == "copy-b":
                path.write_bytes(b"changed")
            return original_hash(path)
        with patch("originals.hash_file", side_effect=tampered_hash), self.assertRaises(DownloadBlocked):
            run_downloads(None, self.plans(), self.run, {}, save_new, lambda *args: Response())
        self.assertFalse((self.run / "download-manifest-private.json").exists())

    def test_download_file_guards(self):
        path = self.run / "existing.bin"
        path.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            stream_resource(None, URLS[0], path, 7, lambda *args: Response())
        self.assertEqual(path.read_bytes(), b"existing")
        try:
            (self.run / "linked.bin").symlink_to(path)
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaises(DownloadBlocked):
            hash_file(self.run / "linked.bin")

    def test_baseline_requires_completed_metadata(self):
        run = self.run / ".smoke/cloud-probe-synthetic"
        (run / "cookies").mkdir(parents=True)
        summary = {"schema": 1, "authenticated": True, "provider_version": PIN, "scope": "album",
                   "album_metadata_completed": True, "two_passes_match": True, "observed_assets": 2,
                   "account_binding_recorded": True}
        save_new(run / "summary.json", summary)
        save_new(run / "album-private.json", self.private)
        self.assertEqual(read_baseline(self.run, run.name), self.private)
        for target, field in ((summary, "account_binding_recorded"), (self.private, "account_id")):
            original = target.pop(field)
            (run / "summary.json").write_text(json.dumps(summary))
            (run / "album-private.json").write_text(json.dumps(self.private))
            with self.subTest(missing=field), self.assertRaisesRegex(ProbeBlocked, "fresh metadata baseline"):
                read_baseline(self.run, run.name)
            target[field] = original
        (run / "album-private.json").write_text(json.dumps(self.private))
        summary["album_metadata_completed"] = False
        (run / "summary.json").write_text(json.dumps(summary))
        with self.assertRaises(ProbeBlocked):
            read_baseline(self.run, run.name)

    def test_original_urls_stay_in_memory(self):
        responses = [{"records": [album_record()]}] + pass_responses() + pass_responses()
        urls = {}
        private, _ = collect_album(fixtures.AlbumTests().service(responses), ROOT,
                                   {"private": [{"zone_id": ZONE, "deleted": False}]}, NAME, {},
                                   resource_urls=urls)
        self.assertEqual(urls, {("CPLMaster", "full-master-1", "resOriginalRes"): "https://SECRET_SIGNED_URL"})
        self.assertNotIn("SECRET", json.dumps(private))


if __name__ == "__main__":
    unittest.main()
