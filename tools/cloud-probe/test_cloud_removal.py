"""Exercise prepared Photos requests with synthetic records and fake HTTP only."""

import copy
import io
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from requests import Response, Session, Timeout
from requests.adapters import HTTPAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-cleanup'))
import cloud_removal as cloud
import conflict
import test_prepare_review as fixtures
from prototype import Blocked, Plan, canonical, digest
from supervised import Backups, Cleanup
from test_album import ROOT


class CloudRemovalTests(unittest.TestCase):
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
        self.session.request = Mock(side_effect=AssertionError('Provider wrapper forbidden'))
        self.session.send = Mock(side_effect=AssertionError('Session redirects forbidden'))
        self.addCleanup(self.session.close)
        self.service = SimpleNamespace(session=self.session, params={'dsid': 'synthetic-account'},
            data={'dsInfo': {'dsid': 'synthetic-account'}}, _get_webservice_url=lambda _: ROOT)
        self.zones = {'private': [{'zone_id': self.data['scope']['zone_id'], 'deleted': False}], 'shared': []}
        self.records = []
        for asset in self.data['assets']:
            pair = {asset['id']: {'recordName': asset['id'], 'recordType': 'CPLAsset',
                'recordChangeTag': asset['asset_change_tag'], 'fields': {
                    'masterRef': {'value': {'recordName': asset['master_id']}}, 'isDeleted': {'value': None}}},
                asset['master_id']: {'recordName': asset['master_id'], 'recordType': 'CPLMaster',
                    'recordChangeTag': asset['master_change_tag'], 'fields': {}}}
            for resource in asset['resources']:
                fields, field = pair[resource['resource_record_id']]['fields'], resource['resource_field']
                fields[field] = {'value': {'size': resource['size'], 'fileChecksum': resource['provider_checksum_opaque'],
                                          'downloadURL': 'https://SECRET_SIGNED_URL', 'wrappingKey': 'SECRET_KEY'}}
                fields[field.removesuffix('Res') + 'FileType'] = {'value': resource['file_type']}
            for record in pair.values():
                record['fields']['captionEnc'] = {'value': 'SECRET_CAPTION'}
            self.records.extend(pair.values())
        self.calls, self.responses, self.fault, self.mutate = [], [], None, None
        wire = patch.object(HTTPAdapter, 'send', autospec=True, side_effect=self.send)
        wire.start()
        self.addCleanup(wire.stop)
        pacing = patch.object(conflict, 'INTERVAL', 0)
        pacing.start()
        self.addCleanup(pacing.stop)

    def adapter(self, data=None, **kwargs):
        data = self.data if data is None else data
        return cloud.CloudRemoval(self.service, ROOT, self.zones, Plan(canonical(data), digest(data)), conflict.Pacer(), **kwargs)

    def send(self, adapter, request, **kwargs):
        self.assertEqual(request.method, 'POST')
        self.assertTrue(urlsplit(request.url).path.endswith(('/records/modify', '/records/lookup')))
        self.assertEqual(parse_qs(urlsplit(request.url).query),
                         {'dsid': ['synthetic-account'], 'remapEnums': ['True'], 'getCurrentSyncToken': ['True']})
        self.assertEqual(kwargs, {'stream': True, 'verify': True, 'cert': None, 'proxies': {}, 'timeout': (10, 30)})
        kind, body = urlsplit(request.url).path.rsplit('/', 1)[-1], json.loads(request.body)
        self.calls.append((kind, body))
        if self.fault == 'timeout':
            raise Timeout('SECRET_UPSTREAM_ERROR')
        if self.fault == 'interrupt':
            raise KeyboardInterrupt
        if kind == 'modify':
            value = {'records': [{'recordName': op['record']['recordName'], 'recordType': 'CPLAsset',
                     'recordChangeTag': 'post-write-tag', 'fields': {'isDeleted': {'value': 1}}}
                     for op in body['operations']]}
        else:
            allowed = {item['recordName'] for item in body['records']}
            value = {'records': [copy.deepcopy(record) for record in self.records if record['recordName'] in allowed]}
        if self.mutate:
            self.mutate(value)
        response = Response()
        response.status_code = {'redirect': 302, 'throttle': 429, 'unavailable': 503,
                                'http_error': 500, 'expired_auth': 401}.get(self.fault, 200)
        response.headers['Content-Type'] = 'application/json'
        if self.fault in ('throttle', 'retry_after'):
            response.headers['Retry-After'] = '17'
        raw = json.dumps(value).encode()
        if self.fault == 'oversized':
            raw = b'x' * (conflict.MAX_RESPONSE + 1)
        elif self.fault == 'malformed':
            raw = b'not JSON SECRET_BODY'
        elif self.fault == 'duplicate_json':
            raw = b'{"records": [], "records": []}'
        response.raw = io.BytesIO(raw)
        response.close = Mock(wraps=response.close)
        self.addCleanup(response.raw.close)
        response.request, response.url = request, request.url
        self.responses.append(response)
        return response

    def test_only_saved_targets_are_sent(self):
        adapter = self.adapter()
        request = adapter.request()
        request['operations'][0]['record']['fields'] = {'isExpunged': {'value': 1}}
        self.assertNotEqual(request, adapter.request())
        expected = adapter.request()
        disabled = logging.root.manager.disable
        def logged_send(adapter, request, **kwargs):
            logging.getLogger('iarchive.synthetic-wire').warning('SECRET_UPSTREAM_LOG')
            return self.send(adapter, request, **kwargs)
        with patch.object(HTTPAdapter, 'send', side_effect=logged_send), \
                self.assertNoLogs('iarchive.synthetic-wire'):
            replies = adapter.remove(expected)
        self.assertEqual(logging.root.manager.disable, disabled)
        self.assertEqual(replies, [{'id': 'full-asset-live', 'reply': 'ACCEPTED'}])
        self.assertEqual(self.calls, [('modify', expected)])
        self.assertEqual([op['record']['recordName'] for op in expected['operations']], ['full-asset-live'])
        self.assertEqual(expected['operations'][0]['record']['fields'], {'isDeleted': {'value': 1}})
        self.assertEqual(adapter.writes, 1)
        for response in self.responses:
            response.close.assert_called_once_with()
        self.session.request.assert_not_called()
        self.session.send.assert_not_called()
        self.assertTrue(self.session.trust_env)
        with self.assertRaisesRegex(Blocked, 'never retry'):
            adapter.remove(expected)
        self.assertEqual(len(self.calls), 1)

    def test_invalid_scope_never_reaches_transport(self):
        for case in ('account', 'database', 'zone', 'empty', 'duplicate', 'missing', 'shared_master', 'too_many', 'tag'):
            data = copy.deepcopy(self.data)
            if case == 'account':
                data['scope']['account_id'] = 'other-account'
            elif case == 'database':
                data['scope']['database'] = 'shared'
            elif case == 'zone':
                data['scope']['zone_id']['ownerRecordName'] = 'other-owner'
            elif case == 'empty':
                data['selected'], data['count'] = [], 0
            elif case == 'duplicate':
                data['selected'] *= 2
                data['count'] = 2
            elif case == 'missing':
                data['selected'] = ['unknown-asset']
            elif case == 'shared_master':
                data['assets'][1]['master_id'] = data['assets'][0]['master_id']
            elif case == 'tag':
                next(a for a in data['assets'] if a['id'] in data['selected'])['asset_change_tag'] = ''
            else:
                template = data['assets'][0]
                data['assets'] = [{**template, 'id': 'asset-' + str(i), 'master_id': 'master-' + str(i)}
                                  for i in range(cloud.MAX_TARGETS + 1)]
                data['selected'] = [a['id'] for a in data['assets']]
                data['count'] = len(data['selected'])
            with self.subTest(case=case), self.assertRaisesRegex(Blocked, 'unambiguous saved selection'):
                self.adapter(data)
        self.assertEqual(self.calls, [])

    def test_prepared_requests_cannot_expand_scope(self):
        prepare = self.session.prepare_request
        for case in ('host', 'path', 'query', 'method', 'body', 'account', 'params', 'retry', 'custom_adapter'):
            adapter, self.calls = self.adapter(), []
            def mutate(request):
                self.assertFalse(self.session.trust_env)
                result = prepare(request)
                if case == 'host':
                    result.url = result.url.replace('icloud.com', 'example.com')
                elif case == 'path':
                    result.url = result.url.replace('records/modify', 'records/delete')
                elif case == 'query':
                    result.url += '&unexpected=True'
                elif case == 'method':
                    result.method = 'DELETE'
                elif case == 'body':
                    result.body = '{}'
                elif case == 'account':
                    self.service.data['dsInfo']['dsid'] = 'changed'
                elif case == 'params':
                    self.service.params['unexpected'] = True
                return result
            mounted = self.session.get_adapter(ROOT)
            class CustomAdapter(HTTPAdapter):
                pass
            custom = CustomAdapter()
            self.addCleanup(custom.close)
            try:
                with self.subTest(case=case), patch.object(self.session, 'prepare_request', side_effect=mutate), \
                        patch.object(mounted.max_retries, 'total', 1 if case == 'retry' else 0), \
                        patch.object(self.session, 'get_adapter', return_value=custom if case == 'custom_adapter' else mounted):
                    self.assertEqual(adapter.remove(adapter.request()), [{'id': 'full-asset-live', 'reply': 'UNKNOWN'}])
                    self.assertEqual((self.calls, adapter.writes), ([], 0))
                    with self.assertRaisesRegex(Blocked, 'never retry'):
                        adapter.remove(adapter.request())
            finally:
                self.service.data['dsInfo']['dsid'] = 'synthetic-account'
                self.service.params.pop('unexpected', None)

    def test_request_edits_never_reach_transport(self):
        for case in ('purge', 'force', 'extra', 'tag', 'zone'):
            adapter = self.adapter()
            request = adapter.request()
            record = request['operations'][0]['record']
            if case == 'purge':
                record['fields'] = {'isExpunged': {'value': 1}}
            elif case == 'force':
                request['operations'][0]['operationType'] = 'forceDelete'
            elif case == 'extra':
                request['operations'].append(copy.deepcopy(request['operations'][0]))
            elif case == 'tag':
                record['recordChangeTag'] = 'different-tag'
            else:
                request['zoneID'] = {'zoneName': 'Other'}
            with self.subTest(case=case), self.assertRaisesRegex(Blocked, 'frozen selection'):
                adapter.remove(request)
        self.assertEqual(self.calls, [])

    def test_transport_failures_remain_unknown(self):
        for fault in ('timeout', 'interrupt', 'redirect', 'oversized', 'slow', 'malformed', 'duplicate_json', 'http_error', 'expired_auth'):
            adapter, self.calls, self.fault = self.adapter(), [], fault
            with self.subTest(fault=fault), patch.object(conflict, 'MAX_SECONDS', 0 if fault == 'slow' else conflict.MAX_SECONDS):
                replies = adapter.remove(adapter.request())
                self.assertEqual(replies, [{'id': 'full-asset-live', 'reply': 'UNKNOWN'}])
                self.assertEqual((len(self.calls), adapter.writes), (1, 1))
                self.assertNotIn('SECRET', json.dumps(replies) + str(adapter.finding))
                with self.assertRaisesRegex(Blocked, 'never retry'):
                    adapter.remove(adapter.request())
                if fault == 'expired_auth':
                    with self.assertRaisesRegex(Blocked, 'no retry'):
                        adapter.lookup()
                    self.assertEqual(len(self.calls), 1)
        for response in self.responses:
            response.close.assert_called_once_with()

    def test_throttling_stops_all_later_requests(self):
        for fault in ('throttle', 'unavailable', 'retry_after', 'record_throttle'):
            adapter, self.calls = self.adapter(), []
            self.fault = fault
            self.mutate = (lambda value: value['records'][0].update(serverErrorCode='THROTTLED', retryAfter=27)) if fault == 'record_throttle' else None
            with self.subTest(fault=fault):
                self.assertEqual(adapter.remove(adapter.request())[0]['reply'], 'UNKNOWN')
                self.assertIsNotNone(adapter.pacer.pause)
                self.assertEqual(adapter.pacer.pause.retry_after_seconds, 27 if fault == 'record_throttle' else 17 if fault != 'unavailable' else None)
                with self.assertRaisesRegex(Blocked, 'no retry'):
                    adapter.lookup()
                self.assertEqual(len(self.calls), 1)

    def test_per_record_replies_stay_separate(self):
        data = copy.deepcopy(self.data)
        data['selected'] = [a['id'] for a in data['assets']]
        data['count'] = len(data['selected'])
        adapter = self.adapter(data)
        self.mutate = lambda value: value['records'][-1].update(serverErrorCode='CONFLICT')
        replies = adapter.remove(adapter.request())
        self.assertEqual({item['reply'] for item in replies}, {'ACCEPTED', 'REJECTED'})
        self.assertEqual({item['id'] for item in replies}, set(data['selected']))
        self.assertEqual(len(self.calls), 1)

    def test_reply_shape_never_implies_success(self):
        for case in ('missing', 'extra', 'duplicate', 'wrong_id', 'malformed_id', 'wrong_type', 'zone', 'bool', 'no_tag', 'purged', 'unknown_error'):
            data = copy.deepcopy(self.data)
            if case == 'duplicate':
                data['selected'] = [a['id'] for a in data['assets']]
                data['count'] = len(data['selected'])
            adapter = self.adapter(data)
            def mutate(value):
                record = value['records'][0]
                if case == 'missing':
                    value['records'].clear()
                elif case == 'extra':
                    value['records'].append(copy.deepcopy(record))
                elif case == 'duplicate':
                    value['records'][1] = copy.deepcopy(record)
                elif case == 'wrong_id':
                    record['recordName'] = 'unknown-asset'
                elif case == 'malformed_id':
                    record['recordName'] = {'malformed': 'SECRET_VALUE'}
                elif case == 'wrong_type':
                    record['recordType'] = 'CPLMaster'
                elif case == 'zone':
                    record['zoneID'] = {'zoneName': 'Other'}
                elif case == 'bool':
                    record['fields']['isDeleted']['value'] = True
                elif case == 'no_tag':
                    record.pop('recordChangeTag')
                elif case == 'purged':
                    record['fields']['isExpunged'] = {'value': 1}
                else:
                    record['serverErrorCode'] = 'SECRET_UNKNOWN_CODE'
            self.mutate = mutate
            with self.subTest(case=case):
                self.assertEqual(adapter.remove(adapter.request()), [{'id': a['id'], 'reply': 'UNKNOWN'}
                                 for a in data['assets'] if a['id'] in data['selected']])

    def test_recovery_reads_only_bound_records(self):
        adapter = self.adapter(recovery_only=True)
        with self.assertRaisesRegex(Blocked, 'unavailable'):
            adapter.remove(adapter.request())
        for _ in range(cloud.MAX_LOOKUPS):
            records = adapter.lookup()
            self.assertEqual({r['recordName'] for r in records}, {'full-asset-live', 'full-master-live'})
            self.assertIsNone(next(r for r in records if r['recordType'] == 'CPLAsset')['fields']['isDeleted'])
            self.assertNotIn('SECRET', json.dumps(records))
            self.assertEqual(next(r for r in records if r['recordType'] == 'CPLMaster')['fields']['resOriginalRes']['size'],
                             next(r for r in self.records if r['recordName'] == 'full-master-live')['fields']['resOriginalRes']['value']['size'])
        with self.assertRaisesRegex(Blocked, 'exhausted'):
            adapter.lookup()
        self.assertEqual((adapter.writes, adapter.lookups), (0, cloud.MAX_LOOKUPS))
        self.assertTrue(all(kind == 'lookup' for kind, _ in self.calls))
        for _, body in self.calls:
            self.assertEqual(body['zoneID'], self.data['scope']['zone_id'])
            self.assertNotIn('operations', body)

    def test_ambiguous_recovery_fails_closed(self):
        for case in ('missing', 'duplicate', 'owner', 'flag', 'bool', 'master_bool', 'purged', 'unknown_resource'):
            adapter = self.adapter(recovery_only=True)
            def mutate(value):
                asset = next(r for r in value['records'] if r['recordType'] == 'CPLAsset')
                if case == 'missing':
                    value['records'].pop()
                elif case == 'duplicate':
                    value['records'][1] = copy.deepcopy(value['records'][0])
                elif case == 'owner':
                    asset['fields']['masterRef']['value']['recordName'] = 'wrong-master'
                elif case == 'flag':
                    asset['fields'].pop('isDeleted')
                elif case == 'bool':
                    asset['fields']['isDeleted'] = {'value': True}
                elif case == 'master_bool':
                    next(r for r in value['records'] if r['recordType'] == 'CPLMaster')['fields']['isDeleted'] = {'value': True}
                elif case == 'purged':
                    asset['fields']['isExpunged'] = {'value': 1}
                else:
                    asset['fields']['resUnknownThingRes'] = {'value': {'size': 1}}
            self.mutate = mutate
            with self.subTest(case=case), self.assertRaisesRegex(Blocked, 'no retry') as error:
                adapter.lookup()
            self.assertNotIn('SECRET', str(error.exception))

    def test_shared_pacing_covers_every_request(self):
        adapter = self.adapter()
        ticks, sleeps = [10.0], []
        adapter.pacer = conflict.Pacer()
        def wait(delay):
            sleeps.append(delay)
            ticks[0] += delay
        with patch.object(conflict, 'INTERVAL', 1.0), patch.object(conflict.time, 'monotonic', side_effect=lambda: ticks[0]), \
                patch.object(conflict.time, 'sleep', side_effect=wait):
            adapter.remove(adapter.request())
            adapter.lookup()
            adapter.lookup()
        self.assertEqual(sleeps, [1.0, 1.0])
        self.assertEqual((adapter.writes, adapter.lookups), (1, 2))

    def test_adapter_has_no_execution_entrypoint(self):
        adapter = self.adapter()
        with self.assertRaisesRegex(Blocked, 'offline removal model'):
            Cleanup(self.review, adapter, Backups())
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
