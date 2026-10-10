# Windows setup and testing

Use Windows 11 x64 with 64-bit Windows PowerShell 5.1 or PowerShell 7.
For isolation from your everyday Windows installation, use **Windows Sandbox or
an expendable Windows VM**. A dedicated directory alone is not an OS sandbox:
compilers, dependency build scripts, and tests still run with your account's
permissions.

## Choose the environment and directory

Use this layout inside your chosen Windows environment:

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

For a host checkout with Git already installed:

```powershell
git clone https://github.com/mpanelo/iarchive.git C:\iarchive-sandbox\repo
Set-Location C:\iarchive-sandbox\repo
.\scripts\windows\setup.ps1
.\scripts\windows\test.ps1
```

### Windows Sandbox: stronger host isolation

[Windows Sandbox](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/)
is available on supported Windows Pro, Enterprise, and Education editions, not
Home. Enabling this Windows feature is a separate administrator action and may
require a restart; the project scripts do not enable it. On Home, use a disposable
Windows VM instead.

Once the feature is enabled, create a minimal, read-only source share. From the
host checkout, export only committed source into a fresh directory (choose a new
name when repeating this):

```powershell
New-Item -ItemType Directory -Path C:\iarchive-sandbox\source-share
git archive --format=zip HEAD --output C:\iarchive-sandbox\source-share\repo.zip
```

Save this as `C:\iarchive-sandbox\iarchive.wsb`, then double-click it:

```xml
<Configuration>
  <VGpu>Disable</VGpu>
  <Networking>Enable</Networking>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <PrinterRedirection>Disable</PrinterRedirection>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>C:\iarchive-sandbox\source-share</HostFolder>
      <SandboxFolder>C:\iarchive-source</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
</Configuration>
```

If you used a directory under your user profile, substitute its absolute host
path in `HostFolder`. Only this small source share is mapped; do not map photo
archives, your user profile, or whole drives. The read-only mapping prevents
guest writes from persisting through that share. Networking is enabled to fetch
tools and dependencies; this is not network isolation. See Microsoft's
[configuration reference](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file).

Inside Windows Sandbox, open PowerShell and create the writable guest checkout:

```powershell
Expand-Archive -LiteralPath C:\iarchive-source\repo.zip -DestinationPath C:\iarchive-sandbox\repo
Set-Location C:\iarchive-sandbox\repo
```

Install the prerequisites **inside the guest**, then run `setup.ps1` and
`test.ps1` there. Host-installed tools are not automatically available in the
guest. Closing Windows Sandbox discards its installed software, checkout, and
results; inspect reports before closing. This configuration exercises synthetic
local files, not physical USB drives. For real drive tests, use the host workflow
below or a VM with a dedicated test device attached.

## Setup and its write locations

Install prerequisites in the environment you chose: rustup with its Cargo
launcher, Visual Studio Build Tools with **Desktop development with C++** and a
Windows SDK, and uv 0.12.3 or newer. See [Rust's MSVC prerequisites](https://rust-lang.github.io/rustup/installation/windows-msvc.html)
and [uv installation](https://docs.astral.sh/uv/getting-started/installation/).
Installing these prerequisites can change that environment's system; use the
guest when you want to avoid installing them on your host. Open a new PowerShell
after installation so the launchers are on PATH.

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
restriction on what arbitrary dependency code could do; use Windows Sandbox or
a VM for that boundary.

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
