"""Guided synthetic walkthrough shared by the Windows and macOS entry points."""

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def safe_directory(path):
    """Inspect before resolving, including existing Windows reparse ancestors."""
    path = Path(os.path.abspath(path))
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise RuntimeError(f"Unsafe demo directory: {ancestor}")
    return path


def command(arguments, report=None):
    result = subprocess.run(arguments, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8")
    if report is not None:
        report.write_text(result.stdout, encoding="utf-8")
        report.with_suffix(".stderr.log").write_text(result.stderr, encoding="utf-8")
    if report is None or result.returncode != 0:
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode != 0:
        raise RuntimeError(f"Stopped: {arguments[0]} exited {result.returncode}.")
    return result.stdout


def review(arguments):
    print("Open the printed URL. Save your notes, then press Ctrl+C here.\n"
          "The demo will stop the gallery and continue with verification.", flush=True)
    # Share the console so Ctrl+C reaches both the gallery and this runner.
    # Wait for its shutdown before another command acquires the state lock.
    with subprocess.Popen(arguments, cwd=ROOT) as server:
        try:
            code = server.wait()
        except KeyboardInterrupt:
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                code = server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
                raise RuntimeError("Gallery did not stop; verification was not started.")
            finally:
                signal.signal(signal.SIGINT, previous)
        if code != 0:
            raise RuntimeError(f"Gallery exited {code}; verification was not started.")


def run_demo(executable, *, interactive=True):
    if not executable.is_file():
        raise RuntimeError("Build iarchive first; see docs/workflows.md.")
    base = safe_directory(ROOT / ".smoke")
    base.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="workflow-", dir=base))
    print(f"Demo files and reports: {run}\n"
          "Synthetic media only. Both copies are on one volume; physical backup\n"
          "independence and cloud coverage remain NOT VERIFIED.", flush=True)
    primary = run / "primary" / "media"
    secondary = run / "secondary" / "media"
    primary_artifacts = run / "primary" / "artifacts"
    secondary_artifacts = run / "secondary" / "artifacts"
    reports = run / "reports"
    for path in (secondary, primary_artifacts, secondary_artifacts, reports):
        path.mkdir(parents=True)
    command([sys.executable, ROOT / "fixtures" / "generate.py",
             "--output", primary, "--clean", "--with-heic"])
    common = [executable, "--state-dir", run / "state", "--json"]
    cycle = ["--cycle", "demo"]
    replica = [*cycle, "--secondary", secondary,
               "--primary-artifacts", primary_artifacts,
               "--secondary-artifacts", secondary_artifacts,
               "--allow-unverified-devices"]

    def stage(name, *arguments):
        print(f"{name}...", flush=True)
        output = command([*common, *arguments], reports / f"{name}.json")
        return json.loads(output)

    stage("doctor", "doctor", "--primary", primary, "--secondary", secondary)
    stage("init", "init", "--primary", primary, "--secondary", secondary)
    stage("import", "import", "--source", primary, *cycle)
    stage("index", "index", *cycle)
    stage("audit", "audit", *cycle, "--worker-project", ROOT / "image-worker")
    stage("report", "report", *cycle)
    stage("export", "export-manifest", *cycle,
          "--output", run / "inventory.jsonl")
    preview = stage("preview", "--dry-run", "replicate", *replica)
    print(f"Preview: copy {preview['planned_files']} files "
          f"({preview['required_copy_bytes']} bytes).\n"
          f"From: {primary}\nTo:   {secondary}", flush=True)
    if interactive and input("Copy these synthetic files? [y/N] ").strip().lower() != "y":
        print(f"Stopped before copying. Demo retained: {run}")
        return run
    stage("replicate", "replicate", *replica)
    if interactive:
        review([*common, "review", *cycle])
    else:
        print("Automated demo: browser review SKIPPED.", flush=True)
    stage("verify", "verify", "--deep", *replica)
    stage("status", "status", *cycle)
    print(f"Demo complete. Local bytes verified. Reports: {reports}", flush=True)
    return run


if __name__ == "__main__":
    executable = ROOT / "target" / "release" / (
        "iarchive.exe" if os.name == "nt" else "iarchive")
    try:
        run_demo(executable)
    except (RuntimeError, EOFError) as error:
        print(f"Demo stopped: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nDemo cancelled. Files and reports were retained.", file=sys.stderr)
        sys.exit(130)
