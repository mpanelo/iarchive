"""One-request rehearsal through saved evidence, with no network or real media."""

from contextlib import redirect_stdout
import copy
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import test_prepare_review as fixtures
import supervised
from supervised import Backups, Blocked, Cleanup, MockRemoval, Plan, canonical, inspect_run


class SupervisedTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EvidenceReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def cleanup(self, selected=('full-asset-live',)):
        session = self.fixture.session()
        session.draft(0, list(selected))
        session.save(session.revision)
        return Cleanup(session, MockRemoval(session.current.data()), Backups())

    def test_saved_selection_binds_one_request(self):
        cleanup = self.cleanup()
        before = {p: p.read_bytes() for root in cleanup.session.roots for p in root.iterdir()}
        plan = cleanup.prepare()
        result = cleanup.execute(plan)
        self.assertTrue(result['complete'])
        self.assertEqual(result['backups']['physical_drives'], 'SIMULATED; NOT VERIFIED')
        self.assertEqual(len(cleanup.provider.requests), 1)
        request = cleanup.provider.requests[0]
        target = next(a for a in cleanup.draft.data()['assets'] if a['id'] == 'full-asset-live')
        self.assertEqual(request['zoneID'], cleanup.draft.data()['scope']['zone_id'])
        self.assertEqual(request['operations'], [{'operationType': 'update', 'record': {
            'recordType': 'CPLAsset', 'recordName': target['id'], 'recordChangeTag': target['asset_change_tag'],
            'fields': {'isDeleted': {'value': 1}}}}])
        self.assertEqual(len(target['resources']), 3)
        self.assertEqual([a['id'] for a in cleanup.provider.metadata.current['assets']], ['full-asset-photo'])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(inspect_run(cleanup.outputs[1]), {'state': 'COMPLETED', 'retry_allowed': False,
                                                        'accepted': 1, 'rejected': 0, 'unknown': 0})
        for name in ('plan.json', 'result.json', 'complete.json', 'journal/000001.json'):
            self.assertEqual(len({(p / name).read_bytes() for p in cleanup.outputs}), 1)
        with self.assertRaisesRegex(Blocked, 'closed'):
            cleanup.execute(plan)
        draft_path = Path(cleanup.draft.data()['outputs'][0]) / 'draft.json'
        reopened = supervised.ReviewDraft.reopen(draft_path)
        another = Cleanup(reopened, MockRemoval(reopened.current.data()), Backups())
        with self.assertRaisesRegex(Blocked, 'already started cleanup'):
            another.prepare()
        self.assertEqual(another.provider.requests, [])
        self.assertEqual(len(cleanup.provider.requests), 1)

    def test_final_guards_prevent_requests(self):
        for case in ('selection', 'bytes', 'receipt', 'plan_copy', 'wrong_plan', 'topology', 'expired', 'metadata'):
            cleanup = self.cleanup()
            plan = cleanup.prepare()
            changed, original = None, None
            if case == 'selection':
                cleanup.session.draft(cleanup.session.revision, [])
            elif case == 'bytes':
                changed = cleanup.session.roots[1] / cleanup.session.files[0]['relative_path']
            elif case == 'receipt':
                changed = cleanup.gate.outputs[-1] / 'receipt.json'
            elif case == 'plan_copy':
                changed = cleanup.outputs[-1] / 'plan.json'
            elif case == 'wrong_plan':
                plan = Plan(plan.payload, '0' * 64)
            elif case == 'topology':
                cleanup.backups.independent = False
            elif case == 'metadata':
                cleanup.provider.metadata.current['assets'][0]['asset_change_tag'] = 'changed'
            if changed:
                original = changed.read_bytes()
                changed.write_bytes(b'{}')
            messages = {'selection': 'selection changed', 'bytes': 'Replica bytes changed',
                        'receipt': 'receipt.json changed', 'plan_copy': 'records differ',
                        'wrong_plan': 'plan changed', 'topology': 'not on independent',
                        'expired': 'confirmation expired', 'metadata': 'metadata changed'}
            try:
                now = cleanup.deadline if case == 'expired' else cleanup.deadline - 1
                with self.subTest(case=case), patch.object(supervised.time, 'monotonic', return_value=now):
                    with self.assertRaisesRegex(Blocked, messages[case]):
                        cleanup.execute(plan)
                    self.assertEqual(cleanup.provider.requests, [])
                    self.assertTrue(all(not list((p / 'journal').iterdir()) for p in cleanup.outputs))
                    with self.assertRaisesRegex(Blocked, 'closed'):
                        cleanup.execute(plan)
            finally:
                if changed:
                    changed.write_bytes(original)

    def test_partial_replies_are_retained(self):
        cleanup = self.cleanup(('full-asset-photo', 'full-asset-live'))
        cleanup.provider.faults['full-asset-live'] = 'error'
        result = cleanup.execute(cleanup.prepare())
        self.assertFalse(result['complete'])
        self.assertEqual(result['outcomes'], {'full-asset-live': 'rejected', 'full-asset-photo': 'accepted'})
        self.assertEqual(inspect_run(cleanup.outputs[0])['rejected'], 1)
        self.assertEqual(len(cleanup.provider.requests), 1)

    def test_uncertain_requests_never_retry(self):
        for fault in ('lost_before', 'lost_after', 'wrong_id', 'interrupt_after'):
            cleanup = self.cleanup()
            cleanup.provider.faults['full-asset-live'] = fault
            with self.subTest(fault=fault):
                result = cleanup.execute(cleanup.prepare())
                self.assertFalse(result['complete'])
                self.assertEqual(result['outcomes'], {'full-asset-live': 'unknown'})
                self.assertEqual(inspect_run(cleanup.outputs[0])['unknown'], 1)
                with self.assertRaisesRegex(Blocked, 'closed'):
                    cleanup.execute(cleanup.plan)
                self.assertEqual(len(cleanup.provider.requests), 1)

    def test_postflight_races_stay_unknown(self):
        for case in ('kept', 'resource', 'new_upload', 'unavailable'):
            cleanup = self.cleanup()
            plan = cleanup.prepare()
            remove = cleanup.provider.remove
            def race(request):
                replies = remove(request)
                active = cleanup.provider.metadata.current['assets']
                if case == 'kept':
                    active[0]['asset_change_tag'] = 'changed'
                elif case == 'resource':
                    cleanup.provider.trash['full-asset-live']['resources'].pop()
                elif case == 'new_upload':
                    added = copy.deepcopy(active[0])
                    added['id'] = 'new-old-capture-upload'
                    active.append(added)
                else:
                    cleanup.provider.metadata.snapshot = Mock(side_effect=OSError('Synthetic read failure'))
                return replies
            cleanup.provider.remove = race
            with self.subTest(case=case):
                result = cleanup.execute(plan)
                self.assertEqual(result['outcomes'], {'full-asset-live': 'unknown'})
                self.assertEqual(len(cleanup.provider.requests), 1)

    def test_intent_must_reach_every_copy(self):
        cleanup = self.cleanup()
        plan = cleanup.prepare()
        import cleanup as demo
        write = demo.write_new
        def fail(path, value):
            if path.parent == cleanup.outputs[1] / 'journal':
                raise OSError('Synthetic disconnected artifact drive')
            write(path, value)
        with patch.object(demo, 'write_new', side_effect=fail), self.assertRaises(OSError):
            cleanup.execute(plan)
        self.assertEqual(cleanup.provider.requests, [])
        with self.assertRaisesRegex(Blocked, 'copies differ'):
            inspect_run(cleanup.outputs[0])

    def test_inspection_never_resumes_execution(self):
        cleanup = self.cleanup()
        cleanup.execute(cleanup.prepare())
        marker = cleanup.outputs[2] / 'complete.json'
        marker.unlink()
        self.assertEqual(inspect_run(cleanup.outputs[0])['state'], 'UNRESOLVED; INSPECT MANUALLY')
        self.assertEqual(len(cleanup.provider.requests), 1)
        journal = cleanup.outputs[2] / 'journal/000001.json'
        journal.write_text('{}')
        with self.assertRaisesRegex(Blocked, 'journal is incomplete'):
            inspect_run(cleanup.outputs[0])

    def test_real_drive_gate_cannot_fall_back(self):
        cleanup = self.cleanup()
        executable = self.fixture.fixture.exe
        cleanup.backups = Backups(executable)
        # Both fixture folders really are on one filesystem on every CI host.
        with self.assertRaisesRegex(Blocked, 'Deep verification failed'):
            cleanup.prepare()
        self.assertEqual(cleanup.provider.requests, [])
        previous, _ = supervised.evidence.load_input(cleanup.session.bundles[0] / 'manifest.jsonl', jsonl=True)
        fresh = copy.deepcopy(previous)
        fresh[0]['source_identity'].update(volume_id='first', physical_disks=[3])
        fresh[0]['secondary_identity'].update(volume_id='second', physical_disks=[4])
        verification = {'independent_physical_drives': 'VERIFIED BY OS TOPOLOGY'}
        with patch.object(supervised.evidence, 'fresh_replica', return_value=(fresh, 'unused', verification)) as verify:
            self.assertEqual(cleanup.backups.check(cleanup.session)['physical_drives'], 'VERIFIED BY OS TOPOLOGY')
            args, manifest = verify.call_args.args
            self.assertFalse(args.allow_unverified_devices)
            self.assertEqual(args.iarchive, executable)
            self.assertEqual(manifest, previous)
            fresh[0]['secondary_identity']['physical_disks'] = [3]
            with self.assertRaisesRegex(Blocked, 'physical backups do not match'):
                cleanup.backups.check(cleanup.session)

    def test_cli_defaults_to_cancellation(self):
        cleanup = self.cleanup()
        draft = Path(cleanup.draft.data()['outputs'][0]) / 'draft.json'
        for answer in ('', 'n', 'sure', 'y'):
            output = io.StringIO()
            argv = ['supervised.py', '--simulate', '--draft', str(draft)]
            with self.subTest(answer=answer), patch.object(sys, 'argv', argv), \
                    patch('builtins.input', return_value=answer) as prompt, redirect_stdout(output):
                self.assertEqual(supervised.main(), 0)
                prompt.assert_called_once_with('Simulate this selection? [y/N] ')
                self.assertIn('Rehearsal completed' if answer == 'y' else 'Cancelled', output.getvalue())
                self.assertNotIn('synthetic-account', output.getvalue())


if __name__ == '__main__':
    unittest.main()
