"""Standalone video recovery and isolation of fixed research scopes."""

from dataclasses import FrozenInstanceError
import copy
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_live_photo_trial as fixtures
from test_cleanup_trial import MASTER, ROOT
from requests.adapters import HTTPAdapter

recovery = fixtures.live


class VideoTrialTests(unittest.TestCase):
    def setUp(self):
        # Reuse fixture construction, not inherited Live Photo test methods.
        self.fixture = fixtures.LivePhotoTrialTests()
        self.fixture.profile = recovery.VIDEO
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_offline_and_completed_recovery(self):
        for case in ("offline", "success", "lostafter"):
            with self.subTest(case=case):
                result, summary, state, run, authenticate = self.fixture.run_cli(case, execute=case != "offline")
                self.assertEqual(result, 0, summary)
                self.assertEqual(summary["scope"], "one_disposable_video_trial")
                self.assertEqual((state["writes"], state["gets"], state["lookups"]),
                                 (0, 0, 0) if case == "offline" else (1, 2, 3))
                self.assertEqual(state["get_fields"], [] if case == "offline" else ["resOriginalRes"] * 2)
                self.assertEqual((run / 'recovery-original.mov').read_bytes(), self.fixture.payloads['resOriginalRes'])
                if case == "offline":
                    authenticate.assert_not_called()
                    self.assertEqual(state["calls"], [])
                    self.assertEqual(summary["limits"]["restored_remote_bytes"], "NOT VERIFIED")
                else:
                    self.assertEqual(summary['phase'], 'completed')
                    self.assertEqual(summary['reply'], 'UNKNOWN' if case == 'lostafter' else 'ACCEPTED')
                    self.assertTrue(summary['restored_originals_sha256_verified'])
                    self.assertEqual(summary['restored_playback'], 'USER REPORTED')
                    self.assertEqual(summary['limits']['restored_remote_bytes'], 'VERIFIED FOR ONE TEST VIDEO')
                    self.assertEqual((run / 'restored-original.mov').read_bytes(), self.fixture.payloads['resOriginalRes'])
                    self.assertFalse(state['removed'])
                    plan = json.loads((run / 'remove-plan-private.json').read_text())
                    self.assertEqual((plan['max_media_gets'], plan['max_resource_bytes']), (2, 8 * 1024 * 1024))

    def test_video_selection_fails_closed(self):
        path = self.fixture.baseline_dir / 'album-private.json'
        cases = {'missing': 'Exactly one video master', 'multiple': 'Exactly one video master',
                 'motion': 'outside this trial', 'edited': 'outside this trial',
                 'oversized': 'Exact original resources'}
        previous = path.read_bytes()
        for case, finding in cases.items():
            try:
                album = copy.deepcopy(self.fixture.baseline)
                for observation in album['passes']:
                    master = next(r for r in observation['masters'] if r['recordName'] == MASTER)
                    if case == 'missing':
                        master['fields']['resOriginalFileType'] = 'public.heic'
                    elif case == 'multiple':
                        observation['masters'][0]['fields']['resOriginalFileType'] = 'com.apple.quicktime-movie'
                    elif case == 'motion':
                        master['fields']['resOriginalVidComplRes'] = copy.deepcopy(master['fields']['resOriginalRes'])
                    elif case == 'edited':
                        master['fields']['adjustmentType'] = 1
                    else:
                        master['fields']['resOriginalRes']['size'] = recovery.VIDEO.max_resource + 1
                self.fixture.write(path, album)
                with self.subTest(case=case):
                    result, summary, state, _, authenticate = self.fixture.run_cli(case)
                    self.assertEqual(result, 1)
                    self.assertIn(finding, summary['finding'])
                    authenticate.assert_not_called()
                    self.assertEqual(state['calls'], [])
            finally:
                path.write_bytes(previous)

    def test_read_failures_prevent_removal(self):
        cases = {'beforebytes': (1, 'differs from the preserved bytes'),
                 'preflight': (1, 'Album changed while preserving')}
        for case, (gets, finding) in cases.items():
            with self.subTest(case=case):
                result, summary, state, run, _ = self.fixture.run_cli(case)
                self.assertEqual(result, 1, summary)
                self.assertEqual((state['writes'], state['gets']), (0, gets))
                self.assertFalse((run / 'write-intent-private.json').exists())
                if finding:
                    self.assertIn(finding, summary['finding'])

    def test_write_and_recovery_outcomes(self):
        cases = {'restoreresource': (1, 'resource metadata changed'),
                 'restoredbytes': (2, 'differs from the preserved bytes'),
                 'playback': (2, 'Playback remains unverified')}
        for case, (gets, finding) in cases.items():
            with self.subTest(case=case):
                result, summary, state, run, _ = self.fixture.run_cli(case)
                self.assertEqual(result, 1, summary)
                self.assertEqual((state['writes'], state['gets']), (1, gets))
                self.assertNotEqual(summary['phase'], 'completed')
                self.assertEqual(summary['restored_playback'], 'NOT VERIFIED')
                self.assertEqual(summary.get('restored_originals_sha256_verified', False), case == 'playback')
                self.assertTrue((run / 'write-intent-private.json').exists())
                self.assertTrue((run / 'write-outcome-private.json').exists())
                if finding:
                    self.assertIn(finding, summary['finding'])

    def test_profiles_and_entrypoints_stay_fixed(self):
        repo = Path(__file__).resolve().parents[2]
        for profile, filename, maximum, gets in (
                (recovery.LIVE_PHOTO, 'live_photo_trial.py', 4 * 1024 * 1024, 4),
                (recovery.VIDEO, 'video_trial.py', 8 * 1024 * 1024, 2)):
            with self.subTest(profile=profile.key):
                self.assertEqual((profile.max_resource, profile.max_gets), (maximum, gets))
                with self.assertRaises(FrozenInstanceError):
                    profile.max_resource = 100 * 1024 * 1024
                resources = profile.resource_map
                resources.clear()
                self.assertTrue(profile.resources)
                with patch.object(recovery, 'main', return_value=0) as main, self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(repo / 'tools/cloud-cleanup' / filename), run_name='__main__')
                self.assertEqual(stopped.exception.code, 0)
                main.assert_called_once_with(profile)
        path = self.fixture.repo / '.smoke/video-size.mov'
        payload = b'x' * (5 * 1024 * 1024)
        path.write_bytes(payload)
        self.assertEqual(recovery.media_bytes(self.fixture.repo, path, profile=recovery.VIDEO), payload)
        with self.assertRaisesRegex(recovery.trial.TrialBlocked, 'bounded nonempty'):
            recovery.media_bytes(self.fixture.repo, path, profile=recovery.LIVE_PHOTO)
        with self.assertRaisesRegex(recovery.trial.TrialBlocked, 'bounded nonempty'):
            recovery.trial.original_bytes(self.fixture.repo, path)
        run = self.fixture.repo / '.smoke/cloud-probe-profile'
        run.mkdir()
        binding, _, inputs = recovery.prepare(self.fixture.repo, self.fixture.baseline_dir.name,
                                               self.fixture.downloads.name, run, profile=recovery.VIDEO)
        with self.assertRaisesRegex(recovery.trial.TrialBlocked, 'profile does not match'):
            recovery.verify_preserved(self.fixture.repo, binding, inputs, run, profile=recovery.LIVE_PHOTO)
        for change, finding in (('size', 'fixed profile byte limit'), ('profile', 'profile does not match')):
            wire = recovery.RecoveryWire(SimpleNamespace(), ROOT, binding, recovery.Pacer(), {}, profile=recovery.VIDEO)
            if change == 'size':
                wire.binding['resources']['resOriginalRes']['size'] = recovery.VIDEO.max_resource + 1
            else:
                wire.profile = recovery.LIVE_PHOTO
            with self.subTest(change=change), patch.object(HTTPAdapter, 'send') as send, \
                    self.assertRaisesRegex(recovery.trial.TrialBlocked, finding):
                recovery.download_resources(wire, self.fixture.urls, run, 'before')
            send.assert_not_called()

    def test_completed_trial_supplies_baseline(self):
        # The video starts from the completed Live Photo's final album capture.
        path = self.fixture.baseline_dir / 'summary.json'
        self.fixture.write(path, {'schema': 1, 'provider_version': recovery.PIN,
                           'scope': 'one_disposable_live_photo_trial', 'phase': 'completed',
                           'restored_originals_sha256_verified': True})
        self.fixture.write(self.fixture.baseline_dir / 'final-album-private.json', self.fixture.baseline)
        result, summary, state, _, authenticate = self.fixture.run_cli('fromlive', execute=False)
        self.assertEqual(result, 0, summary)
        authenticate.assert_not_called()
        self.assertEqual(state['calls'], [])
        self.fixture.write(path, {'schema': 1, 'provider_version': recovery.PIN,
                           'scope': 'one_disposable_live_photo_trial', 'phase': 'playback_confirmation',
                           'restored_originals_sha256_verified': True})
        result, summary, state, _, authenticate = self.fixture.run_cli('incompletebaseline')
        self.assertEqual(result, 1)
        self.assertIn('completed five-item metadata baseline', summary['finding'])
        authenticate.assert_not_called()
        self.assertEqual(state['calls'], [])


if __name__ == '__main__':
    unittest.main()
