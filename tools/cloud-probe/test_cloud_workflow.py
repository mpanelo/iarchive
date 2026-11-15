"""Connect saved evidence and durable intent to mocked Photos HTTP."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from requests import Response, Timeout
from requests.adapters import HTTPAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-cleanup'))
import cleanup as demo
import supervised
from cloud_workflow import CloudCleanup, recover_run
from conflict import Pacer
from prompts import Cancelled
from prototype import Blocked, canonical
from supervised import Backups, Cleanup, inspect_run
import test_cloud_metadata as metadata_fixtures
import test_album as album


class CloudWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = metadata_fixtures.CloudMetadataTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.session, self.service, self.zones = self.fixture.review, self.fixture.service, self.fixture.zones
        self.service._get_webservice_url = lambda _: album.ROOT
        self.records = copy.deepcopy(self.fixture.records)
        for record in self.records:
            if record['recordType'] == 'CPLAsset':
                record['fields']['isDeleted'] = {'value': None}
        self.calls, self.removed, self.fault = [], set(), None
        wire = patch.object(HTTPAdapter, 'send', side_effect=self.send)
        wire.start()
        self.addCleanup(wire.stop)
        previous, _ = supervised.evidence.load_input(self.session.bundles[0] / 'manifest.jsonl', jsonl=True)
        fresh = copy.deepcopy(previous)
        fresh[0]['source_identity'].update(volume_id='modeled-first', physical_disks=[3])
        fresh[0]['secondary_identity'].update(volume_id='modeled-second', physical_disks=[4])
        proof = {'independent_physical_drives': 'VERIFIED BY OS TOPOLOGY'}
        topology = patch.object(supervised.evidence, 'fresh_replica', return_value=(fresh, 'unused', proof))
        self.topology = topology.start()
        self.addCleanup(topology.stop)

    def workflow(self, *, all_items=False):
        if all_items:
            self.session.draft(self.session.revision, [a['id'] for a in self.session.assets])
            self.session.save(self.session.revision)
        self.provider = CloudCleanup(self.service, album.ROOT, self.zones, album.NAME, self.session.current, Pacer())
        self.flow = Cleanup(self.session, self.provider, Backups(self.fixture.fixture.fixture.exe))
        return self.flow

    def confirm(self, flow, answer='y'):
        with patch('builtins.input', return_value=answer), redirect_stdout(io.StringIO()):
            return flow.confirm_and_execute()

    def send(self, adapter, request, **kwargs):
        path, body = urlsplit(request.url).path, json.loads(request.body)
        kind = path.rsplit('/', 1)[-1]
        self.calls.append(kind)
        self.assertEqual(kwargs['timeout'], (10, 30))
        self.assertIs(kwargs['verify'], True)
        if kind == 'modify':
            # Observe the actual network boundary, rather than mocking execution.
            for output in self.flow.outputs:
                event = json.loads((output / 'journal/000001.json').read_text())
                self.assertEqual(event['status'], 'attempted')
                self.assertEqual(event['plan_sha256'], self.flow.plan.sha256)
                marker = json.loads((output.parent.parent / 'cleanup-started.json').read_text())
                self.assertEqual(marker['plan_sha256'], self.flow.plan.sha256)
            self.assertEqual(body, self.provider.writer.request())
            if self.fault == 'lost_before':
                raise Timeout('SECRET_LOST_REPLY')
            value = {'records': []}
            for op in body['operations']:
                key = op['record']['recordName']
                record = next(r for r in self.records if r['recordName'] == key)
                if self.fault == 'partial' and key == 'full-asset-live':
                    value['records'].append({'recordName': key, 'serverErrorCode': 'CONFLICT'})
                    continue
                self.removed.add(key)
                record['recordChangeTag'] = 'removed-tag'
                record['fields']['isDeleted'] = {'value': 1}
                value['records'].append(copy.deepcopy(record))
            if self.fault == 'lost_after':
                raise Timeout('SECRET_LOST_REPLY')
            if self.fault == 'resource':
                master = next(r for r in self.records if r['recordName'] == 'full-master-live')
                master['fields']['resOriginalRes']['value']['size'] += 1
            if self.fault == 'kept':
                next(r for r in self.records if r['recordName'] == 'full-asset-photo')['recordChangeTag'] = 'changed'
        elif kind == 'lookup':
            if self.fault == 'lookup':
                raise Timeout('SECRET_LOOKUP_ERROR')
            names = {r['recordName'] for r in body['records']}
            value = {'records': [copy.deepcopy(r) for r in self.records if r['recordName'] in names]}
        else:
            excluded = self.removed | {r['fields']['masterRef']['value']['recordName'] for r in self.records
                                      if r['recordName'] in self.removed}
            active = [copy.deepcopy(r) for r in self.records if r['recordName'] not in excluded]
            if 'batch' in body:
                value = album.count_response(sum(r['recordType'] == 'CPLAsset' for r in active))
            elif body['query']['recordType'] == 'CPLAlbumByPositionLive':
                value = {'records': [album.album_record(identity=self.session.scope['album_id'])]}
            else:
                rank = next(f['fieldValue']['value'] for f in body['query']['filterBy'] if f['fieldName'] == 'startRank')
                value = {'records': active if rank == 0 else []}
        response = Response()
        response.status_code = 200
        response.headers['Content-Type'] = 'application/json'
        response.raw = io.BytesIO(json.dumps(value).encode())
        self.addCleanup(response.raw.close)
        response.request, response.url = request, request.url
        return response

    def test_confirmation_connects_the_durable_request(self):
        flow = self.workflow()
        flow.prepare()
        before = {p: p.read_bytes() for root in self.session.roots for p in root.iterdir()}
        result = self.confirm(flow, 'yes')
        self.assertTrue(result['complete'], result.get('finding'))
        self.assertEqual(result['mode'], supervised.CLOUD_MODE)
        self.assertEqual(self.removed, {'full-asset-live'})
        self.assertEqual(self.calls.count('modify'), 1)
        self.assertEqual(self.topology.call_count, 2)
        self.assertEqual(inspect_run(flow.outputs[0])['accepted'], 1)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        for name in ('replies.json', 'postcheck.json', 'result.json', 'complete.json'):
            copies = {(output / name).read_bytes() for output in flow.outputs}
            self.assertEqual(len(copies), 1)
            self.assertNotIn(b'SECRET', copies.pop())
        with self.assertRaisesRegex(Blocked, 'closed'):
            flow.execute(flow.plan)
        self.assertEqual(self.calls.count('modify'), 1)

    def test_empty_album_is_checked_after_removal(self):
        flow = self.workflow(all_items=True)
        flow.prepare()
        result = self.confirm(flow)
        self.assertTrue(result['complete'], result.get('finding'))
        self.assertEqual(len(self.removed), 2)
        self.assertEqual(self.calls.count('modify'), 1)
        postcheck = json.loads((flow.outputs[0] / 'postcheck.json').read_text())
        self.assertTrue(postcheck['remaining_album_matched'])

    def test_partial_reply_checks_both_sets(self):
        flow = self.workflow(all_items=True)
        flow.prepare()
        self.fault = 'partial'
        result = self.confirm(flow)
        self.assertEqual(result['outcomes'], {'full-asset-live': 'rejected', 'full-asset-photo': 'accepted'})
        self.assertFalse(result['complete'])
        self.assertEqual(inspect_run(flow.outputs[1])['rejected'], 1)
        self.assertEqual(self.calls.count('modify'), 1)

    def test_unknown_outcomes_require_read_only_recovery(self):
        for fault in ('lost_before', 'lost_after', 'lookup', 'resource', 'kept'):
            # Each case has a fresh saved draft and no reusable attempted plan.
            self.session = self.fixture.fixture.session()
            self.session.draft(0, ['full-asset-live'])
            self.session.save(1)
            self.records = copy.deepcopy(self.fixture.records)
            for record in self.records:
                if record['recordType'] == 'CPLAsset':
                    record['fields']['isDeleted'] = {'value': None}
            self.removed, self.calls, self.fault = set(), [], None
            flow = self.workflow()
            flow.prepare()
            self.fault = fault
            with self.subTest(fault=fault):
                result = self.confirm(flow)
                self.assertEqual(result['outcomes'], {'full-asset-live': 'unknown'})
                self.assertFalse(result['complete'])
                self.assertEqual(self.calls.count('modify'), 1)
                self.assertNotIn('SECRET', canonical(result))
                if fault in ('lost_before', 'lost_after'):
                    prior = {p: p.read_bytes() for output in flow.outputs for p in output.iterdir() if p.is_file()}
                    self.fault = None
                    recovery = recover_run(flow.outputs[0], self.service, album.ROOT, self.zones, Pacer())
                    self.assertFalse(recovery['retry_allowed'])
                    self.assertFalse(recovery['outcomes_changed'])
                    self.assertEqual(recovery['targets'][0]['deletion_flag'], 1 if fault == 'lost_after' else None)
                    self.assertEqual(prior, {p: p.read_bytes() for p in prior})
                    self.assertEqual(inspect_run(flow.outputs[0])['unknown'], 1)
                    self.assertEqual(self.calls.count('modify'), 1)

    def test_drive_and_confirmation_gates_precede_writes(self):
        provider = CloudCleanup(self.service, album.ROOT, self.zones, album.NAME, self.session.current, Pacer())
        with self.assertRaisesRegex(Blocked, 'strict physical-drive'):
            Cleanup(self.session, provider, Backups())
        self.assertEqual(self.calls, [])
        flow = self.workflow()
        with patch.object(flow.backups, 'check', return_value={'physical_drives': 'SIMULATED; NOT VERIFIED'}), \
                self.assertRaisesRegex(Blocked, 'verified physical drives'):
            flow.prepare()
        self.assertEqual(self.calls, [])
        flow = self.workflow()
        plan = flow.prepare()
        with self.assertRaisesRegex(Blocked, 'final confirmation'):
            flow.execute(plan)
        self.assertNotIn('modify', self.calls)
        flow = self.workflow()
        flow.prepare()
        with self.assertRaises(Cancelled):
            self.confirm(flow, '')
        self.assertNotIn('modify', self.calls)

    def test_last_check_rejects_drift(self):
        for case in ('bytes', 'metadata'):
            flow = self.workflow()
            flow.prepare()
            path = self.session.roots[1] / self.session.files[0]['relative_path']
            original = path.read_bytes()
            def answer(_prompt):
                if case == 'bytes':
                    path.write_bytes(b'changed')
                else:
                    next(r for r in self.records if r['recordName'] == 'full-asset-live')['recordChangeTag'] = 'changed'
                return 'y'
            try:
                with self.subTest(case=case), patch('builtins.input', side_effect=answer), redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(Blocked, 'Replica bytes changed|metadata changed'):
                        flow.confirm_and_execute()
                self.assertNotIn('modify', self.calls)
            finally:
                path.write_bytes(original)

    def test_partial_intent_prevents_http_dispatch(self):
        flow = self.workflow()
        flow.prepare()
        write = demo.write_new
        def fail(path, value):
            if path.parent == flow.outputs[1] / 'journal':
                raise OSError('Synthetic disconnected journal drive')
            write(path, value)
        with patch.object(demo, 'write_new', side_effect=fail), self.assertRaises(OSError):
            self.confirm(flow)
        self.assertNotIn('modify', self.calls)
        with self.assertRaisesRegex(Blocked, 'copies differ'):
            inspect_run(flow.outputs[0])

    def test_expiry_after_intent_prevents_dispatch(self):
        flow, now = self.workflow(), [100.0]
        write = demo.write_new
        def persist(path, value):
            write(path, value)
            if path == flow.outputs[-1] / 'journal/000001.json':
                now[0] = flow.deadline
        with patch.object(supervised.time, 'monotonic', side_effect=lambda: now[0]):
            flow.prepare()
            with patch.object(demo, 'write_new', side_effect=persist):
                result = self.confirm(flow)
        self.assertEqual(result['request_count'], 0)
        self.assertEqual(result['outcomes'], {'full-asset-live': 'unknown'})
        self.assertNotIn('modify', self.calls)
        self.assertEqual(inspect_run(flow.outputs[0])['unknown'], 1)


if __name__ == '__main__':
    unittest.main()
