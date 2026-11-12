"""Bounded observed-resource downloads through fake metadata and media wires."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from requests import Request, Response, Timeout
from requests.adapters import HTTPAdapter

import download_observed as observed
import test_cloud_metadata as fixtures
from probe import PIN, save_new
from preflight import metadata_for
from reconcile import load_input, reconcile
from originals import DownloadBlocked


class ObservedDownloadTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CloudMetadataTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.fixture.root
        source = self.repo / '.smoke/cloud-probe-captured'
        source.mkdir(parents=True)
        self.path = source / 'metadata-1-private.json'
        save_new(self.path, metadata_for(self.fixture.data, observed.SOURCE))
        save_new(source / 'summary.json', {'schema': 1, 'provider_version': PIN, 'scope': 'read_only_preflight',
                                         'authenticated': True, 'completed': True, 'metadata_snapshots': 1})
        self.baseline = observed.Baseline(self.repo, self.path)
        self.bodies = {}
        resources = {(item['resource_record_type'], item['resource_record_id'], item['resource_field']): item
                     for asset in self.fixture.data['assets'] for item in asset['resources']}
        for record in self.fixture.records:
            for field, entry in record['fields'].items():
                if not field.endswith('Res'):
                    continue
                resource = resources[(record['recordType'], record['recordName'], field)]
                url = f'https://cvws.icloud-content.com/fixture-{len(self.bodies)}%2fresource?token=fixture%7evalue'
                entry['value']['downloadURL'] = url
                normalized = Request('GET', url).prepare().url
                self.bodies[normalized] = (self.repo / 'copy-a' / resource['relative_path']).read_bytes()
        self.gets, self.fault, self.on_get = [], None, None
        def send(adapter, request, **kwargs):
            return self.send(adapter, request, **kwargs)
        wire = patch.object(HTTPAdapter, 'send', send)
        wire.start()
        self.addCleanup(wire.stop)

    def send(self, adapter, request, **kwargs):
        if request.method != 'GET':
            return self.fixture.send(adapter, request, **kwargs)
        self.assertTrue(kwargs['stream'])
        self.assertIs(kwargs['verify'], True)
        self.assertEqual(kwargs['timeout'], (10, 30))
        self.assertEqual(request.headers['Range'], 'bytes=0-')
        self.gets.append(request.url)
        if self.on_get:
            self.on_get()
        if self.fault == 'timeout':
            raise Timeout('SECRET_UPSTREAM_URL')
        body = self.bodies[request.url]
        if self.fault == 'short':
            body = body[:-1]
        elif self.fault == 'long':
            body += b'extra'
        response = Response()
        response.status_code = {'redirect': 302, 'throttle': 429, 'range': 206}.get(self.fault, 200)
        response.headers['Content-Type'] = 'application/json' if self.fault == 'document' else 'application/octet-stream'
        if self.fault == 'throttle':
            response.headers['Retry-After'] = '20'
        if self.fault == 'range':
            response.headers['Content-Range'] = 'bytes 1-2/3'
        response.raw = io.BytesIO(body)
        response.request, response.url = request, request.url
        return response

    def run_directory(self, name):
        path = self.repo / '.smoke' / name
        path.mkdir()
        return path

    def test_completed_copies_reconcile_by_owner(self):
        provider, run, summary = self.fixture.provider(), self.run_directory('success'), {}
        observed.download(provider, self.baseline, run, summary)
        self.assertTrue(summary['completed'])
        self.assertEqual((summary['media_gets'], summary['metadata_requests']), (6, 18))
        manifest_path = run / 'download-manifest-private.json'
        manifest = json.loads(manifest_path.read_text())
        complete = json.loads((run / 'complete.json').read_text())
        self.assertEqual(complete['manifest_sha256'], hashlib.sha256(manifest_path.read_bytes()).hexdigest())
        self.assertEqual(complete['resource_report_sha256'], hashlib.sha256((run / 'resource-report-private.json').read_bytes()).hexdigest())
        for item in manifest['resources']:
            a, b = (run / item[key] for key in ('copy_a', 'copy_b'))
            self.assertEqual(a.read_bytes(), b.read_bytes())
            self.assertEqual(hashlib.sha256(a.read_bytes()).hexdigest(), item['response_sha256'])
        previews = [item for item in manifest['resources'] if item['resource_field'] == 'resJPEGThumbRes']
        self.assertEqual({item['resource_record_type'] for item in previews}, {'CPLAsset', 'CPLMaster'})
        self.assertEqual(len({item['copy_a'] for item in previews}), 2)
        for path in run.glob('*.json'):
            self.assertNotIn('https://', path.read_text())
        self.fixture.session.request.assert_not_called()
        # Exercise compatibility with actual Rust replica manifests, not just
        # hand-assembled records that happen to resemble their schema.
        exe = self.fixture.fixture.fixture.exe
        first, second = run / 'primary-artifacts', run / 'secondary-artifacts'
        first.mkdir()
        second.mkdir()
        def cli(*args):
            result = subprocess.run([str(exe), '--json', '--state-dir', str(run / 'state'), *map(str, args)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        cli('import', '--cycle', 'observed', '--source', run / 'copy-a')
        replica = cli('replicate', '--cycle', 'observed', '--secondary', run / 'copy-b', '--primary-artifacts', first,
                      '--secondary-artifacts', second, '--allow-unverified-devices')
        records, _ = load_input(Path(replica['artifact_bundles'][0]) / 'manifest.jsonl', jsonl=True)
        result = reconcile(json.loads((run / 'resource-report-private.json').read_text()), manifest, records)
        self.assertEqual(result['findings'], [])
        self.assertEqual(result['summary']['matched_resources'], 6)
        self.assertEqual(result['limits']['physical_drive_independence'], 'NOT VERIFIED')

    def test_metadata_drift_blocks_completion(self):
        for phase in ('before', 'after'):
            provider, run = self.fixture.provider(), self.run_directory('drift-' + phase)
            self.fixture.calls, self.gets = [], []
            def change(value):
                value['records'][0]['recordChangeTag'] = 'new-tag'
            self.fixture.mutate = change if phase == 'before' else None
            self.on_get = (lambda: setattr(self.fixture, 'mutate', change)) if phase == 'after' else None
            with self.subTest(phase=phase), self.assertRaisesRegex(observed.Blocked, 'Fresh metadata differs'):
                observed.download(provider, self.baseline, run, {})
            self.assertEqual(len(self.gets), 0 if phase == 'before' else 6)
            self.assertFalse((run / 'download-manifest-private.json').exists())
            self.assertFalse((run / 'complete.json').exists())
            self.fixture.mutate, self.on_get = None, None

    def test_media_failures_stop_without_retry(self):
        findings = {'timeout': (Timeout, None), 'redirect': (observed.Blocked, 'redirect refused'),
                    'throttle': (observed.ChangesThrottled, 'requested a pause'),
                    'short': (DownloadBlocked, 'shorter'), 'long': (DownloadBlocked, 'exceeded its declared'),
                    'range': (DownloadBlocked, 'mismatched byte range'), 'document': (DownloadBlocked, 'document')}
        for fault in ('timeout', 'redirect', 'throttle', 'short', 'long', 'range', 'document'):
            provider, run, summary = self.fixture.provider(), self.run_directory(fault), {}
            self.fault, self.gets = fault, []
            exception, message = findings[fault]
            rejection = self.assertRaisesRegex(exception, message) if message else self.assertRaises(exception)
            with self.subTest(fault=fault), rejection:
                observed.download(provider, self.baseline, run, summary)
            self.assertEqual(len(self.gets), 1)
            self.assertEqual(summary['media_gets'], 1)
            self.assertFalse((run / 'download-manifest-private.json').exists())
            self.assertFalse((run / 'complete.json').exists())
            if fault == 'throttle':
                self.assertEqual(provider.pacer.pause.retry_after_seconds, 20)

    def test_prepared_media_cannot_change_scope(self):
        prepare = self.fixture.session.prepare_request
        guard = observed.MediaTransport.guard
        for case in ('method', 'host', 'range', 'body', 'other_resource', 'tls', 'retries'):
            provider, run = self.fixture.provider(), self.run_directory('prepared-' + case)
            self.gets = []
            def mutate(request):
                prepared = prepare(request)
                if request.method != 'GET':
                    return prepared
                if case == 'method':
                    prepared.method = 'POST'
                elif case == 'host':
                    prepared.url = 'https://evil.example/SECRET_URL'
                elif case == 'range':
                    prepared.headers['Range'] = 'bytes=1-'
                elif case == 'body':
                    prepared.body = 'unexpected'
                elif case == 'other_resource':
                    prepared.url = next(url for url in self.bodies if url != prepared.url)
                return prepared
            @contextmanager
            def changed_guard(media):
                adapter = self.fixture.session.get_adapter(fixtures.album.ROOT)
                with guard(media) as state, patch.object(self.fixture.session, 'verify', case != 'tls'), \
                        patch.object(adapter.max_retries, 'total', 1 if case == 'retries' else 0):
                    yield state
            with self.subTest(case=case), patch.object(self.fixture.session, 'prepare_request', side_effect=mutate), \
                    patch.object(observed.MediaTransport, 'guard', changed_guard), \
                    self.assertRaisesRegex(observed.Blocked, 'Media request exceeded'):
                observed.download(provider, self.baseline, run, {})
            self.assertEqual(self.gets, [])
            self.assertEqual(provider.requests, 9)

    def test_changed_evidence_prevents_publication(self):
        for case in ('baseline', 'copy', 'existing'):
            provider, run, summary = self.fixture.provider(), self.run_directory('changed-' + case), {}
            self.gets = []
            original = self.path.read_bytes()
            def change():
                if case == 'baseline':
                    self.path.write_text('{}')
                elif case == 'copy' and len(self.gets) == 6:
                    path = next((run / 'copy-b').iterdir())
                    path.write_bytes(b'changed')
            self.on_get = change
            if case == 'existing':
                (run / 'copy-a').mkdir()
                (run / 'copy-a/keep.jpg').write_bytes(b'keep')
            rejection = (self.assertRaises(FileExistsError) if case == 'existing' else
                         self.assertRaisesRegex(observed.Blocked, 'approved metadata capture changed' if case == 'baseline' else 'local research copy changed'))
            with self.subTest(case=case), rejection:
                observed.download(provider, self.baseline, run, summary)
            self.path.write_bytes(original)
            self.assertEqual(len(self.gets), 1 if case == 'baseline' else 6 if case == 'copy' else 0)
            self.assertFalse((run / 'download-manifest-private.json').exists())
            if case == 'existing':
                self.assertEqual((run / 'copy-a/keep.jpg').read_bytes(), b'keep')
            self.on_get = None

    def test_baseline_limits_and_ownership(self):
        for case in ('large', 'total', 'duplicate', 'owner', 'original', 'unknown', 'partial', 'boolean'):
            data = self.baseline.data()
            asset, item = data['assets'][0], data['assets'][0]['resources'][0]
            if case == 'large':
                item['size'] = observed.MAX_VARIANT_BYTES + 1
            elif case == 'total':
                for entry in (entry for a in data['assets'] for entry in a['resources']):
                    entry['size'] = 12 * 1024 * 1024
            elif case == 'duplicate':
                asset['resources'].append(copy.deepcopy(item))
            elif case == 'owner':
                item['owner_change_tag'] = 'wrong-tag'
            elif case == 'original':
                item.update(resource_field='resOriginalRes')
            elif case == 'unknown':
                item['resource_field'] = 'resUnknownRes'
            elif case == 'partial':
                data['complete'] = False
            else:
                item['size'] = True
            message = {'large': '32 MiB', 'total': '64 MiB', 'duplicate': 'duplicated', 'owner': 'owner',
                       'original': 'originals or rendition', 'unknown': 'unknown', 'partial': 'complete bounded', 'boolean': '32 MiB'}[case]
            with self.subTest(case=case), self.assertRaisesRegex(observed.Blocked, message):
                observed.frozen(data)

        # Exact byte limits are accepted without allocating media-sized buffers.
        data = self.baseline.data()
        for asset in data['assets']:
            asset['resources'] = [item for item in asset['resources'] if item['resource_field'] == 'resOriginalRes']
            asset['resources'][0]['size'] = observed.MAX_VARIANT_BYTES
        self.assertEqual(len(json.loads(observed.frozen(data))['assets']), 2)

        # Five distinct owners can carry 21 resources; a 22nd remains forbidden.
        data = self.baseline.data()
        for number in range(3):
            asset = copy.deepcopy(data['assets'][0])
            asset.update(id='extra-asset-' + str(number), master_id='extra-master-' + str(number))
            for item in asset['resources']:
                item['resource_record_id'] = asset['id'] if item['resource_record_type'] == 'CPLAsset' else asset['master_id']
            data['assets'].append(asset)
        self.assertEqual(sum(len(asset['resources']) for asset in json.loads(observed.frozen(data))['assets']), 21)
        asset = data['assets'][1]
        preview = copy.deepcopy(asset['resources'][0])
        preview['resource_field'] = 'resJPEGThumbRes'
        asset['resources'].append(preview)
        with self.assertRaisesRegex(observed.Blocked, '21-resource'):
            observed.frozen(data)

    def test_publication_failures_never_complete(self):
        publish = observed.publish
        for case in ('manifest', 'marker', 'edited'):
            run, summary = self.run_directory('publication-' + case), {}
            def save(path, value):
                if path.name == ('complete.json' if case == 'marker' else 'download-manifest-private.json'):
                    if case != 'edited':
                        raise OSError('Simulated publication failure')
                    value = {**value, 'complete': False}
                publish(path, value)
            rejection = (self.assertRaisesRegex(observed.Blocked, 'Published download evidence changed') if case == 'edited'
                         else self.assertRaisesRegex(OSError, 'publication failure'))
            with self.subTest(case=case), patch.object(observed, 'publish', side_effect=save), rejection:
                observed.download(self.fixture.provider(), self.baseline, run, summary)
            self.assertIsNot(summary.get('completed'), True)
            self.assertFalse((run / 'complete.json').exists())

    def test_cli_confirms_once_before_account_access(self):
        cancellations = ('blank', 'no', 'invalid', 'eof', 'interrupt')
        for case in ('offline', *cancellations, 'notty', 'success', 'yes', 'account', 'stale'):
            run, output, errors = self.run_directory('cli-' + case), io.StringIO(), io.StringIO()
            service = self.fixture.service
            service.requires_2fa = service.requires_2sa = False
            service._get_webservice_url = lambda _: fixtures.album.ROOT
            service.data['dsInfo']['dsid'] = 'other' if case == 'account' else 'synthetic-account'
            original = self.path.read_bytes()
            self.gets = []
            args = ['download_observed.py', '--baseline', str(self.path), '--album', fixtures.album.NAME]
            if case != 'offline':
                args.append('--live')
            def answer(prompt):
                self.assertEqual(prompt, 'Download these resources? [y/N] ')
                self.assertIn('Destination: ' + str(run), output.getvalue())
                self.assertIn('6 resources', output.getvalue())
                self.assertIn('Album: ' + fixtures.album.NAME, output.getvalue())
                self.assertNotIn(self.baseline.sha256, output.getvalue())
                self.assertEqual(self.gets, [])
                if case == 'stale':
                    self.path.write_text('{}')
                if case in ('eof', 'interrupt'):
                    raise EOFError if case == 'eof' else KeyboardInterrupt
                return {'blank': '', 'no': 'N', 'invalid': 'DOWNLOAD 6 RESOURCES old-digest', 'yes': 'YES'}.get(case, ' y ')
            with self.subTest(case=case), patch.object(sys, 'argv', args), patch.object(observed, 'Baseline', return_value=self.baseline), \
                    patch.object(sys.stdin, 'isatty', return_value=case != 'notty'), patch.object(errors, 'isatty', return_value=True), \
                    patch('builtins.input', side_effect=answer) as approval, \
                    patch.object(observed.getpass, 'getpass', return_value='local@example.test'), \
                    patch('pyicloud_ipd.PyiCloudService', return_value=service) as authenticate, \
                    patch.object(observed, 'new_run', return_value=run), \
                    patch.object(observed, 'discover_zones', return_value=({**self.fixture.zones, 'shared': []}, [])) as discover, \
                    redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(observed.main(), 1 if case in ('notty', 'account', 'stale') else 0)
            self.path.write_bytes(original)
            success = case in ('success', 'yes')
            self.assertEqual(approval.call_count, 0 if case in ('offline', 'notty') else 1)
            self.assertEqual(authenticate.call_count, 1 if success or case == 'account' else 0)
            self.assertEqual(discover.call_count, int(success))
            self.assertEqual(len(self.gets), 6 if success else 0)
            self.assertNotIn('SECRET', output.getvalue() + errors.getvalue())
            self.assertNotIn('local@example.test', output.getvalue() + errors.getvalue())
            if success:
                self.assertTrue(json.loads((run / 'summary.json').read_text())['completed'])
            elif case in cancellations:
                summary = json.loads((run / 'summary.json').read_text())
                self.assertTrue(summary['cancelled'])
                self.assertFalse(summary['authenticated'])
                self.assertFalse(summary['completed'])
                self.assertFalse((run / 'copy-a').exists())
                self.assertIn('Cancelled', output.getvalue())


if __name__ == '__main__':
    unittest.main()
