"""Guided launcher with actual local checks, modeled topology and fake account access."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud-cleanup'))
import run as launcher
import supervised
from prototype import Blocked
import test_cloud_workflow as fixtures
import probe
import test_album as album


class CloudLauncherTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CloudWorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.fixture.zones = {**self.fixture.zones, 'shared': []}
        self.service.requires_2fa = self.service.requires_2sa = False
        self.review = self.fixture.session
        self.root = self.fixture.fixture.fixture.root
        self.draft = Path(self.review.current.data()['outputs'][0]) / 'draft.json'

    def invoke(self, answers=('y', 'y'), args=None, *, authentication_error=None):
        output, account_runs = io.StringIO(), []
        new_run, cleanup = probe.new_run, launcher.Cleanup
        def private_run(_repo):
            run = new_run(self.root)
            account_runs.append(run)
            return run
        def capture(review, provider, backups):
            flow = cleanup(review, provider, backups)
            self.fixture.flow, self.fixture.provider = flow, provider
            return flow
        argv = ['run.py'] + (args if args is not None else ['--draft', str(self.draft), '--album', album.NAME,
                                                          '--iarchive', str(self.fixture.fixture.fixture.fixture.exe)])
        with patch.object(sys, 'argv', argv), patch('builtins.input', side_effect=answers), \
                patch.object(sys.stdin, 'isatty', return_value=True), patch.object(sys.stderr, 'isatty', return_value=True), \
                patch('pyicloud_ipd.PyiCloudService', return_value=self.service, side_effect=authentication_error) as authenticate, \
                patch.object(launcher.getpass, 'getpass', return_value='fake@example.invalid') as credentials, \
                patch.object(launcher, 'new_run', side_effect=private_run), \
                patch.object(launcher, 'discover_zones', return_value=(self.fixture.zones, [])) as discovery, \
                patch.object(launcher, 'Cleanup', side_effect=capture), redirect_stdout(output):
            result = launcher.main()
        self.assertNotIn('SECRET', output.getvalue())
        self.assertNotIn('fake@example.invalid', output.getvalue())
        summaries = [json.loads((run / 'summary.json').read_text()) for run in account_runs]
        self.discovery = discovery
        return result, summaries, authenticate, credentials, output.getvalue()

    def test_cleanup_reaches_only_the_confirmed_request(self):
        result, summaries, authenticate, credentials, output = self.invoke()
        self.assertEqual(result, 0, output)
        self.assertEqual(self.fixture.calls.count('modify'), 1)
        self.assertEqual(summaries[0]['status'], 'completed')
        self.assertEqual(summaries[0]['modification_requests'], 1)
        self.assertTrue(summaries[0]['authenticated'])
        authenticate.assert_called_once()
        credentials.assert_called_once()
        self.assertIn('Cleanup records:', output)

    def test_declining_signin_never_accesses_accounts(self):
        result, summaries, authenticate, credentials, output = self.invoke(answers=('',))
        self.assertEqual(result, 0)
        self.assertEqual(summaries, [])
        authenticate.assert_not_called()
        credentials.assert_not_called()
        self.assertEqual(self.fixture.calls, [])
        self.assertIn('Cancelled', output)

    def test_local_checks_prevent_account_access(self):
        for case in ('drive', 'recovery'):
            args = ['--recover', '--run', str(self.root / 'missing-run')] if case == 'recovery' else None
            with self.subTest(case=case), patch.object(launcher.Backups, 'check', side_effect=Blocked('Synthetic drive unavailable')):
                result, summaries, authenticate, credentials, output = self.invoke(answers=(), args=args)
                self.assertEqual(result, 1)
                self.assertEqual(summaries, [])
                authenticate.assert_not_called()
                credentials.assert_not_called()
                self.assertEqual(self.fixture.calls, [])

    def test_declining_removal_preserves_the_draft(self):
        result, summaries, _, _, output = self.invoke(answers=('y', ''))
        self.assertEqual(result, 0)
        self.assertEqual(summaries[0]['status'], 'cancelled')
        self.assertEqual(summaries[0]['modification_requests'], 0)
        self.assertNotIn('modify', self.fixture.calls)
        self.assertFalse(any((Path(p) / 'cleanup-started.json').exists() for p in self.review.current.data()['outputs']))
        self.assertIn('No removal request was sent', output)

    def test_authentication_errors_do_not_expose_secrets(self):
        result, summaries, _, _, output = self.invoke(answers=('y',), authentication_error=RuntimeError('SECRET_AUTH_TOKEN'))
        self.assertEqual(result, 1)
        self.assertFalse(summaries[0]['authenticated'])
        self.assertEqual(summaries[0]['modification_requests'], 0)
        self.assertEqual(self.fixture.calls, [])
        self.assertIn('Stopped before removal', output)

    def test_wrong_account_blocks_library_discovery(self):
        self.service.data['dsInfo']['dsid'] = self.service.params['dsid'] = 'other-account'
        result, summaries, _, _, output = self.invoke(answers=('y',))
        self.assertEqual(result, 1)
        self.assertFalse(summaries[0]['authenticated'])
        self.discovery.assert_not_called()
        self.assertEqual(self.fixture.calls, [])

    def test_second_factor_uses_local_verification(self):
        self.service.requires_2fa = True
        with patch.object(launcher, 'verify_second_factor') as verify:
            result, summaries, _, _, output = self.invoke(answers=('y', ''))
        self.assertEqual(result, 0)
        verify.assert_called_once_with(self.service)
        self.assertNotIn('modify', self.fixture.calls)

    def test_uncertain_write_is_not_reported_unchanged(self):
        self.fixture.fault = 'lookup'
        result, summaries, _, _, output = self.invoke()
        self.assertEqual(result, 1)
        self.assertEqual(summaries[0]['modification_requests'], 1)
        self.assertEqual(summaries[0]['status'], 'stopped')
        self.assertIn('unresolved', output)
        self.assertNotIn('Stopped before removal', output)
        self.assertEqual(self.fixture.calls.count('modify'), 1)

    def test_failed_result_retains_uncertain_write(self):
        write = supervised.write_new
        def fail(path, value):
            if path.name == 'result.json':
                raise OSError('Synthetic disconnected result drive')
            write(path, value)
        with patch.object(supervised, 'write_new', side_effect=fail):
            result, summaries, _, _, output = self.invoke()
        self.assertEqual(result, 1)
        self.assertEqual(summaries[0]['modification_requests'], 1)
        self.assertIn('Cloud changes may have occurred', output)
        self.assertNotIn('Stopped before removal', output)
        self.assertEqual(self.fixture.calls.count('modify'), 1)

    def test_recovery_never_repeats_the_write(self):
        flow = self.fixture.workflow()
        flow.prepare()
        self.fixture.fault = 'lost_after'
        self.assertFalse(self.fixture.confirm(flow)['complete'])
        self.fixture.fault = None
        original = [(output / 'result.json').read_bytes() for output in flow.outputs]
        result, summaries, _, _, output = self.invoke(answers=('y',), args=['--recover', '--run', str(flow.outputs[0])])
        self.assertEqual(result, 0, output)
        self.assertEqual(summaries[0]['scope'], 'read_only_cleanup_recovery')
        self.assertEqual(summaries[0]['modification_requests'], 0)
        self.assertEqual(original, [(output / 'result.json').read_bytes() for output in flow.outputs])
        self.assertEqual(self.fixture.calls.count('modify'), 1)
        self.assertIn('Original outcomes are unchanged', output)


if __name__ == '__main__':
    unittest.main()
