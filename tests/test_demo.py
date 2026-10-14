"""Exercise the guided script's command wiring and copy confirmation."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.demo import ROOT, run_demo, safe_directory


class DemoTests(unittest.TestCase):
    def test_demo_rejects_linked_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "target"
            target.mkdir()
            link = root / "linked"
            if os.name == "nt":
                subprocess.run(["cmd", "/C", "mklink", "/J", str(link), str(target)],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            else:
                link.symlink_to(target, target_is_directory=True)
            try:
                with self.assertRaisesRegex(RuntimeError, "Unsafe demo directory"):
                    safe_directory(link / "new-run")
                self.assertEqual(list(target.iterdir()), [])
            finally:
                if os.name == "nt":
                    link.rmdir()
                else:
                    link.unlink()

    def test_walkthrough_and_copy_refusal(self):
        target = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target"))
        executable = target / "debug" / (
            "iarchive.exe" if os.name == "nt" else "iarchive")
        self.assertTrue(executable.is_file(), "Run cargo test --workspace first.")
        for accepted in (False, True):
            with self.subTest(accepted=accepted), redirect_stdout(StringIO()), \
                    patch("builtins.input", return_value="n"):
                run = run_demo(executable, interactive=not accepted)
                reports = run / "reports"
                preview = json.loads((reports / "preview.json").read_text())
                self.assertFalse(preview["complete"])
                primary = run / "primary" / "media"
                secondary = run / "secondary" / "media"
                if not accepted:
                    self.assertEqual(list(secondary.iterdir()), [])
                    self.assertFalse((reports / "replicate.json").exists())
                    self.assertFalse((reports / "verify.json").exists())
                    continue
                verified = json.loads((reports / "verify.json").read_text())
                self.assertTrue(verified["complete"])
                self.assertEqual(verified["local_replica"], "LOCAL BYTES VERIFIED")
                self.assertEqual(verified["independent_physical_drives"], "NOT VERIFIED")
                self.assertEqual(verified["cloud_coverage"], "NOT VERIFIED")
                originals = {path.relative_to(primary): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in primary.rglob("*") if path.is_file()}
                copies = {path.relative_to(secondary): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in secondary.rglob("*") if path.is_file()}
                self.assertEqual(len(originals), 9)
                self.assertEqual(originals, copies)
                self.assertTrue((run / "inventory.jsonl").is_file())
                self.assertTrue((reports / "status.json").is_file())


if __name__ == "__main__":
    unittest.main()
