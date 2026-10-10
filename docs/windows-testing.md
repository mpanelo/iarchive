# Windows setup and testing

Use Windows 11 x64 with 64-bit Windows PowerShell 5.1 or PowerShell 7. Clone the repository with Git, then open PowerShell in the checkout. The scripts also locate the checkout when invoked from another working directory.

## Setup

If Rust, the MSVC build tools, a Windows SDK, and uv are already installed:

```powershell
.\scripts\windows\setup.ps1
```

To install missing tools through WinGet first:

```powershell
.\scripts\windows\setup.ps1 -InstallPrerequisites
```

The optional installation uses the official WinGet packages `Microsoft.VisualStudio.2022.BuildTools`, `Rustlang.Rustup`, and `astral-sh.uv`. It requests the C++ workload with recommended components. Installer agreements and elevation prompts remain interactive. If an installer requests a restart, restart Windows and rerun setup. For manual installation, see [Rust's MSVC prerequisites](https://rust-lang.github.io/rustup/installation/windows-msvc.html) and [uv installation](https://docs.astral.sh/uv/getting-started/installation/).

Setup adds rustfmt and Clippy to the active Rust toolchain, installs cargo-deny 0.20.2 if absent, creates the locked worker environment with Python 3.12, and builds `target\release\iarchive.exe`. It requires an x64 MSVC Rust host. Open a new PowerShell window after installing prerequisites so newly installed tools are on your normal PATH.

If Windows blocks script execution, run the script with a process-local policy:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1
```

The same form works for `test.ps1` and its arguments. This sets the policy for that process without changing the saved user or machine policy. Organization policies can still restrict execution; follow those policies.

## Automated checks and local smoke test

```powershell
.\scripts\windows\test.ps1
```

The script runs formatting, Clippy, the full Rust tests with the image worker required, cargo-deny, the Python image tests, and the SQLite tests. It builds a release executable, then checks the complete CLI workflow with synthetic PNG, JPEG, and HEIC media.

The smoke tests check:

- Native command failures stop the script; unsafe drive arguments and junction paths are refused.
- Dry-run leaves no application state; repeated import remains idempotent.
- Exact duplicate detection, cached indexing, and a deep scan that recomputes hashes.
- Exact, similar, and dark review candidates; cached image analysis.
- Manifest records match independently computed SHA-256 hashes.
- Manifest exports refuse existing files and destinations inside media.
- Original synthetic file bytes and modification times remain unchanged during scans.
- Intentionally corrupt and partial files produce an incomplete audit with expected exit code 2 and never become review candidates.
- A temporarily renamed fixture directory simulates an unavailable source and invalidates completion. The script restores its original name in a `finally` block.

Expected nonzero application exits are checked explicitly; they do not fail the script when they match the test expectation. Any unexpected command failure or assertion stops the run and produces script exit code 1. A successful script exits 0. Missing optional `ffprobe` or `icloudpd` capabilities are acceptable; the image worker must be available.

## External drives

Run the full checks and repeat the smoke test on each selected drive:

```powershell
.\scripts\windows\test.ps1 -ExternalDrives 'D:', 'E:'
```

Each drive must be available and writable. Arguments accept drive roots only, such as `D:` or `D:\`; archive-directory arguments are rejected. For each selected drive, the script creates a fresh `iarchive-test-<random-id>\media` directory at its root. It generates a separate tiny fixture library there and repeats the same checks. Application state, previews, reports, and manifests stay in the checkout's `.smoke` directory, outside the fixture roots. No existing photo archive is scanned or modified.

After a full check has already passed, reuse the release executable to repeat only the script guards and smoke tests:

```powershell
.\scripts\windows\test.ps1 -SmokeOnly -ExternalDrives 'D:', 'E:'
```

The summary marks automated checks as **SKIPPED (SmokeOnly)**. This mode does not establish that the full test suite passed for the current checkout. Avoid modifying the code between the full checks and a smoke-only run.

The unavailable-source test renames only its own generated fixture directory; it does not unplug or unmount a drive. Real USB disconnect/reconnect behavior needs a separate manual test. Drive letters do not establish separate physical devices, and independently generated fixtures on two drives are not replicas. Local replication, physical-drive identity, and cloud coverage remain **NOT VERIFIED**.

## Results

Each smoke run retains `.smoke\windows-<random-id>\summary.json`, per-command JSON reports, source hash/mtime snapshots, an exported manifest, SQLite state, and thumbnail previews. Failures during smoke testing retain the same artifacts and write `passed: false` to the summary. A failure in the earlier quality/build checks stops before smoke artifacts are created; inspect the console output.

The initial clean-fixture snapshot is saved before adding intentionally invalid test files. The final snapshot includes those added fixtures. Each scanning phase compares its own before/after snapshot. Test files and junction fixtures are retained for inspection; the scripts do not delete or clean up any directory. External test directories are also retained at the paths printed by the script.

GitHub Actions executes setup and the full test script under PowerShell 7, then repeats the smoke tests under Windows PowerShell 5.1. The drive-root branch uses the runner's `C:` volume; CI does not establish real external-drive behavior. Synthetic JSON reports and manifests are uploaded as the `windows-script-reports` artifact.
