# Windows setup and testing

Use this guide to build and test iarchive with generated sample files on Windows
11 x64, including Windows Home. Use 64-bit Windows PowerShell 5.1 or PowerShell
7. You need Git to clone the repository and internet access
for the initial downloads.

The scripts keep project tools and test results in the checkout. Prerequisite
installers can make changes elsewhere in Windows. This directory layout is not
an operating-system sandbox: builds and tests run with your account's permissions.

## 1. Choose a folder and clone

Open a regular, non-administrator PowerShell window in a writable folder where
you want to keep the project. Choose a location outside photo archives and synced
folders. Check your current location, then clone:

```powershell
Get-Location
git clone https://github.com/mpanelo/iarchive.git
Set-Location .\iarchive
```

Git creates `iarchive` beneath your current directory. No particular drive or
parent folder is required. If you already cloned the repository, open that
checkout instead of cloning again.

Run the remaining examples from the `iarchive` checkout. The scripts calculate
their storage paths from the checkout location, even if invoked from elsewhere.

## 2. Run setup

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1
```

Setup automatically checks for these prerequisites and reuses compatible tools:

- Rustup and the Cargo launcher.
- uv 0.12.3 or newer.
- Visual Studio 2019 or newer C++ tools, with an x64 compiler and linker.
- A Windows SDK with headers and x64 Windows/UCRT libraries.

If a prerequisite is missing, setup installs it through WinGet. It upgrades an
older uv when needed. Existing Community, Professional, Enterprise, and Build
Tools installations with compatible C++ tools are reused. An existing Visual
Studio 2022 Build Tools installation without C++ is modified to add the workload.
Setup checks again after each installation, so it does not install a separate
SDK if the C++ installer already supplied one. When all prerequisites are
compatible, no prerequisite installer runs and WinGet is not required.

**Prerequisite installation can change Windows outside the checkout**, including
installed applications and saved PATH entries. Setup prints the installation
commands. WinGet agreement prompts are not automatically accepted, and installers
may ask for administrator permission. C++ and SDK installation requests no
automatic restart. If setup reports a required restart, complete it and run setup
again. Installation or discovery errors stop setup before the build.

After these checks, setup prepares project tools, installs locked dependencies,
and builds `target\release\iarchive.exe`. It keeps a separate x64 MSVC Rust
toolchain, rustfmt, Clippy, cargo-deny 0.20.2, and managed Python 3.12 in the
checkout. This project phase does not update your default Rust toolchain or
existing Python installations.

Wait for `Setup complete` before running tests. After a first-time installation,
close your terminal windows and open a fresh PowerShell window in the checkout
so subsequent commands can find the newly installed tools. See troubleshooting
below if tests still report that Cargo is missing.

### Where setup and tests write

```text
iarchive\
  .windows-sandbox\           project Rust, Python, caches, temporary files
  image-worker\.venv\        project Python environment
  target\                     compiled binaries
  .smoke\windows-<id>\        synthetic media, state, previews, reports
```

The scripts use process-only environment settings for these paths, disable uv's
Python registry registration, and restore their previous process settings when
they finish or fail. They reject output directories with symlink or reparse-point
ancestors. They do not edit PowerShell profiles or saved execution policies.
The name `.windows-sandbox` identifies project storage; it does not restrict what
compilers, dependency build scripts, or tests can do.

## 3. Run the full local tests

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test.ps1
```

This runs formatting, Clippy, Rust tests with the image worker required,
cargo-deny, Python image tests, and SQLite tests. It then builds a release
executable and exercises the CLI with generated PNG, JPEG, and HEIC files.
No existing photo archive is scanned.

The synthetic smoke tests check:

- Dry-run creates no application state, and repeated imports are idempotent.
- Exact duplicates, cached indexing, deep hashing, and image analysis behave as
  expected; similar and dark images are reported as review candidates.
- Exported manifest hashes match independently computed SHA-256 values, and
  export refuses existing files or destinations inside media.
- Scanning preserves fixture contents and modification times.
- Corrupt and partial files produce incomplete reports and never become review
  candidates; temporarily unavailable sources invalidate completion.
- Command failures stop execution, process settings are restored after failure,
  and unsafe drive paths, unmarked test folders, and junctions are refused.

The tests deliberately create invalid files and temporarily rename their own
fixture folder to simulate an unavailable source. That folder is restored in a
`finally` block. Expected application exit code 2 is checked explicitly; it does
not mean the script failed. The test script exits 0 on success and 1 on an
unexpected failure. Missing optional `ffprobe` or `icloudpd` is acceptable; the
image worker is required.

## 4. Test selected external drives

