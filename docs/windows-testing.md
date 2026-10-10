# Windows setup and testing

Use Windows 11 x64, including Windows Home, with 64-bit Windows PowerShell 5.1
or PowerShell 7. The scripts use a dedicated checkout and keep project tools,
caches, and temporary files under it. This is directory scoping, not OS isolation:
compilers, dependency build scripts, and tests still run with your account's
permissions.

## Choose the directory

Use this dedicated layout:

```text
C:\iarchive-sandbox\
  repo\                       dedicated source checkout
    .windows-sandbox\         project Rust, Python, caches, temporary files
    image-worker\.venv\      project Python environment
    target\                   compiled binaries
    .smoke\windows-<id>\      synthetic media, state, previews, reports
```

Open a **regular, non-administrator PowerShell** in `C:\iarchive-sandbox\repo`.
If creating that parent directory requires elevation on your system, use
`$env:USERPROFILE\iarchive-sandbox\repo` instead. The scripts derive all local
paths from their own checkout location; they also work when called from another
working directory. Keep the checkout outside photo archives and synced folders.

With Git already installed:

```powershell
git clone https://github.com/mpanelo/iarchive.git C:\iarchive-sandbox\repo
Set-Location C:\iarchive-sandbox\repo
.\scripts\windows\setup.ps1
.\scripts\windows\test.ps1
```

## Setup and its write locations

Install prerequisites if they are missing: rustup with its Cargo
launcher, Visual Studio Build Tools with **Desktop development with C++** and a
Windows SDK, and uv 0.12.3 or newer. See [Rust's MSVC prerequisites](https://rust-lang.github.io/rustup/installation/windows-msvc.html)
and [uv installation](https://docs.astral.sh/uv/getting-started/installation/).
Installing prerequisites can change Windows outside the checkout. Review their
installation options separately; the setup script does not install them. Open a
new PowerShell after installation so the launchers are on PATH.

```powershell
.\scripts\windows\setup.ps1
```

Setup uses existing launchers to download a separate x64 MSVC Rust toolchain,
rustfmt, Clippy, cargo-deny 0.20.2, and managed Python 3.12 into the checkout. It
syncs the locked worker dependencies and builds `target\release\iarchive.exe`.
It does not run WinGet, installers, rustup self-update, or change your default
Rust toolchain. The old `-InstallPrerequisites` option has been removed.

Both scripts set Cargo/Rustup/uv cache paths and temporary directories only for
their process, disable uv's Python registry registration, and restore their
previous process environment in `finally`. They reject local output directories
whose ancestors are symlinks or reparse points. Existing global toolchains,
Python installations, saved PATH, PowerShell profiles, and saved execution
policies are not changed by the scripts. These are scoped tool settings, not a
restriction on what arbitrary dependency code could do. The directory name
`.windows-sandbox` identifies project storage, not a Windows security boundary.

If Windows blocks script execution, run it with a process-local policy:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1
```

The same form works for `test.ps1` and its arguments. This does not change the
saved user or machine policy. Organization policies can still restrict execution;
follow those policies.

## Automated checks and local smoke test

```powershell
.\scripts\windows\test.ps1
```

The script runs formatting, Clippy, the full Rust tests with the image worker required, cargo-deny, the Python image tests, and the SQLite tests. It builds a release executable, then checks the complete CLI workflow with synthetic PNG, JPEG, and HEIC media.

The smoke tests check:

- Native command failures stop the script; process settings are restored after failure, and unsafe drive arguments, unmarked test directories, and junction paths are refused.
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

**This option explicitly permits writes outside the checkout, only to the selected test volumes.** Omit it for local-only testing. Each drive must be available and writable. Arguments accept drive roots only, such as `D:` or `D:\`; archive-directory arguments are rejected. For each selected drive, the script creates a fresh `iarchive-sandbox\run-<random-id>\media` directory on that volume. The dedicated `iarchive-sandbox` parent is marked as owned by these tests; an existing parent without the expected marker is refused. It generates a separate tiny fixture library there and repeats the same checks. Application state, previews, reports, and manifests stay in the checkout's `.smoke` directory, outside the fixture roots. No existing photo archive is scanned or modified.

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
