"""Install a pinned local scanner and check Git history before publication."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "8.30.1"
# Upstream v8.30.1 release checksums, not a downloaded trust-on-first-use manifest.
DOWNLOADS = {
    ("Darwin", "arm64"): ("darwin_arm64.tar.gz", "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"),
    ("Darwin", "x86_64"): ("darwin_x64.tar.gz", "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"),
    ("Linux", "arm64"): ("linux_arm64.tar.gz", "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"),
    ("Linux", "x86_64"): ("linux_x64.tar.gz", "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
    ("Windows", "arm64"): ("windows_arm64.zip", "b95f5e4f5c425cedca7ee203d9afd29597e692c4924a12ed42f970537c72cc0f"),
    ("Windows", "x86_64"): ("windows_x64.zip", "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e"),
}
PRIVATE_ROOTS = {
    ".smoke", ".windows-sandbox", ".tools", ".cargo-home", ".uv-cache",
    ".aws", ".codex", "target",
}
PRIVATE_NAMES = {"plan.md", "codex_prompt.md", ".env", ".ds_store"}
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class Blocked(Exception):
    pass


def git(root, *args, allowed=(0,)):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode not in allowed:
        # Git errors can contain credential-bearing remote URLs or file contents.
        raise Blocked("Git inspection failed; secret checking is incomplete")
    return result.stdout


def regular(path):
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400


def safe_directory(path):
    for parent in (*reversed(path.parents), path):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise Blocked("Linked or non-directory secret tool destination")
    return path


def tool_directory(root):
    return root / (".windows-sandbox" if os.name == "nt" else ".tools") / "gitleaks"


def executable(root):
    return tool_directory(root) / ("gitleaks.exe" if os.name == "nt" else "gitleaks")


def require_scanner(root):
    exe = executable(root)
    safe_directory(exe.parent)
    marker = exe.parent / "binary.sha256"
    try:
        if (not regular(exe) or not regular(marker)
                or hashlib.sha256(exe.read_bytes()).hexdigest() != marker.read_text().strip()):
            raise Blocked("Local Gitleaks is incomplete or changed; inspect its directory")
    except FileNotFoundError:
        raise Blocked("Gitleaks is missing; run python scripts/check_secrets.py setup") from None
    result = subprocess.run([str(exe), "version"], capture_output=True, timeout=15)
    if result.returncode or result.stdout.decode().strip() != VERSION:
        raise Blocked("Local Gitleaks version does not match the project pin")
    return exe


def extract_binary(archive, destination, windows):
    """Copy only the fixed executable member; never extract archive paths."""
    name = "gitleaks.exe" if windows else "gitleaks"
    if windows:
        with zipfile.ZipFile(archive) as package:
            members = [item for item in package.infolist() if item.filename == name]
            if len(members) != 1 or stat.S_ISLNK(members[0].external_attr >> 16):
                raise Blocked("Scanner archive has no unique regular executable")
            if members[0].file_size > 64 * 1024 * 1024:
                raise Blocked("Scanner executable exceeds extraction limit")
            with package.open(members[0]) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive) as package:
            members = [item for item in package.getmembers() if item.name == name]
            if len(members) != 1 or not members[0].isfile():
                raise Blocked("Scanner archive has no unique regular executable")
            if members[0].size > 64 * 1024 * 1024:
                raise Blocked("Scanner executable exceeds extraction limit")
            with package.extractfile(members[0]) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)
    destination.chmod(0o755)


def install_scanner(root):
    target = tool_directory(root)
    safe_directory(target.parent)
    if target.exists() or target.is_symlink():
        require_scanner(root)
        return
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    try:
        suffix, expected = DOWNLOADS[platform.system(), machine]
    except KeyError:
        raise Blocked("No pinned Gitleaks binary for this platform") from None
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gitleaks-", dir=target.parent) as temporary:
        stage = Path(temporary)
        archive = stage / "download"
        url = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/gitleaks_{VERSION}_{suffix}"
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("xb") as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 32 * 1024 * 1024:
                    raise Blocked("Scanner distribution exceeds download limit")
                output.write(chunk)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise Blocked("Scanner distribution checksum mismatch")
        unpacked = stage / "ready"
        unpacked.mkdir()
        binary = unpacked / executable(root).name
        extract_binary(archive, binary, os.name == "nt")
        (unpacked / "binary.sha256").write_text(hashlib.sha256(binary.read_bytes()).hexdigest())
        unpacked.rename(target)
    require_scanner(root)


def enable_hook(root):
    configured = git(root, "config", "--get", "core.hooksPath", allowed=(0, 1)).decode().strip()
    if configured and configured != ".githooks":
        raise Blocked("An existing hooksPath is configured; integrate the guard without replacing it")
    hooks = Path(os.fsdecode(git(root, "rev-parse", "--git-path", "hooks")).strip())
    if not hooks.is_absolute():
        hooks = root / hooks
    if not configured and hooks.exists() and any(
        item.is_file() and not item.name.endswith(".sample") for item in hooks.iterdir()
    ):
        raise Blocked("Existing Git hooks found; integrate the guard without replacing them")
    hook = root / ".githooks/pre-push"
    if not regular(hook):
        raise Blocked("Project pre-push hook is missing or linked")
    hook.chmod(hook.stat().st_mode | 0o111)
    # Store the selected interpreter, so Windows Store aliases/PATH cannot silently skip the hook.
    git(root, "config", "--local", "iarchive.secretsPython", Path(sys.executable).as_posix())
    git(root, "config", "--local", "core.hooksPath", ".githooks")


def private_path(path):
    parts = path.replace("\\", "/").casefold().split("/")
    return (parts[0] in PRIVATE_ROOTS or any(part in PRIVATE_NAMES for part in parts)
            or ".venv" in parts or "__pycache__" in parts)


def check_paths(paths):
    refused = sorted({path for path in paths if path and private_path(path)})
    if refused:
        for path in refused:
            print("Private file blocked: " + json.dumps(path, ensure_ascii=True), file=sys.stderr)
        raise Blocked("Private artifacts must stay outside published Git history")


def nul_paths(data):
    return [os.fsdecode(path) for path in data.split(b"\0") if path]


def scan(root, exe, mode, *args, content=None):
    with tempfile.TemporaryDirectory(prefix="iarchive-secrets-") as temporary:
        report = Path(temporary) / "redacted.json"
        command = [str(exe), mode, "--config", str(ROOT / ".gitleaks.toml"),
                   "--redact=100", "--ignore-gitleaks-allow", "--gitleaks-ignore-path", temporary,
                   "--no-banner", "--no-color", "--exit-code=10", "--report-format=json",
                   "--report-path", str(report), *args]
        # Do not let environment configuration add exceptions or diagnostics.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GITLEAKS_")}
        result = subprocess.run(command, cwd=root, input=content, capture_output=True, env=env, timeout=180)
        if result.returncode == 10:
            findings = json.loads(report.read_text())
            for finding in findings:
                # Never echo the matching line, secret, commit message, author or upstream stderr.
                print("Secret detected: " + json.dumps({
                    "file": finding.get("File", ""), "line": finding.get("StartLine"),
                    "rule": finding.get("RuleID"), "commit": finding.get("Commit", "")[:12],
                }, ensure_ascii=True), file=sys.stderr)
            raise Blocked("Secret scan found credentials; remove them before publication")
        if result.returncode != 0 or not report.is_file():
            raise Blocked("Gitleaks failed; secret checking is incomplete")


def history(root, exe, tips):
    if git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise Blocked("Full Git history is required; fetch with --unshallow before scanning")
    commits = set()
    for tip in tips:
        if not OID.fullmatch(tip):
            raise Blocked("Invalid pushed object ID")
        current = tip
        for _ in range(16):
            kind = git(root, "cat-file", "-t", current).strip()
            if kind == b"commit":
                commits.add(current)
                break
            if kind != b"tag":
                raise Blocked("Only commit and annotated commit-tag pushes are supported")
            tag = git(root, "cat-file", "-p", current)
            scan(root, exe, "stdin", content=tag)
            current = tag.splitlines()[0].removeprefix(b"object ").decode("ascii")
            if not OID.fullmatch(current):
                raise Blocked("Invalid tag target")
        else:
            raise Blocked("Tag nesting exceeds secret inspection limit")
    if not commits:
        return
    revisions = sorted(commits)
    options = ["--full-history", "-m", "--no-renames", "--no-ext-diff", "--no-textconv"]
    paths = git(root, "log", "--format=", "--name-only", "-z", *options, *revisions, "--")
    check_paths(nul_paths(paths))
    scan(root, exe, "git", "--log-opts=" + " ".join([*options, *revisions]), str(root))
    messages = git(root, "log", "--format=%B", *revisions, "--")
    scan(root, exe, "stdin", content=messages)


def pending(root, exe):
    paths = nul_paths(git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"))
    check_paths(paths)
    # Inspect the index separately: a staged secret may already be gone from the working file.
    scan(root, exe, "git", "--staged", str(root))
    with tempfile.TemporaryDirectory(prefix="iarchive-pending-") as temporary:
        snapshot = Path(temporary)
        for name in set(paths):
            source = root / name
            if not source.exists() and not source.is_symlink():
                continue  # An unstaged deletion; the index was still inspected above.
            safe_directory(source.parent)
            if not regular(source):
                raise Blocked("Linked or non-regular working file cannot be scanned")
            destination = snapshot / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        # Copy only Git-visible files, never walk ignored archives, cookies or tool caches.
        scan(root, exe, "dir", str(snapshot))


def push_tips(lines):
    tips = set()
    for line in lines.splitlines():
        parts = line.split()
        if len(parts) != 4 or not OID.fullmatch(parts[1]) or not OID.fullmatch(parts[3]):
            raise Blocked("Malformed pre-push input; secret checking is incomplete")
        if set(parts[1]) != {"0"}:  # Deletions send no new content.
            tips.add(parts[1])
    return sorted(tips)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Install Gitleaks and enable the local push guard")
    setup.add_argument("--no-hook", action="store_true", help="Install only (for CI)")
    check = commands.add_parser("check", help="Scan all reachable history and pending files")
    check.add_argument("--history-only", action="store_true", help="Omit pending files (for CI)")
    push = commands.add_parser("pre-push", help=argparse.SUPPRESS)
    push.add_argument("remote", nargs="?")
    push.add_argument("url", nargs="?")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            install_scanner(ROOT)
            if not args.no_hook:
                enable_hook(ROOT)
            print(f"Gitleaks {VERSION} ready" + ("; push guard enabled" if not args.no_hook else ""))
            return 0
        tips = push_tips(sys.stdin.read()) if args.command == "pre-push" else None
        if tips == []:
            return 0
        exe = require_scanner(ROOT)
        if tips is None:
            tips = git(ROOT, "for-each-ref", "--format=%(objectname)").decode().splitlines()
            head = git(ROOT, "rev-parse", "--verify", "HEAD", allowed=(0, 128)).decode().strip()
            if head:
                tips.append(head)
        history(ROOT, exe, sorted(set(tips)))
        if args.command == "check" and not args.history_only:
            pending(ROOT, exe)
        print("Secret check passed")
        return 0
    except Blocked as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile):
        print("Secret checking failed; no push is permitted. Inspect the local setup.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
