"""Exercise the pinned upstream MFA helper with fake accounts and no network."""

from contextlib import redirect_stdout
import io
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from probe import verify_second_factor


class MfaTests(unittest.TestCase):
    def setUp(self):
        try:
            from icloudpd import authentication
        except ImportError:
            self.skipTest("Optional provider integration: run in tools/cloud-probe environment")
        self.authentication = authentication
        guard = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.calls = []
        calls = self.calls

        class FakeService:
            def get_trusted_phone_numbers(self):
                calls.append("phones")
                return [SimpleNamespace(id=7, obfuscated_number="***-***-0100")]

            def trigger_push_notification(self):
                calls.append("push")
                return True

            def send_2fa_code_sms(self, device_id):
                calls.append(("sms", device_id))
                return True

            def validate_2fa_code(self, code):
                calls.append(("validate_push", code))
                return True

            def validate_2fa_code_sms(self, device_id, code):
                calls.append(("validate_sms", device_id, code))
                return True

        self.service = FakeService()

    def test_push_is_requested_before_code_entry(self):
        output = io.StringIO()
        with patch("probe.getpass.getpass", return_value="123456"), redirect_stdout(output):
            verify_second_factor(self.service)
        self.assertEqual(self.calls, ["phones", "push", ("validate_push", "123456")])
        self.assertNotIn("123456", output.getvalue())

    def test_sms_choice_requests_delivery_before_validation(self):
        output = io.StringIO()
        with patch("probe.getpass.getpass", side_effect=["a", "654321"]), redirect_stdout(output):
            verify_second_factor(self.service)
        self.assertEqual(self.calls, ["phones", "push", ("sms", 7), ("validate_sms", 7, "654321")])
        self.assertNotIn("654321", output.getvalue())

    def test_failed_push_still_offers_sms(self):
        output = io.StringIO()
        with patch.object(self.service, "trigger_push_notification", return_value=False), \
                patch("probe.getpass.getpass", side_effect=["a", "654321"]), redirect_stdout(output):
            verify_second_factor(self.service)
        self.assertIn(("sms", 7), self.calls)
        self.assertIn(("validate_sms", 7, "654321"), self.calls)


if __name__ == "__main__":
    unittest.main()
