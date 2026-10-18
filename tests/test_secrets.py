"""Exercise our scanner wiring and push boundaries using synthetic repositories."""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_secrets as guard

PROJECT = Path(__file__).resolve().parents[1]


class SecretTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Required integration dependency, installed by project setup and CI.
        cls.scanner = guard.require_scanner(PROJECT)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="iarchive-secret-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "checkout"
        self.root.mkdir()
        self.remote = self.root.parent / "remote.git"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("GIT_")}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_TERMINAL_PROMPT="0")
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Synthetic test")
        self.git("config", "user.email", "test@example.invalid")
        subprocess.run(["git", "init", "--bare", str(self.remote)], env=self.env,
                       check=True, capture_output=True)
        self.git("remote", "add", "origin", str(self.remote))
        for name in [".gitleaks.toml", ".gitattributes", ".githooks/pre-push", "scripts/check_secrets.py"]:
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / name, destination)
        (self.root / ".gitignore").write_text(".tools/\n.windows-sandbox/\n.smoke/\n")
        exe = guard.executable(self.root)
        exe.parent.mkdir(parents=True)
        shutil.copyfile(self.scanner, exe)
        exe.chmod(0o755)
        (exe.parent / "binary.sha256").write_text(hashlib.sha256(exe.read_bytes()).hexdigest())
        (self.root / ".githooks/pre-push").chmod(0o755)
        self.commit("Add synthetic checkout")
        with patch.dict(os.environ, self.env, clear=True):
            guard.enable_hook(self.root)

    def git(self, *args, expected=0):
        result = subprocess.run(["git", "-C", str(self.root), *args], env=self.env,
                                capture_output=True, text=True, timeout=60)
        if expected is not None:
            self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def commit(self, message):
        self.git("add", "--all")
        self.git("update-index", "--chmod=+x", ".githooks/pre-push")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def credential(self):
        # Generated at runtime: no test credential literal needs an allowlist.
        return "session_token = '" + secrets.token_urlsafe(32) + "'\n"

    def assert_refused(self, result, reason, credential=None):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(reason, result.stderr)
        self.assertEqual(self.git("ls-remote", "origin").stdout, "")
        if credential:
            self.assertNotIn(credential.split("'")[1], result.stdout + result.stderr)

    def test_clean_push_and_deletion(self):
        self.git("tag", "-a", "v-test", "-m", "Synthetic release")
        self.git("push", "origin", "main", "v-test")
        remote = self.git("ls-remote", "origin").stdout
        self.assertIn("refs/heads/main", remote)
        self.assertIn("refs/tags/v-test", remote)
        # A deletion transfers no new content, even if the scanner was removed.
        guard.executable(self.root).unlink()
        self.git("push", "origin", ":refs/tags/v-test")
        self.assertNotIn("refs/tags/v-test", self.git("ls-remote", "origin").stdout)

    def test_removed_secret_blocks_push(self):
        credential = self.credential()
        path = self.root / "settings.txt"
        path.write_text(credential)
        self.commit("Add disposable credential")
        path.unlink()
        self.commit("Remove disposable credential")
        result = self.git("push", "origin", "main", expected=None)
        self.assert_refused(result, "Secret scan found credentials", credential)
        self.assertIn("icloud-session-credential", result.stderr)

    def test_tags_and_messages_are_scanned(self):
        credential = self.credential()
        self.git("tag", "-a", "bad-tag", "-m", credential)
        self.assert_refused(self.git("push", "origin", "bad-tag", expected=None),
                            "Secret scan found credentials", credential)
        self.git("tag", "-d", "bad-tag")
        self.git("commit", "--allow-empty", "-m", credential)
        self.assert_refused(self.git("push", "origin", "main", expected=None),
                            "Secret scan found credentials", credential)

    def test_all_pushed_refs_are_scanned(self):
        self.git("checkout", "-b", "other")
        (self.root / "settings.txt").write_text(self.credential())
        self.commit("Add disposable settings")
        self.git("checkout", "main")
        self.assert_refused(self.git("push", "origin", "main", "other", expected=None),
                            "Secret scan found credentials")

    def test_merge_resolution_is_scanned(self):
        self.git("checkout", "-b", "other")
        (self.root / "other.txt").write_text("Synthetic branch")
        self.commit("Add other file")
        self.git("checkout", "main")
        (self.root / "main.txt").write_text("Synthetic main")
        self.commit("Add main file")
        self.git("merge", "--no-ff", "--no-commit", "other")
        (self.root / "settings.txt").write_text(self.credential())
        self.commit("Merge synthetic branches")
        self.assert_refused(self.git("push", "origin", "main", expected=None),
                            "Secret scan found credentials")

    def test_private_history_blocks_push(self):
        for name in [".smoke/session.json", "PLAN.md", "CODEX_PROMPT.md",
                     ".windows-sandbox/cookies", "nested/.env"]:
            with self.subTest(path=name):
                parent = self.git("rev-parse", "HEAD").stdout.strip()
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Disposable private artifact, with no token pattern")
                self.git("add", "--force", "--", name)
                self.git("commit", "-m", "Add private artifact")
                path.unlink()
                self.commit("Remove private artifact")
                self.assert_refused(self.git("push", "origin", "main", expected=None),
                                    "Private artifacts must stay outside")
                self.git("reset", "--hard", parent)

    def test_pending_index_and_worktree(self):
        (self.root / ".smoke").mkdir()
        (self.root / ".smoke/session.json").write_text(self.credential())
        exe = guard.require_scanner(self.root)
        with patch.object(guard, "ROOT", self.root), patch.dict(os.environ, self.env, clear=True):
            # Ignored private artifacts must never be read by a filesystem scan.
            guard.pending(self.root, exe)
            path = self.root / "settings.txt"
            for staged in [False, True]:
                with self.subTest(staged=staged):
                    # Exercise both the default GitHub detector and our iCloud rule.
                    if staged:
                        credential = self.credential()
                    else:
                        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                        token = "".join(secrets.SystemRandom().sample(alphabet, 36))
                        credential = "token = '" + "ghp_" + token + "'"
                    path.write_text(credential)
                    if staged:
                        self.git("add", "settings.txt")
                        path.write_text("Credential removed only in the working copy")
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaisesRegex(guard.Blocked, "found credentials"):
                            guard.pending(self.root, exe)
                    if staged:
                        self.git("reset", "--", "settings.txt")
                    path.unlink()
            self.git("add", "--force", ".smoke/session.json")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(guard.Blocked, "Private artifacts"):
                    guard.pending(self.root, exe)

    def test_linked_working_paths_are_refused(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "settings.txt").write_text("Synthetic media outside the checkout")
        linked = self.root / "linked"
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
                           check=True, capture_output=True)
            self.addCleanup(linked.rmdir)
        else:
            linked.symlink_to(outside, target_is_directory=True)
        with patch.object(guard, "ROOT", self.root), patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(guard.Blocked, "Linked or"):
                guard.pending(self.root, self.scanner)

    def test_scanner_failures_block_push(self):
        exe = guard.executable(self.root)
        original = exe.read_bytes()
        for content, reason in [(b"changed", "incomplete or changed"), (None, "Gitleaks is missing")]:
            with self.subTest(content=content):
                if content is None:
                    exe.unlink()
                else:
                    exe.write_bytes(content)
                self.assert_refused(self.git("push", "origin", "main", expected=None), reason)
                exe.write_bytes(original)
                exe.chmod(0o755)
        config = self.root / ".gitleaks.toml"
        original_config = config.read_text()
        credential = self.credential()
        config.write_text("invalid = [\n# " + credential)
        self.assert_refused(self.git("push", "origin", "main", expected=None),
                            "Gitleaks failed", credential)
        config.write_text(original_config)
        self.git("config", "--unset", "iarchive.secretsPython")
        self.assert_refused(self.git("push", "origin", "main", expected=None), "Secret guard needs setup")

    def test_hook_setup_preserves_custom_hooks(self):
        for custom_path in [False, True]:
            with self.subTest(custom_path=custom_path), patch.dict(os.environ, self.env, clear=True):
                self.git("config", "--unset", "core.hooksPath", expected=None)
                if custom_path:
                    self.git("config", "core.hooksPath", ".other-hooks")
                else:
                    (self.root / ".git/hooks/pre-commit").write_text("Existing hook")
                with self.assertRaisesRegex(guard.Blocked, "existing hooksPath|Existing Git hooks"):
                    guard.enable_hook(self.root)
                self.assertEqual(self.git("config", "--get", "core.hooksPath", expected=None).stdout.strip(),
                                 ".other-hooks" if custom_path else "")

    def test_invalid_history_is_refused(self):
        for line in ["truncated input", "refs/heads/main not-an-id refs/heads/main " + "0" * 40]:
            with self.subTest(line=line):
                with self.assertRaisesRegex(guard.Blocked, "Malformed pre-push"):
                    guard.push_tips(line)
        # A tag pointing directly at a blob cannot silently become a zero-commit pass.
        blob = self.git("rev-parse", "HEAD:.gitignore").stdout.strip()
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(guard.Blocked, "Only commit"):
                guard.history(self.root, self.scanner, [blob])
            (self.root / ".git/shallow").write_text(self.git("rev-parse", "HEAD").stdout)
            with self.assertRaisesRegex(guard.Blocked, "Full Git history"):
                guard.history(self.root, self.scanner, [self.git("rev-parse", "HEAD").stdout.strip()])

    def test_install_verifies_download_checksum(self):
        target = guard.tool_directory(self.root)
        shutil.rmtree(target)
        data = io.BytesIO()
        if os.name == "nt":
            with zipfile.ZipFile(data, "w") as package:
                package.writestr("gitleaks.exe", b"synthetic executable")
        else:
            with tarfile.open(fileobj=data, mode="w:gz") as package:
                item = tarfile.TarInfo("gitleaks")
                item.size = len(b"synthetic executable")
                package.addfile(item, io.BytesIO(b"synthetic executable"))
        content = data.getvalue()
        machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(guard.platform.machine().lower(),
                                                               guard.platform.machine().lower())
        key = guard.platform.system(), machine
        for expected in ["0" * 64, hashlib.sha256(content).hexdigest()]:
            with self.subTest(checksum=expected), \
                    patch.dict(guard.DOWNLOADS, {key: ("fixture", expected)}), \
                    patch.object(guard.urllib.request, "urlopen", return_value=io.BytesIO(content)) as network, \
                    patch.object(guard, "require_scanner"):
                if expected == "0" * 64:
                    with self.assertRaisesRegex(guard.Blocked, "checksum mismatch"):
                        guard.install_scanner(self.root)
                    self.assertFalse(target.exists())
                else:
                    guard.install_scanner(self.root)
                    guard.install_scanner(self.root)
                    self.assertEqual(network.call_count, 1)
                    self.assertEqual(guard.executable(self.root).read_bytes(), b"synthetic executable")

    def test_extraction_never_follows_links(self):
        for windows in [False, True]:
            for linked in [False, True]:
                with self.subTest(windows=windows, linked=linked):
                    archive = self.root / "distribution"
                    destination = self.root / "extracted"
                    name = "gitleaks.exe" if windows else "gitleaks"
                    if windows:
                        with zipfile.ZipFile(archive, "w") as package:
                            item = zipfile.ZipInfo(name)
                            if linked:
                                item.create_system = 3
                                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                            package.writestr(item, "synthetic executable")
                            package.writestr("../../escape", "Must never be extracted")
                    else:
                        with tarfile.open(archive, "w") as package:
                            item = tarfile.TarInfo(name)
                            if linked:
                                item.type = tarfile.SYMTYPE
                                item.linkname = "../../escape"
                            else:
                                item.size = len(b"synthetic executable")
                            package.addfile(item, io.BytesIO(b"synthetic executable"))
                    if linked:
                        with self.assertRaisesRegex(guard.Blocked, "unique regular executable"):
                            guard.extract_binary(archive, destination, windows)
                    else:
                        guard.extract_binary(archive, destination, windows)
                        self.assertEqual(destination.read_bytes(), b"synthetic executable")
                        destination.unlink()
                    self.assertFalse((self.root.parent / "escape").exists())


if __name__ == "__main__":
    unittest.main()
