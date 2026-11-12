"""Read-only checks, internal plan bindings and default-No consent."""

from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import test_prepare_review as fixtures
import preflight
from preflight import Blocked, CHECK_SECONDS, LIMITS, MockMetadata, Preflight, Plan, canonical, digest


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EvidenceReviewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.session = self.fixture.session()
        self.session.draft(0, ['full-asset-live'])
        self.session.save(1)
        self.draft = self.session.current

    def gate(self):
        return Preflight(self.session, MockMetadata(self.draft.data()))

    def test_receipts_bind_the_saved_selection(self):
        before = {p: p.read_bytes() for root in self.session.roots for p in root.iterdir()}
        gate = self.gate()
        plan = gate.prepare()
        self.assertIs(plan, gate.plan)
        self.assertEqual(plan.data()['schema'], 2)
        data = gate.plan.data()
        self.assertEqual(data['draft_sha256'], self.draft.sha256)
        self.assertEqual([a['id'] for a in data['targets']], ['full-asset-live'])
        self.assertEqual(len(data['targets'][0]['resources']), 3)
        self.assertEqual(data['kept'], ['full-asset-photo'])
        self.assertEqual(data['bytes'], self.draft.data()['bytes'])
        receipt = gate.finish(plan)
        self.assertEqual(gate.phase, 'passed')
        self.assertEqual(gate.provider.reads, 2)
        self.assertEqual(receipt['limits'], LIMITS)
        self.assertFalse(receipt['limits']['removal_approved'])
        self.assertEqual(receipt['provider_metadata'], 'MOCK MATCHED')
        self.assertEqual(receipt['result'], 'SIMULATED_CHECK_PASSED')
        self.assertNotIn('confirmed_at', receipt)
        self.assertIn('check_started_at', receipt)
        for name in ('plan.json', 'intent.json', 'receipt.json', 'complete.json'):
            self.assertEqual(len({(p / name).read_bytes() for p in gate.outputs}), 1)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        with self.assertRaisesRegex(Blocked, 'cannot be reused'):
            gate.finish(plan)
        another = self.gate()
        self.assertNotEqual(another.prepare(), plan)
        with self.assertRaisesRegex(Blocked, 'plan changed'):
            another.finish(plan)
        from cleanup import data_for
        with self.assertRaisesRegex(Blocked, 'supervised offline cleanup plan'):
            data_for(gate.plan)

    def test_internal_plan_must_match_and_remain_fresh(self):
        for case in ('count', 'digest', 'empty', 'expired', 'expires_during_check'):
            now = [100.0]
            gate = self.gate()
            with self.subTest(case=case), patch.object(preflight.time, 'monotonic', side_effect=lambda: now[0]):
                plan = gate.prepare()
                answer = plan
                if case == 'count':
                    data = plan.data()
                    data['count'] += 1
                    answer = Plan(canonical(data), digest(data))
                elif case == 'digest':
                    answer = Plan(plan.payload, '0' * 64)
                elif case == 'empty':
                    answer = None
                elif case == 'expired':
                    now[0] += CHECK_SECONDS
                else:
                    snapshot = gate.provider.snapshot
                    def expire():
                        value = snapshot()
                        now[0] += CHECK_SECONDS
                        return value
                    gate.provider.snapshot = expire
                finding = 'expired' if case.startswith('expire') else 'plan changed'
                with self.assertRaisesRegex(Blocked, finding):
                    gate.finish(answer)
                self.assertEqual(gate.phase, 'blocked')
                self.assertTrue(all(not (p / 'intent.json').exists() for p in gate.outputs))
                with self.assertRaisesRegex(Blocked, 'cannot be reused'):
                    gate.finish(plan)

    def test_fresh_mock_metadata_must_match(self):
        for case in ('account', 'zone', 'new_item', 'kept_item', 'resource', 'unknown', 'incomplete', 'malformed', 'unavailable'):
            gate = self.gate()
            plan = gate.prepare()
            current = gate.provider.current
            if case in ('account', 'zone'):
                current['scope'][{'account': 'account_id', 'zone': 'zone_id'}[case]] = 'changed'
            elif case == 'new_item':
                added = copy.deepcopy(current['assets'][0])
                added['id'] = 'new-old-capture-upload'
                current['assets'].append(added)
            elif case == 'kept_item':
                next(a for a in current['assets'] if a['id'] == 'full-asset-photo')['asset_change_tag'] = 'new-tag'
            elif case == 'resource':
                current['assets'][0]['resources'].pop()
            elif case == 'unknown':
                current['assets'][0]['resources'][0]['resource_field'] = 'resUnknownRes'
            elif case == 'malformed':
                current['assets'] = None
            elif case == 'unavailable':
                gate.provider.snapshot = Mock(side_effect=OSError('Synthetic metadata read failure'))
            else:
                current['complete'] = False
            finding = 'unavailable or malformed' if case in ('malformed', 'unavailable') else 'Mock metadata changed'
            with self.subTest(case=case), self.assertRaisesRegex(Blocked, finding):
                gate.finish(plan)
            if case == 'unavailable':
                gate.provider.snapshot.assert_called_once_with()
            self.assertTrue(all(not (p / 'intent.json').exists() for p in gate.outputs))
            self.assertEqual(gate.phase, 'blocked')
        # Record order is irrelevant when full identities and values match.
        gate = self.gate()
        gate.provider.current['assets'].reverse()
        for asset in gate.provider.current['assets']:
            asset['resources'].reverse()
        plan = gate.prepare()
        self.assertEqual(gate.finish(plan)['result'], 'SIMULATED_CHECK_PASSED')

    def test_changed_inputs_and_media_block_checks(self):
        for case in ('selection', 'evidence', 'first', 'second', 'plan', 'used'):
            session = self.fixture.session()
            session.draft(0, ['full-asset-live'])
            session.save(1)
            gate = Preflight(session, MockMetadata(session.current.data()))
            plan = gate.prepare()
            path, original = None, None
            finding = 'Review selection changed'
            try:
                if case == 'selection':
                    session.draft(1, ['full-asset-photo'])
                elif case == 'used':
                    path = gate.outputs[0] / 'intent.json'
                    path.write_text('{}', encoding='utf-8')
                    finding = 'already used'
                else:
                    path = (self.fixture.paths[1] if case == 'evidence' else gate.outputs[-1] / 'plan.json'
                            if case == 'plan' else next(session.roots[case == 'second'].iterdir()))
                    original = path.read_bytes()
                    path.write_bytes(b'{}' if case in ('evidence', 'plan') else b'x' * len(original))
                    finding = 'Review evidence changed' if case == 'evidence' else 'plan.json changed' if case == 'plan' else 'Replica bytes changed'
                with self.subTest(case=case), self.assertRaisesRegex(Blocked, finding):
                    gate.finish(plan)
                self.assertTrue(all(not (p / 'receipt.json').exists() for p in gate.outputs))
            finally:
                if path is not None:
                    path.unlink() if original is None else path.write_bytes(original)

    def test_partial_publication_never_passes(self):
        for stage in ('plan.json', 'intent.json', 'receipt.json', 'complete.json', 'receipt_changed'):
            gate = self.gate()
            write = preflight.write_new
            def fail(path, value):
                if stage == 'receipt_changed':
                    write(path, value)
                    if path.name == 'complete.json' and path.parent == gate.outputs[-1]:
                        (gate.outputs[0] / 'receipt.json').write_text('{}', encoding='utf-8')
                    return
                if path.name == stage and path.parent == gate.outputs[-1]:
                    raise OSError('Synthetic disconnect')
                return write(path, value)
            failure, finding = (Blocked, 'receipt.json changed') if stage == 'receipt_changed' else (OSError, 'disconnect')
            with self.subTest(stage=stage), patch.object(preflight, 'write_new', side_effect=fail), self.assertRaisesRegex(failure, finding):
                plan = gate.prepare()
                gate.finish(plan)
            self.assertEqual(gate.phase, 'blocked')
            if stage != 'receipt_changed':
                self.assertFalse((gate.outputs[-1] / 'complete.json').exists())
            self.assertTrue(all((p / 'draft.json').exists() for p in map(Path, self.draft.data()['outputs'])))

    def test_provider_and_selection_boundaries(self):
        provider = Mock()
        with self.assertRaisesRegex(Blocked, 'in-memory metadata mock'):
            Preflight(self.session, provider)
        provider.snapshot.assert_not_called()
        session = self.fixture.session()
        session.save(0)
        with self.assertRaisesRegex(Blocked, 'Nothing is selected'):
            Preflight(session, MockMetadata(session.current.data()))

    def test_last_read_does_not_prove_atomicity(self):
        gate = self.gate()
        plan = gate.prepare()
        snapshot = gate.provider.snapshot
        def change_after_read():
            observed = snapshot()
            gate.provider.current['assets'][0]['asset_change_tag'] = 'changed-after-last-read'
            return observed
        with patch.object(gate.provider, 'snapshot', side_effect=change_after_read):
            receipt = gate.finish(plan)
        self.assertEqual(receipt['limits']['server_version_condition'], 'NOT VERIFIED')
        self.assertFalse(receipt['limits']['removal_approved'])
        self.assertEqual(receipt['limits']['live_execution'], 'UNAVAILABLE')

    def test_cli_accepts_yes_and_defaults_to_no(self):
        draft = Path(self.draft.data()['outputs'][0]) / 'draft.json'
        prepare = Preflight.prepare
        for answer in ('y', 'YES', ' yes ', '', 'n', 'no', 'wrong', EOFError, KeyboardInterrupt):
            output, errors = io.StringIO(), io.StringIO()
            approved = isinstance(answer, str) and answer.strip().casefold() in ('y', 'yes')
            def reply(prompt):
                self.assertEqual(prompt, 'Run read-only check? [y/N] ')
                if not isinstance(answer, str):
                    raise answer
                return answer
            with self.subTest(answer=answer), patch.object(sys, 'argv', ['preflight.py', '--simulate', '--draft', str(draft)]), \
                    patch('builtins.input', side_effect=reply) as choice, \
                    patch.object(Preflight, 'prepare', autospec=True, side_effect=prepare) as prepared, \
                    redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(preflight.main(), 0)
            self.assertEqual(choice.call_count, 1)
            self.assertEqual(prepared.call_count, int(approved))
            self.assertNotIn('full-asset-live', output.getvalue() + errors.getvalue())
            self.assertNotIn(self.draft.sha256, output.getvalue())
            self.assertIn('in-memory model', output.getvalue())
            self.assertIn('Check passed' if approved else 'Cancelled', output.getvalue())

    def test_cli_rejects_selection_changed_at_prompt(self):
        draft = Path(self.draft.data()['outputs'][0]) / 'draft.json'
        def reply(_prompt):
            self.session.draft(1, ['full-asset-photo'])
            self.session.save(2)
            return 'y'
        with patch.object(sys, 'argv', ['preflight.py', '--simulate', '--draft', str(draft)]), \
                patch.object(preflight.ReviewDraft, 'reopen', return_value=self.session), \
                patch('builtins.input', side_effect=reply), patch.object(Preflight, 'prepare') as prepared, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(preflight.main(), 1)
        prepared.assert_not_called()


if __name__ == '__main__':
    unittest.main()
