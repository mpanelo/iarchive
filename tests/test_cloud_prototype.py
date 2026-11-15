"""Include the standalone offline experiment in the existing Python check."""

from pathlib import Path
import subprocess
import sys
import unittest


class OfflineCloudPrototypeTests(unittest.TestCase):
    def test_policy_suite(self):
        root = Path(__file__).resolve().parents[1]
        for suite in ("cloud-prototype", "cloud-cleanup"):
            with self.subTest(suite=suite):
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", "unittest", "discover", "-s", f"tools/{suite}", "-v"],
                        cwd=root, capture_output=True, text=True, timeout=300, check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    progress = "\n".join(output.decode(errors="replace") if isinstance(output, bytes) else output
                                         for output in (error.stdout, error.stderr) if output)
                    self.fail(f"{suite} exceeded the five-minute suite limit. Captured progress:\n{progress}")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
