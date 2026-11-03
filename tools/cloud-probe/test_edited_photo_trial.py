"""Edited-photo recovery binds original and rendition to distinct owners."""

import copy
import json
import unittest

import test_live_photo_trial as fixtures
from test_cleanup_trial import MASTER, TARGET

recovery = fixtures.live


class EditedPhotoTrialTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LivePhotoTrialTests()
        self.fixture.profile = recovery.EDITED_PHOTO
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_original_and_rendition_recover(self):
        inputs = {p: p.read_bytes() for p in self.fixture.downloads.rglob('*') if p.is_file()}
        for case in ('offline', 'success', 'lostafter'):
            with self.subTest(case=case):
                result, summary, state, run, authenticate = self.fixture.run_cli(case, execute=case != 'offline')
                self.assertEqual(result, 0, summary)
                self.assertEqual(summary['scope'], 'one_disposable_edited_photo_trial')
                self.assertEqual((state['writes'], state['gets'], state['lookups']),
                                 (0, 0, 0) if case == 'offline' else (1, 4, 3))
                plan = json.loads((run / 'offline-plan-private.json').read_text())
                self.assertEqual((plan['max_resource_bytes'], plan['max_media_gets']), (4 * 1024 * 1024, 4))
                for field, owner, identity, kind in (('resOriginalRes', 'master', MASTER, 'CPLMaster'),
                                                     ('resJPEGFullRes', 'asset', TARGET, 'CPLAsset')):
                    resource = plan['binding']['resources'][field]
                    self.assertEqual((resource['owner'], resource['record_id'], resource['record_type']),
                                     (owner, identity, kind))
                    filename = self.fixture.resources[field][0]
                    self.assertEqual((run / ('recovery-' + filename)).read_bytes(), self.fixture.payloads[field])
                    if case != 'offline':
                        self.assertEqual((run / ('restored-' + filename)).read_bytes(), self.fixture.payloads[field])
                if case == 'offline':
                    authenticate.assert_not_called()
                    self.assertEqual(state['calls'], [])
                    self.assertEqual(summary['restored_crop'], 'NOT VERIFIED')
                else:
                    self.assertEqual(summary['phase'], 'completed')
                    self.assertEqual(summary['reply'], 'UNKNOWN' if case == 'lostafter' else 'ACCEPTED')
                    self.assertTrue(summary['restored_originals_sha256_verified'])
                    self.assertTrue(summary['restored_edited_rendition_sha256_verified'])
                    self.assertEqual(summary['restored_crop'], 'USER REPORTED')
                    self.assertNotIn('restored_playback', summary)
                    self.assertEqual(state['get_fields'], ['resOriginalRes', 'resJPEGFullRes'] * 2)
                    self.assertFalse(state['removed'])
        self.assertEqual(inputs, {p: p.read_bytes() for p in inputs})

    def test_edited_scope_rejects_ambiguity(self):
        path = self.fixture.baseline_dir / 'album-private.json'
        previous = path.read_bytes()
        cases = {'missing': 'Exact original resources', 'moved': 'Exact original resources',
                 'duplicate': 'Exact original resources', 'type': 'Exact original resources',
                 'oversized': 'Exact original resources', 'adjustment': 'outside this trial',
                 'unadjusted': 'outside this trial', 'extra': 'outside this trial',
                 'multiple': 'Exactly one edited photo asset'}
        for case, finding in cases.items():
            try:
                album = copy.deepcopy(self.fixture.baseline)
                for observation in album['passes']:
                    asset = next(r for r in observation['assets'] if r['recordName'] == TARGET)
                    master = next(r for r in observation['masters'] if r['recordName'] == MASTER)
                    fields = asset['fields']
                    if case == 'missing':
                        fields.pop('resJPEGFullRes')
                    elif case in ('moved', 'duplicate'):
                        master['fields']['resJPEGFullRes'] = copy.deepcopy(fields['resJPEGFullRes'])
                        master['fields']['resJPEGFullFileType'] = 'public.heic'
                        if case == 'moved':
                            fields.pop('resJPEGFullRes')
                    elif case == 'type':
                        fields['resJPEGFullFileType'] = 'public.jpeg'
                    elif case == 'oversized':
                        fields['resJPEGFullRes']['size'] = recovery.EDITED_PHOTO.max_resource + 1
                    elif case in ('adjustment', 'unadjusted'):
                        fields['adjustmentType'] = 'unknown.editor' if case == 'adjustment' else 0
                    elif case == 'extra':
                        fields['resSidecarRes'] = copy.deepcopy(fields['resJPEGFullRes'])
                    else:
                        observation['assets'][0]['fields']['resJPEGFullRes'] = copy.deepcopy(fields['resJPEGFullRes'])
                self.fixture.write(path, album)
                with self.subTest(case=case):
                    result, summary, state, _, authenticate = self.fixture.run_cli(case)
                    self.assertEqual(result, 1)
                    self.assertIn(finding, summary['finding'])
                    authenticate.assert_not_called()
                    self.assertEqual(state['calls'], [])
            finally:
                path.write_bytes(previous)

    def test_manifest_binds_rendition_owner(self):
        previous = self.fixture.manifest.read_bytes()
        for field, record_id, record_type in (('resOriginalRes', TARGET, 'CPLAsset'),
                                             ('resJPEGFullRes', MASTER, 'CPLMaster'),
                                             ('resJPEGFullRes', 'other-asset', 'CPLAsset')):
            try:
                manifest = json.loads(previous)
                resource = next(r for r in manifest['resources'] if r['resource_field'] == field)
                resource.update(resource_record_id=record_id, resource_record_type=record_type)
                self.fixture.write(self.fixture.manifest, manifest)
                with self.subTest(field=field, owner=record_id):
                    result, summary, state, _, authenticate = self.fixture.run_cli('owner' + record_id + field)
                    self.assertEqual(result, 1)
                    self.assertIn('download binding or hashes differ', summary['finding'])
                    authenticate.assert_not_called()
                    self.assertEqual(state['calls'], [])
            finally:
                self.fixture.manifest.write_bytes(previous)

    def test_rendition_changes_block_completion(self):
        cases = {'lookupowner': (0, 0, 'Exact original resources'),
                 'beforebytes': (0, 2, 'differs from the preserved bytes'),
                 'restoreresource': (1, 2, 'resource metadata changed'),
                 'restoreedit': (1, 2, 'outside this trial'),
                 'restoredbytes': (1, 4, 'differs from the preserved bytes'),
                 'crop': (1, 4, 'Crop remains unverified')}
        for case, (writes, gets, finding) in cases.items():
            with self.subTest(case=case):
                result, summary, state, run, _ = self.fixture.run_cli(case)
                self.assertEqual(result, 1, summary)
                self.assertIn(finding, summary['finding'])
                self.assertEqual((state['writes'], state['gets']), (writes, gets))
                self.assertNotEqual(summary['phase'], 'completed')
                self.assertEqual(summary['restored_crop'], 'NOT VERIFIED')
                self.assertEqual(summary.get('restored_edited_rendition_sha256_verified', False), case == 'crop')
                self.assertEqual((run / 'write-intent-private.json').exists(), writes == 1)


if __name__ == '__main__':
    unittest.main()
