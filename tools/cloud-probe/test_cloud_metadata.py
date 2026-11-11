"""Exercise real metadata transport and saved drafts without network access."""

from contextlib import nullcontext, redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from requests import Response, Session, Timeout
from requests.adapters import HTTPAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-cleanup'))
import cloud_metadata as cloud
import conflict
import preflight
import test_prepare_review as fixtures
from probe import PIN
import test_album as album


class CloudMetadataTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EvidenceReviewTests()
        self.fixture.include_previews = True
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.review = self.fixture.session()
        self.review.draft(0, ['full-asset-live'])
        self.review.save(1)
        self.data = self.review.current.data()
        self.session = Session()
        self.session.request = Mock(side_effect=AssertionError('Provider request wrapper forbidden'))
        self.addCleanup(self.session.close)
        self.service = SimpleNamespace(session=self.session, params={'dsid': 'synthetic-account'},
                                       data={'dsInfo': {'dsid': 'synthetic-account'}})
        self.zones = {'private': [{'zone_id': self.data['scope']['zone_id'], 'deleted': False}]}
        self.records = []
        for asset in self.data['assets']:
            items = {asset['id']: {'recordType': 'CPLAsset', 'recordName': asset['id'],
                                  'recordChangeTag': asset['asset_change_tag'],
                                  'fields': {'masterRef': {'value': {'recordName': asset['master_id']}}}},
                     asset['master_id']: {'recordType': 'CPLMaster', 'recordName': asset['master_id'],
                                          'recordChangeTag': asset['master_change_tag'], 'fields': {}}}
            for resource in asset['resources']:
                fields = items[resource['resource_record_id']]['fields']
                field = resource['resource_field']
                fields[field] = {'value': {'size': resource['size'], 'fileChecksum': resource['provider_checksum_opaque'],
                                          'downloadURL': 'https://SECRET_SIGNED_URL', 'wrappingKey': 'SECRET_KEY'}}
                fields[field.removesuffix('Res') + 'FileType'] = {'value': resource['file_type']}
            self.records.extend(items.values())
        self.calls, self.fault, self.mutate = [], None, None
        pacing = patch.object(conflict, 'INTERVAL', 0)
        pacing.start()
        self.addCleanup(pacing.stop)
        wire = patch.object(HTTPAdapter, 'send', autospec=True, side_effect=self.send)
        wire.start()
        self.addCleanup(wire.stop)

    def provider(self):
        return cloud.CloudMetadata(self.service, album.ROOT, self.zones, album.NAME, self.data, conflict.Pacer())

    def send(self, adapter, request, **kwargs):
        self.assertEqual(request.method, 'POST')
        self.assertEqual(kwargs['timeout'], (10, 30))
        self.assertIs(kwargs['verify'], True)
        self.assertTrue(kwargs['stream'])
        body = json.loads(request.body)
        self.calls.append(body)
        if self.fault == 'timeout':
            raise Timeout('SECRET_UPSTREAM_ERROR')
        if 'batch' in body:
            value = album.count_response(3 if self.fault == 'count' else 2)
        elif body['query']['recordType'] == 'CPLAlbumByPositionLive':
            value = {'records': [album.album_record(identity='wrong' if self.fault == 'album' else self.data['scope']['album_id'])]}
        else:
            rank = next(item['fieldValue']['value'] for item in body['query']['filterBy'] if item['fieldName'] == 'startRank')
            value = {'records': copy.deepcopy(self.records) if rank == 0 else []}
            if self.fault == 'continuation' and rank:
                value['moreComing'] = True
            if self.mutate and not rank:
                self.mutate(value)
        response = Response()
        response.status_code = 429 if self.fault == 'throttle' else 302 if self.fault == 'redirect' else 200
        response.headers['Content-Type'] = 'application/json'
        if self.fault == 'throttle':
            response.headers['Retry-After'] = '60'
        response.raw = io.BytesIO(json.dumps(value).encode())
        response.request, response.url = request, request.url
        return response

    def test_fresh_metadata_confirms_saved_draft(self):
        provider = self.provider()
        provider.output = self.fixture.root / 'metadata'
        provider.output.mkdir()
        gate = preflight.Preflight(self.review, provider)
        phrase = gate.prepare()
        self.assertTrue(phrase.startswith('CONFIRM READ ONLY PREFLIGHT 1 '))
        receipt = gate.confirm(phrase)
        previews = [resource for resource in gate.plan.data()['targets'][0]['resources']
                    if resource['resource_field'] == 'resJPEGThumbRes']
        self.assertEqual(len(previews), 2)
        self.assertEqual({item['resource_record_type'] for item in previews}, {'CPLAsset', 'CPLMaster'})
        self.assertEqual(len({item['relative_path'] for item in previews}), 2)
        self.assertEqual(len({item['recorded_sha256'] for item in previews}), 2)
        self.assertEqual(receipt['result'], 'READ_ONLY_CHECK_CONFIRMED')
        self.assertEqual(receipt['provider_metadata'], 'OBSERVED ICLOUD METADATA MATCHED')
        self.assertFalse(receipt['limits']['removal_approved'])
        self.assertEqual(receipt['limits']['server_version_condition'], 'NOT VERIFIED')
        self.assertEqual((provider.reads, provider.requests), (2, 18))
        self.session.request.assert_not_called()
        self.assertNotIn('SECRET', json.dumps(receipt) + gate.plan.payload)
        for path in provider.output.iterdir():
            self.assertNotIn('SECRET', path.read_text())
            self.assertEqual(json.loads(path.read_text())['source'], cloud.SOURCE)
        with self.assertRaisesRegex(preflight.Blocked, 'closed'):
            provider.snapshot()
        self.assertEqual(len(self.calls), 18)

    def test_bound_identity_prevents_photo_reads(self):
        for case in ('account', 'zone', 'database', 'album'):
            with self.subTest(case=case):
                data = copy.deepcopy(self.data)
                if case == 'account':
                    data['scope']['account_id'] = 'other'
                elif case == 'zone':
                    data['scope']['zone_id']['ownerRecordName'] = 'other'
                elif case == 'database':
                    data['scope']['database'] = 'shared'
                self.fault, self.calls = case, []
                with self.assertRaises(preflight.Blocked):
                    cloud.CloudMetadata(self.service, album.ROOT, self.zones, album.NAME, data, conflict.Pacer()).snapshot()
                self.assertEqual(len(self.calls), 1 if case == 'album' else 0)

    def test_transport_failure_never_retries(self):
        for fault in ('timeout', 'redirect', 'throttle', 'count', 'continuation'):
            self.fault, self.calls = fault, []
            provider = self.provider()
            with self.subTest(fault=fault), self.assertRaisesRegex(preflight.Blocked, 'no retry') as error:
                provider.snapshot()
            self.assertNotIn('SECRET', str(error.exception))
            before = len(self.calls)
            with self.assertRaisesRegex(preflight.Blocked, 'closed'):
                provider.snapshot()
            self.assertEqual(len(self.calls), before)
            if fault in ('timeout', 'redirect', 'throttle'):
                self.assertEqual(before, 1)
            else:
                self.assertEqual(before, 2 if fault == 'count' else 4)
            if fault == 'throttle':
                self.assertIsNotNone(provider.pacer.pause)

    def test_prepared_requests_cannot_expand_scope(self):
        prepare = self.session.prepare_request
        for case in ('method', 'zone', 'album', 'host', 'query', 'retry', 'verify', 'budget', 'account'):
            provider, self.calls = self.provider(), []
            def mutate(request):
                result = prepare(request)
                if case == 'method':
                    result.method = 'GET'
                elif case == 'zone':
                    body = json.loads(result.body)
                    body['zoneID'] = {'zoneName': 'Other'}
                    result.body = json.dumps(body)
                elif case == 'album':
                    result.url = result.url.replace('/records/query', '/records/modify')
                elif case == 'host':
                    result.url = result.url.replace('icloud.com', 'example.com')
                elif case == 'query':
                    result.url = result.url.replace('synthetic-account', 'other-account')
                return result
            adapter = self.session.get_adapter(album.ROOT)
            with self.subTest(case=case), patch.object(self.session, 'prepare_request', side_effect=mutate), \
                    patch.object(adapter.max_retries, 'total', 1 if case == 'retry' else 0), \
                    patch.object(self.session, 'verify', False if case == 'verify' else True), \
                    patch.object(cloud, 'MAX_REQUESTS', 0 if case == 'budget' else 50):
                if case == 'account':
                    self.service.data['dsInfo']['dsid'] = 'other'
                try:
                    with self.assertRaises(preflight.Blocked):
                        provider.snapshot()
                    self.assertEqual(self.calls, [])
                finally:
                    self.service.data['dsInfo']['dsid'] = 'synthetic-account'

    def test_changed_metadata_blocks_confirmation(self):
        for case in ('tag', 'resource', 'asset_preview', 'master_preview', 'missing_preview', 'unknown', 'passes'):
            gate = preflight.Preflight(self.review, self.provider())
            phrase = gate.prepare()
            def mutate(value):
                record = value['records'][0]
                if case == 'tag':
                    record['recordChangeTag'] = 'new-tag'
                elif case in ('asset_preview', 'master_preview', 'missing_preview'):
                    owner_type = 'CPLMaster' if case == 'master_preview' else 'CPLAsset'
                    owner = next(item for item in value['records'] if item['recordType'] == owner_type
                                 and 'resJPEGThumbRes' in item['fields'])
                    if case == 'missing_preview':
                        owner['fields'].pop('resJPEGThumbRes')
                        owner['fields'].pop('resJPEGThumbFileType')
                    else:
                        owner['fields']['resJPEGThumbRes']['value']['size'] += 1
                elif case in ('resource', 'unknown'):
                    master = next(item for item in value['records'] if item['recordType'] == 'CPLMaster')
                    if case == 'unknown':
                        master['fields']['resNovelRes'] = copy.deepcopy(master['fields']['resOriginalRes'])
                    else:
                        master['fields']['resOriginalRes']['value']['size'] += 1
                elif len(self.calls) % 9 == 3:
                    record['recordChangeTag'] = 'changed-between-passes'
            self.mutate = mutate
            finding = 'unavailable or malformed' if case in ('unknown', 'passes') else 'metadata changed'
            with self.subTest(case=case), self.assertRaisesRegex(preflight.Blocked, finding):
                gate.confirm(phrase)
            self.assertEqual(gate.provider.requests, 18)
            self.assertFalse(any((output / 'receipt.json').exists() for output in gate.outputs))
            self.mutate = None

    def test_authentication_requires_interactive_opt_in(self):
        for tty, answer in ((False, 'READ ONLY PREFLIGHT'), (True, 'wrong')):
            with self.subTest(tty=tty), patch.object(sys.stdin, 'isatty', return_value=tty), \
                    patch.object(sys.stderr, 'isatty', return_value=tty), patch('builtins.input', return_value=answer), \
                    patch.object(cloud.getpass, 'getpass') as credentials, redirect_stdout(io.StringIO()), \
                    self.assertRaises(preflight.Blocked):
                with cloud.authenticate(self.data, album.NAME):
                    self.fail('Authentication should be refused')
            credentials.assert_not_called()
        with patch.object(cloud, 'version', return_value='other'), self.assertRaises(preflight.Blocked):
            self.provider()

    def test_authenticated_scope_is_checked_before_discovery(self):
        for case in ('match', 'account', 'discovery'):
            run = self.fixture.root / ('auth-' + case)
            run.mkdir()
            self.service.requires_2fa = self.service.requires_2sa = False
            self.service._get_webservice_url = lambda _: album.ROOT
            self.service.data['dsInfo']['dsid'] = 'other' if case == 'account' else 'synthetic-account'
            zones = {**self.zones, 'shared': []}
            with self.subTest(case=case), patch.object(sys.stdin, 'isatty', return_value=True), \
                    patch.object(sys.stderr, 'isatty', return_value=True), \
                    patch('builtins.input', return_value='READ ONLY PREFLIGHT'), \
                    patch.object(cloud.getpass, 'getpass', return_value='local@example.test'), \
                    patch.object(cloud, 'new_run', return_value=run), \
                    patch('pyicloud_ipd.PyiCloudService', return_value=self.service) as constructor, \
                    patch.object(cloud, 'discover_zones', return_value=(zones, ['incomplete'] if case == 'discovery' else [])) as discover, \
                    redirect_stdout(io.StringIO()):
                if case == 'match':
                    with cloud.authenticate(self.data, album.NAME) as provider:
                        self.assertIs(type(provider), cloud.CloudMetadata)
                        self.assertEqual(provider.reads, 0)
                else:
                    with self.assertRaises(preflight.Blocked):
                        with cloud.authenticate(self.data, album.NAME):
                            self.fail('Wrong scope must stop')
                self.assertEqual(constructor.call_count, 1)
                self.assertEqual(discover.call_count, 0 if case == 'account' else 1)
            summary = json.loads((run / 'summary.json').read_text())
            self.assertEqual(summary['completed'], case == 'match')
            self.assertEqual(summary['metadata_requests'], 0)
            self.assertNotIn('local@example.test', json.dumps(summary))

    def test_cloud_cli_confirms_or_cancels(self):
        draft = str(Path(self.data['outputs'][0]) / 'draft.json')
        for case in ('confirm', 'cancel'):
            provider, output = self.provider(), io.StringIO()
            def answer(prompt):
                self.assertTrue(prompt.startswith('Type CONFIRM READ ONLY PREFLIGHT 1 '))
                return prompt[len('Type '):-len(': ')] if case == 'confirm' else ''
            with self.subTest(case=case), patch.object(sys, 'argv', ['preflight.py', '--read-only-cloud', '--draft', draft,
                                                                    '--album', album.NAME]), \
                    patch.object(preflight, 'authenticate', return_value=nullcontext(provider)) as authenticate, \
                    patch('builtins.input', side_effect=answer), redirect_stdout(output):
                self.assertEqual(preflight.main(), 0)
            authenticate.assert_called_once_with(self.data, album.NAME, None)
            self.assertEqual(provider.reads, 2 if case == 'confirm' else 1)
            self.assertIn('READ_ONLY_CHECK_CONFIRMED' if case == 'confirm' else 'Cancelled', output.getvalue())
            self.assertNotIn('SECRET', output.getvalue())


if __name__ == '__main__':
    unittest.main()
