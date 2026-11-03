"""Review actual project evidence formats using only invented media."""

from contextlib import redirect_stdout
import copy
import hashlib
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

SOCKET = socket.socket
DNS = socket.getaddrinfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-prototype'))
import test_reconcile as fixtures
import prepare_review
from prepare_review import Blocked, LIMITS, MODE, ReviewDraft
from prototype import Plan, canonical, digest
from review import serve


class EvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReconcileTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        code, self.report, paths = self.fixture.run_fresh_cli()
        self.assertEqual(code, 0, self.report)
        self.report_path = self.root / 'reconciliation.json'
        self.fixture.write_json(self.report_path, self.report)
        self.paths = (self.report_path, paths[0], paths[1])

    def session(self):
        return ReviewDraft(*self.paths)

    def test_draft_keeps_variants_together(self):
        session = self.session()
        self.assertEqual(session.state()['selected'], [])
        selected = ['full-asset-photo']
        state = session.draft(0, selected)
        saved = session.save(state['revision'])
        plan = session.current
        data = plan.data()
        self.assertEqual(data['mode'], MODE)
        self.assertEqual(data['limits'], LIMITS)
        self.assertEqual(data['selected'], selected)
        self.assertEqual(data['kept'], ['full-asset-live'])
        kept = next(a for a in data['assets'] if a['id'] == 'full-asset-live')
        self.assertEqual(len(kept['resources']), 3)
        self.assertEqual({r['resource_record_type'] for r in kept['resources']}, {'CPLAsset', 'CPLMaster'})
        self.assertEqual(saved['bytes'], sum(r['size'] for a in data['assets'] if a['id'] in selected for r in a['resources']))
        self.assertIsNone(saved['confirmation'])
        self.assertEqual(saved['draft_digest'], plan.sha256)
        self.assertEqual(len({(Path(p) / 'draft.json').read_bytes() for p in data['outputs']}), 1)
        for output in data['outputs']:
            path = Path(output)
            self.assertEqual(Plan.load(path / 'draft.json'), plan)
            self.assertEqual(json.loads((path / 'complete.json').read_text()), {'draft_sha256': plan.sha256})
            html = (path / 'review.html').read_text()
            self.assertIn('Saved review draft', html)
            self.assertNotIn('SYNTHETIC PREVIEW', html)
            self.assertNotIn('Confirm in Terminal', html)
            self.assertIn('disabled', html)
        from cleanup import Fixture, UnconditionalCloud, simulate
        provider = UnconditionalCloud(Fixture(self.root / 'mock-boundary').snapshot)
        with self.assertRaisesRegex(Blocked, 'supervised offline cleanup plan'):
            simulate(plan, provider, plan.sha256)
        self.assertEqual(provider.calls, [])
        # Editing retires every old copy and can save a keep-all draft.
        state = session.draft(saved['revision'], [])
        self.assertIsNone(state['draft_digest'])
        self.assertTrue(all((Path(p) / 'superseded.json').exists() for p in data['outputs']))
        session.save(state['revision'])
        self.assertNotEqual(session.current.sha256, plan.sha256)
        self.assertEqual(session.current.data()['count'], 0)
        self.assertEqual(len(session.current.data()['kept']), 2)

    def test_reopen_restores_saved_choices(self):
        for selected in ([], ['full-asset-live']):
            original = self.session()
            original.draft(0, selected)
            original.save(original.revision)
            plan = original.current
            paths = [Path(output) / 'draft.json' for output in plan.data()['outputs']]
            before = {path: path.read_bytes() for path in paths}
            original.close()
            for path in paths:
                with self.subTest(selected=selected, copy=path.parent.parent.parent.name):
                    reopened = ReviewDraft.reopen(path)
                    state = reopened.state()
                    self.assertEqual(state['selected'], selected)
                    self.assertEqual(state['revision'], plan.data()['revision'])
                    self.assertEqual(state['draft_digest'], plan.sha256)
                    self.assertIsNone(state['confirmation'])
                    reopened.save(state['revision'])
                    self.assertEqual(reopened.current, plan)
                    reopened.close()
            self.assertEqual(before, {path: path.read_bytes() for path in paths})
            reopened = ReviewDraft.reopen(paths[0])
            changed = [] if selected else ['full-asset-photo']
            state = reopened.draft(reopened.revision, changed)
            reopened.save(state['revision'])
            self.assertNotEqual(reopened.current.sha256, plan.sha256)
            self.assertEqual(reopened.current.data()['selected'], changed)
            self.assertTrue(all((path.parent / 'superseded.json').exists() for path in paths))
            with self.assertRaisesRegex(Blocked, 'superseded'):
                ReviewDraft.reopen(paths[0])

    def test_reopen_requires_three_intact_copies(self):
        original = self.session()
        original.save(0)
        paths = [Path(output) for output in original.current.data()['outputs']]
        draft = paths[0] / 'draft.json'
        for case in ('missing', 'incomplete', 'changed', 'superseded'):
            target = paths[-1] / ('complete.json' if case == 'incomplete' else 'draft.json')
            before = target.read_bytes()
            marker = paths[-1] / 'superseded.json'
            try:
                if case in ('missing', 'incomplete'):
                    target.unlink()
                elif case == 'changed':
                    target.write_text('{}', encoding='utf-8')
                else:
                    marker.write_text('{}', encoding='utf-8')
                with self.subTest(case=case), self.assertRaisesRegex(Blocked, 'saved draft|superseded|draft copies'):
                    ReviewDraft.reopen(draft)
            finally:
                target.write_bytes(before)
                marker.unlink(missing_ok=True)

    def test_reopen_binds_selection_and_context(self):
        original = self.session()
        original.save(0)
        plan = original.current
        paths = [Path(output) for output in plan.data()['outputs']]
        for case in ('selection', 'counts', 'scope', 'resources', 'limits', 'roots', 'locations', 'legacy'):
            data = plan.data()
            if case == 'selection':
                data['selected'] = ['missing-asset']
            elif case == 'counts':
                data['bytes'] += 1
            elif case == 'scope':
                data['scope']['account_id'] = 'different-account'
            elif case == 'resources':
                data['assets'][0]['resources'].pop()
            elif case == 'limits':
                data['limits']['removal_approved'] = True
            elif case == 'roots':
                data['root_identities'][0][1] += 1
            elif case == 'locations':
                data['outputs'][-1] = str(self.root)
            else:
                data.pop('evidence')
            edited = Plan(canonical(data), digest(data))
            try:
                for path in paths:
                    (path / 'draft.json').write_text(canonical({'payload': edited.payload, 'sha256': edited.sha256}), encoding='utf-8')
                    (path / 'complete.json').write_text(canonical({'draft_sha256': edited.sha256}), encoding='utf-8')
                with self.subTest(case=case), self.assertRaisesRegex(Blocked, 'Saved draft|Saved media|reopen metadata'):
                    ReviewDraft.reopen(paths[0] / 'draft.json')
            finally:
                for path in paths:
                    (path / 'draft.json').write_text(canonical({'payload': plan.payload, 'sha256': plan.sha256}), encoding='utf-8')
                    (path / 'complete.json').write_text(canonical({'draft_sha256': plan.sha256}), encoding='utf-8')

    def test_incomplete_evidence_blocks_preparation(self):
        originals = {path: path.read_bytes() for path in self.paths}
        cases = {'mode': 'complete fresh reconciliation', 'findings': 'complete fresh reconciliation',
                 'input': 'input changed', 'mapping': 'does not match', 'unknown': 'incomplete'}
        for case, finding in cases.items():
            try:
                report = copy.deepcopy(self.report)
                if case == 'mode':
                    report['mode'] = 'RECORDED_EVIDENCE_ONLY'
                elif case == 'findings':
                    report['findings'] = [{'code': 'missing_original_resource'}]
                elif case == 'mapping':
                    report['matches'][0]['relative_path'] = 'wrong.jpg'
                else:
                    observation = copy.deepcopy(self.fixture.observation)
                    if case == 'unknown':
                        observation['resources'][0]['role'] = 'unknown'
                    else:
                        observation['album_id'] = 'changed-album'
                    self.fixture.write_json(self.paths[1], observation)
                    if case == 'unknown':
                        report['input_sha256']['resource_report'] = hashlib.sha256(self.paths[1].read_bytes()).hexdigest()
                self.fixture.write_json(self.report_path, report)
                with self.subTest(case=case), self.assertRaisesRegex(Blocked, finding):
                    self.session()
            finally:
                for path, content in originals.items():
                    path.write_bytes(content)

    def test_stale_selections_and_evidence_stop(self):
        session = self.session()
        for invalid in (['full-asset-live'] * 2, [False], ['missing-asset'], ['resOriginalRes']):
            with self.subTest(selected=invalid), self.assertRaisesRegex(Blocked, 'unique full item IDs'):
                session.draft(0, invalid)
        session.draft(0, ['full-asset-live'])
        with self.assertRaisesRegex(Blocked, 'another request'):
            session.save(0)
        self.paths[1].write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(Blocked, 'verified and saved'):
            session.save(1)
        self.assertEqual(session.phase, 'stopped')
        self.assertIsNone(session.state()['draft_digest'])

    def test_media_changes_stop_save_and_reopen(self):
        for side in ('copy-a', 'copy-b'):
            path = next((self.root / side).iterdir())
            original, info = path.read_bytes(), path.stat()
            for case, operation in ((case, operation) for case in ('bytes', 'missing', 'extra')
                                    for operation in ('save', 'reopen')):
                session = self.session()
                if operation == 'reopen':
                    session.save(0)
                    draft = Path(session.current.data()['outputs'][0]) / 'draft.json'
                    session.close()
                extra = path.parent / 'unexpected.mov'
                try:
                    if case == 'bytes':
                        path.write_bytes(b'x' * len(original))
                        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                    elif case == 'missing':
                        path.unlink()
                    else:
                        extra.write_bytes(b'unknown companion')
                    finding = 'verified and saved' if operation == 'save' else 'Replica (bytes|file set) changed'
                    with self.subTest(side=side, case=case, operation=operation), self.assertRaisesRegex(Blocked, finding):
                        session.save(0) if operation == 'save' else ReviewDraft.reopen(draft)
                    if operation == 'save':
                        self.assertEqual(session.phase, 'stopped')
                    if case == 'bytes':
                        self.assertEqual(path.read_bytes(), b'x' * len(original))
                finally:
                    extra.unlink(missing_ok=True)
                    path.write_bytes(original)
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))

    def test_partial_saves_never_claim_success(self):
        session = self.session()
        write = prepare_review.write_new
        def fail(path, content):
            if path.name == 'complete.json' and session.bundles[-1] in path.parents:
                raise OSError('Synthetic disconnect')
            return write(path, content)
        with patch.object(prepare_review, 'write_new', side_effect=fail), \
                self.assertRaisesRegex(Blocked, 'saved at every location'):
            session.save(0)
        self.assertEqual(session.phase, 'stopped')
        self.assertFalse(session.state()['saved'])
        self.assertIsNone(session.current)
        self.assertFalse(list((session.bundles[-1] / 'reviews').glob('*/complete.json')))

    def test_saved_draft_changes_invalidate_state(self):
        session = self.session()
        session.save(0)
        original = session.current
        path = Path(original.data()['outputs'][-1]) / 'draft.json'
        path.write_text('{}', encoding='utf-8')
        self.assertEqual(session.state()['phase'], 'stopped')
        self.assertIsNone(session.state()['draft_digest'])

    def test_loopback_saves_without_cloud_actions(self):
        session = self.session()
        def local_dns(host, *args, **kwargs):
            if host != '127.0.0.1':
                raise AssertionError('External DNS forbidden')
            return DNS(host, *args, **kwargs)
        with patch.object(socket, 'socket', SOCKET), patch.object(socket, 'getaddrinfo', side_effect=local_dns), serve(session) as url:
            address = urlsplit(url)
            def request(method, path, body=None):
                headers = {'Host': address.netloc, 'Authorization': 'Bearer ' + address.fragment,
                           'Origin': 'http://' + address.netloc, 'Content-Type': 'application/json'}
                connection = HTTPConnection(address.hostname, address.port, timeout=5)
                try:
                    connection.request(method, path, json.dumps(body) if body is not None else None, headers)
                    response = connection.getresponse()
                    return response.status, response.read().decode()
                finally:
                    connection.close()
            status, html = request('GET', '/')
            self.assertEqual(status, 200)
            self.assertIn('REVIEW DRAFT', html)
            self.assertNotIn('full-asset-live', html)
            self.assertNotIn('Confirm in Terminal', html)
            status, raw = request('GET', '/api/state')
            self.assertEqual(status, 200)
            self.assertIn('observed variants', json.loads(raw)['cards'])
            status, raw = request('POST', '/api/draft', {'revision': 0, 'selected': ['full-asset-live']})
            self.assertEqual(status, 200)
            status, raw = request('POST', '/api/save', {'revision': json.loads(raw)['revision']})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(raw)['saved'])
            self.assertIsNone(json.loads(raw)['confirmation'])
            self.assertEqual(request('POST', '/api/execute', {})[0], 404)

    def test_cli_closes_local_review(self):
        from contextlib import contextmanager
        @contextmanager
        def opened(session):
            self.assertEqual(session.state()['selected'], [])
            yield 'http://127.0.0.1:1234/#synthetic-token'
        args = ['prepare_review.py', '--reconciliation', str(self.paths[0]),
                '--resource-report', str(self.paths[1]), '--download-manifest', str(self.paths[2])]
        output = io.StringIO()
        for encoding in ('utf-8', 'utf-8-sig', 'utf-16'):
            self.report_path.write_text(json.dumps(self.report), encoding=encoding)
            with self.subTest(encoding=encoding), patch.object(sys, 'argv', args), \
                    patch('review.serve', side_effect=opened), patch('builtins.input', return_value=''), redirect_stdout(output):
                self.assertEqual(prepare_review.main(), 0)
        self.assertIn('Open the review: http://127.0.0.1:', output.getvalue())
        self.assertNotIn('full-asset-live', output.getvalue())

    def test_cli_reopens_saved_review(self):
        from contextlib import contextmanager
        original = self.session()
        original.draft(0, ['full-asset-live'])
        original.save(1)
        path = Path(original.current.data()['outputs'][1]) / 'draft.json'
        @contextmanager
        def opened(session):
            self.assertEqual(session.state()['selected'], ['full-asset-live'])
            self.assertEqual(session.current, original.current)
            yield 'http://127.0.0.1:1234/#synthetic-token'
        output = io.StringIO()
        with patch.object(sys, 'argv', ['prepare_review.py', '--resume', str(path)]), \
                patch('review.serve', side_effect=opened), patch('builtins.input', return_value=''), redirect_stdout(output):
            self.assertEqual(prepare_review.main(), 0)
        self.assertIn('Saved draft:', output.getvalue())
        self.assertIn('Reopen with:', output.getvalue())
        self.assertIn('--resume', output.getvalue())
        self.assertNotIn('full-asset-live', output.getvalue())


if __name__ == '__main__':
    unittest.main()
