# Windows setup and testing

Use Windows 11 x64, including Windows Home, with 64-bit Windows PowerShell 5.1
or PowerShell 7. The scripts use a dedicated checkout and keep project tools,
caches, and temporary files under it. This is directory scoping, not OS isolation:
compilers, dependency build scripts, and tests still run with your account's
permissions.

## Choose the directory

Cloning creates an `iarchive` folder beneath your current directory, with this layout:

```text
.\iarchive\                   source checkout
  .windows-sandbox\           project Rust, Python, caches, temporary files
  image-worker\.venv\        project Python environment
  target\                     compiled binaries
  .smoke\windows-<id>\        synthetic media, state, previews, reports
```

Open a **regular, non-administrator PowerShell** in a writable directory where
you want the checkout. Keep it outside photo archives and synced folders. The
scripts derive all local paths from their own checkout location; they also work
when called from another working directory.

With Git already installed:

```powershell
git clone https://github.com/mpanelo/iarchive.git
Set-Location .\iarchive
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test.ps1
```

## Setup and its write locations

Run setup with no installation flag:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1
```

Setup detects and reuses compatible prerequisites automatically:

- Rustup and the Cargo launcher.
- uv 0.12.3 or newer. An older uv installation is upgraded if necessary.
- Visual Studio 2019 or newer C++ tools, including existing Community,
  Professional, Enterprise, and Build Tools installations. Detection checks for
  the x64 compiler and linker, rather than requiring them to be on PATH.
- A Windows SDK with its headers and x64 Windows/UCRT libraries.

If everything is present, no prerequisite installer runs and WinGet is not
required. Otherwise setup uses the WinGet community source to install only the
missing prerequisites. The C++ workload may also supply the SDK, so setup checks
again after each installation. Existing Visual Studio 2022 Build Tools without
C++ are modified to add the workload. Other compatible installations are reused.
The old `-InstallPrerequisites` option is unnecessary; detection and installation
are now the default.

**Prerequisite installation can change Windows outside the checkout**, including
installed applications and saved PATH entries. Setup prints each installation
command; WinGet agreement prompts are not automatically accepted, and installers
can request administrator permission. MSVC/SDK installation requests no automatic
restart. If installation fails, requires a restart, or tools remain unavailable,
setup stops with instructions instead of continuing to the build. Complete any
restart request, then rerun setup. Discovery errors also stop setup rather than
being treated as evidence that another installation is needed.

WinGet is supplied by Microsoft's [App Installer](https://learn.microsoft.com/en-us/windows/package-manager/winget/).
If it is unavailable and prerequisites are missing, setup reports that before
attempting installation. Setup refreshes PATH for its own process after installs;
open a new PowerShell window afterward for other commands to see installed tools.

For a preconfigured environment where installers must not run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1 -SkipPrerequisites
```

This still checks all prerequisites and fails if any are missing or incompatible.
It is optional: normal setup already skips compatible installations automatically.

### Manual prerequisite commands

For manual installation, run only the commands for tools you need:

```powershell
winget install --exact --id Microsoft.VisualStudio.2022.BuildTools --source winget --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --norestart"
winget install --exact --id Rustlang.Rustup --source winget
winget install --exact --id astral-sh.uv --source winget
```

If C++ tools are already installed but the SDK is missing, install it separately:

```powershell
winget install --exact --id Microsoft.WindowsSDK.10.0.22621 --source winget --custom /norestart
```

For an existing uv version older than 0.12.3:

```powershell
winget upgrade --exact --id astral-sh.uv --source winget
```

See [Rust's MSVC prerequisites](https://rust-lang.github.io/rustup/installation/windows-msvc.html),
[uv installation](https://docs.astral.sh/uv/getting-started/installation/), and
[WinGet installation options](https://learn.microsoft.com/en-us/windows/package-manager/winget/install).
Open a new PowerShell after manual installation, then rerun setup.

### Project setup

After prerequisite checks pass, setup downloads a separate x64 MSVC Rust
toolchain, rustfmt, Clippy, cargo-deny 0.20.2, and managed Python 3.12 into the
checkout. It syncs locked worker dependencies and builds
`target\release\iarchive.exe`. This project phase does not run rustup self-update
or change your default Rust toolchain.

Both scripts set Cargo/Rustup/uv cache paths and temporary directories only for
their process, disable uv's Python registry registration, and restore their
previous process environment in `finally`. They reject local output directories
whose ancestors are symlinks or reparse points. The project phase does not change
existing global toolchains or Python installations. The scripts do not edit
PowerShell profiles or saved execution policies; prerequisite installers may
update installed tools and saved PATH as described above. These are scoped tool
settings, not a restriction on what arbitrary dependency code could do. The directory name
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

GitHub Actions runs synthetic prerequisite detection/installer tests and checks
real runner prerequisite discovery under PowerShell 7 and Windows PowerShell 5.1.
These tests simulate installer calls; they do not install applications. CI executes
setup with `-SkipPrerequisites` and the full test script under PowerShell 7, then repeats the smoke tests under Windows PowerShell 5.1. The drive-root branch uses the runner's `C:` volume; CI does not establish real external-drive behavior. Synthetic JSON reports and manifests are uploaded as the `windows-script-reports` artifact.