After local tests pass, replace the example letters with the drives you want to
test:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -Command "& '.\scripts\windows\test.ps1' -ExternalDrives 'D:', 'E:'"
```

This reruns the full checks and the local smoke test, then repeats the smoke test
on each selected volume. Use one drive letter if you only want to test one drive.
The example uses `-Command` because Windows PowerShell's `-File` option cannot
pass an array of drive arguments correctly; see Microsoft's
[PowerShell command-line documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1).

**Selecting a drive permits the test script to write generated fixtures outside
the checkout.** Omit `-ExternalDrives` to keep testing local. Supply available,
writable drive roots such as `D:` or `D:\`, never an archive folder.

For each selected drive, the script creates:

```text
D:\iarchive-sandbox\run-<random-id>\media\
```

The parent `iarchive-sandbox` folder has an ownership marker. An existing folder
without the expected marker is refused. Each run gets a fresh directory;
existing archives are not scanned or modified. State, previews, reports, and
manifests remain in the checkout's `.smoke` directory, outside the generated
media roots. Test files are retained for inspection.

If the full checks already passed for the current code, you can repeat only the
script guards and smoke tests using the existing release executable:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -Command "& '.\scripts\windows\test.ps1' -SmokeOnly -ExternalDrives 'D:', 'E:'"
```

This marks automated checks as **SKIPPED (SmokeOnly)**. Run the full tests again
if you change the code.

The unavailable-source test renames a generated fixture folder; it does not
unplug or unmount a drive. USB disconnect/reconnect behavior needs a separate
manual test. Drive letters do not prove separate physical devices, and these
independently generated fixtures are not replicas. Physical-drive identity,
archive replication, and cloud coverage are outside the scope of these tests.

## 5. Read the results

A successful run prints `PASS` and the path to its `summary.json`:

```text
.smoke\windows-<random-id>\summary.json
```

The summary describes the test run:

- `passed: true` means all checks selected for that run succeeded.
- `automated_checks` is `PASSED` for a full run or `SKIPPED (SmokeOnly)` when
  only script guards and smoke tests ran.
- `smoke_tests` contains the results for the generated local and selected-drive
  fixtures.

It also records the checkout, project tools, and PowerShell version. A passing
summary does not establish that your photo archive is backed up.

That run directory also retains per-command JSON reports, source hash and
modification-time snapshots, an exported manifest, SQLite state, and thumbnail
previews. Individual application reports retain their backup verification
status separately from the test-run result.

A smoke-test failure writes `passed: false` and retains its artifacts. A failure
in the earlier quality checks or build stops before smoke artifacts are created;
read the console error in that case.

Clean-fixture snapshots are captured before invalid test files are added. Later
snapshots include those deliberately added files; each scan compares its own
before/after snapshots. The scripts leave generated files, guard fixtures, and
external test directories in place. They do not clean up directories.

## Troubleshooting

### PowerShell says scripts are disabled

Use the `powershell.exe -NoProfile -ExecutionPolicy RemoteSigned` commands shown
above. The execution policy applies only to the new PowerShell process and does
not change saved user or machine settings. Organization policies can still
restrict execution; follow those policies. Administrator PowerShell is not
required to run the scripts.

### Setup succeeded, but tests say Cargo is missing

Setup refreshes PATH within its own process. An already-open terminal can still
have the PATH from before Rust was installed, and the current test script does
not refresh it. You do not need to reinstall Rust or repeat successful setup.

For Rust installed in its standard user location, add the launcher directory to
your current PowerShell process and retry:

```powershell
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test.ps1
```

This changes PATH only in the current process and child processes. If you chose a
custom Rust installation location, use its launcher directory instead. Closing
all terminal windows and opening a fresh PowerShell window also lets Windows
supply the updated saved PATH.

### WinGet is unavailable or prerequisite installation failed

WinGet is provided by Microsoft's
[App Installer](https://learn.microsoft.com/en-us/windows/package-manager/winget/).
Setup only needs it when a prerequisite is missing or needs an upgrade. Resolve
the reported installation error or restart request, open a fresh PowerShell
window, and rerun setup. Compatible prerequisites will be reused.

If you prefer to install prerequisites manually, run only the commands you need:

```powershell
winget install --exact --id Microsoft.VisualStudio.2022.BuildTools --source winget --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --norestart"
winget install --exact --id Rustlang.Rustup --source winget
winget install --exact --id astral-sh.uv --source winget
```

If C++ tools are present but the Windows SDK is missing:

```powershell
winget install --exact --id Microsoft.WindowsSDK.10.0.22621 --source winget --custom /norestart
```

To upgrade uv older than 0.12.3:

```powershell
winget upgrade --exact --id astral-sh.uv --source winget
```

These installers can change Windows outside the checkout and request
administrator permission. Open a fresh PowerShell window afterward, then rerun
setup. See [Rust's MSVC prerequisites](https://rust-lang.github.io/rustup/installation/windows-msvc.html)
and [uv installation](https://docs.astral.sh/uv/getting-started/installation/).

## What CI covers

GitHub Actions checks prerequisite discovery and simulated installer calls under
PowerShell 7 and Windows PowerShell 5.1. It also checks discovery against the
runner's actual installed tools. CI uses the check-only setup option
`-SkipPrerequisites` to prevent prerequisite installers from running; you do not
need this option for normal setup.

CI runs setup and the full test script under PowerShell 7, then repeats smoke
tests under Windows PowerShell 5.1. It exercises drive-root handling on the
runner's `C:` volume and uploads synthetic JSON reports and manifests as
`windows-script-reports`. A passing CI run does not verify installation on a
clean Windows machine or behavior on real external drives.
