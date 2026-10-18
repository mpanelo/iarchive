"""Project-local extraction and reuse guards, with no network or system installers."""
import hashlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import setup_metadata as setup


class MetadataSetupTests(unittest.TestCase):
    def test_portable_extraction_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name, linked in [("package/../../escape", False), ("package/link", True)]:
                with self.subTest(name=name):
                    archive = root / "bad.zip"
                    with zipfile.ZipFile(archive, "w") as stream:
                        item = zipfile.ZipInfo(name)
                        if linked:
                            item.create_system = 3
                            item.external_attr = (stat.S_IFLNK | 0o777) << 16
                        stream.writestr(item, "outside")
                    with self.assertRaisesRegex(ValueError, "Unsafe|Linked"):
                        setup.unpack(archive, root, "nt")
                    self.assertFalse((root.parent / "escape").exists())
            archive = root / "tool.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("package/", b"")
                stream.writestr("package/exiftool(-k).exe", b"synthetic executable")
                stream.writestr("package/exiftool_files/perl.exe", b"synthetic dependency")
            target = root / "unpacked"
            target.mkdir()
            setup.unpack(archive, target, "nt")
            self.assertEqual((target / "exiftool.exe").read_bytes(), b"synthetic executable")
            self.assertTrue((target / "exiftool_files/perl.exe").is_file())

    def test_setup_reuses_completed_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            exe = "exiftool.exe" if os.name == "nt" else "exiftool"
            tools = root / (".windows-sandbox" if os.name == "nt" else ".tools")
            payload = (b"synthetic executable" if os.name == "nt" else
                       b"#!/bin/sh\nprintf '%s\\n' 'synthetic metadata reader'\n")
            data = io.BytesIO()
            if os.name == "nt":
                with zipfile.ZipFile(data, "w") as stream:
                    stream.writestr("package/exiftool(-k).exe", payload)
            else:
                with tarfile.open(fileobj=data, mode="w:gz") as stream:
                    item = tarfile.TarInfo("package/exiftool")
                    item.mode = 0o644  # Setup must set safe executable permissions itself.
                    item.size = len(payload)
                    stream.addfile(item, io.BytesIO(payload))
            content = data.getvalue()
            pin = ("https://example.invalid/fixture", hashlib.sha256(content).hexdigest())
            with patch.object(setup, "ROOT", root), patch.dict(setup.DOWNLOADS, {os.name: pin}), \
                    patch.object(setup.urllib.request, "urlopen", return_value=io.BytesIO(content)) as network:
                setup.setup()
                installed = tools / "exiftool" / exe
                if os.name == "posix":
                    result = subprocess.run([str(installed)], capture_output=True, text=True,
                                            check=True, timeout=10)
                    self.assertEqual(result.stdout.strip(), "synthetic metadata reader")
                    self.assertEqual(installed.stat().st_mode & 0o111, stat.S_IXUSR)
                    installed.chmod(installed.stat().st_mode & ~0o111)
                    self.assertFalse(os.access(installed, os.X_OK))
                setup.setup()
                self.assertEqual(network.call_count, 1)
                self.assertEqual(installed.read_bytes(), payload)
                if os.name == "posix":
                    result = subprocess.run([str(installed)], capture_output=True, text=True,
                                            check=True, timeout=10)
                    self.assertEqual(result.stdout.strip(), "synthetic metadata reader")
                    self.assertEqual(installed.stat().st_mode & 0o111, stat.S_IXUSR)
            self.assertEqual(len(list(tools.iterdir())), 1)
            with patch.object(setup, "ROOT", root):
                (tools / "exiftool/iarchive-version.txt").write_text("wrong")
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    setup.setup()


if __name__ == "__main__":
    unittest.main()
