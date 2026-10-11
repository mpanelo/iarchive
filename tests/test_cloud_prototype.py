"""Include the standalone offline experiment in the existing Python check."""

from pathlib import Path
import subprocess
import sys
import unittest


class OfflineCloudPrototypeTests(unittest.TestCase):
    def test_adversarial_policy_suite(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tools/cloud-prototype", "-v"],
            cwd=root, capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_read_only_probe_scope_guards(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tools/cloud-probe", "-v"],
            cwd=root, capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
