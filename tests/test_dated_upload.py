"""Verify the upload fixture through the project's actual metadata reader."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fixtures"))
from dated_upload import generate


class DatedUploadTests(unittest.TestCase):
    def test_unique_jpeg_retains_capture_date(self):
        tool = ROOT / (".windows-sandbox/exiftool/exiftool.exe" if os.name == "nt"
                       else ".tools/exiftool/exiftool")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first, second = generate(root / "one"), generate(root / "two")
            self.assertNotEqual(hashlib.sha256(first.read_bytes()).digest(),
                                hashlib.sha256(second.read_bytes()).digest())
            with Image.open(first) as image:
                image.load()
                self.assertEqual((image.format, image.size), ("JPEG", (640, 360)))
            result = subprocess.run([str(tool), "-j", "-DateTimeOriginal", "-CreateDate",
                                     "-OffsetTimeOriginal", str(first)],
                                    capture_output=True, text=True, check=True, timeout=30)
            metadata = json.loads(result.stdout)[0]
            self.assertEqual(metadata["DateTimeOriginal"], "2001:01:02 03:04:05")
            self.assertEqual(metadata["CreateDate"], "2001:01:02 03:04:05")
            self.assertEqual(metadata["OffsetTimeOriginal"], "+00:00")
            before = first.read_bytes()
            with self.assertRaises(FileExistsError):
                generate(root / "one")
            self.assertEqual(first.read_bytes(), before)

    def test_linked_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / "target"
            target.mkdir()
            link = root / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as error:
                self.skipTest(str(error))
            for output in (link, link / "new"):
                with self.subTest(output=output), self.assertRaisesRegex(ValueError, "Linked"):
                    generate(output)
            self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
