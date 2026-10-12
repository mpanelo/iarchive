# Windows setup and tests

Use Windows 11 x64, including Home, with PowerShell 5.1 or 7. Run from a regular
terminal. You need Git and internet access for initial setup.

The scripts use generated files, never your photo archive.

## 1. Clone

Open PowerShell in the folder where you want the checkout:

```powershell
git clone https://github.com/mpanelo/iarchive.git
cd iarchive
```

Already cloned? Open that checkout instead. Keep it outside archives and synced
folders.

## 2. Set up

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1
```

Setup detects Rustup/Cargo, uv, MSVC C++ tools and a Windows SDK. Compatible tools
are reused; missing tools are installed through WinGet. It then installs locked
project dependencies and builds `target\release\iarchive.exe`.

**Prerequisite installers can change Windows outside the checkout** and may ask
for administrator access. Project tools stay in `.windows-sandbox`; this is a
storage folder, not OS isolation. Profiles and saved execution policies are not
changed. The policy in the command applies only to that PowerShell process.

Wait for `Setup complete`. If tools were installed, open a fresh terminal in the
checkout before testing. Complete any requested restart and rerun setup.

## 3. Test locally

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test.ps1
```

This runs Rust/Python checks, builds the release executable, then tests the CLI
with synthetic PNG, JPEG and HEIC files. It checks hashing, cached scans,
non-overwriting replication, recovery bundles and expected failures.

Run commands separately. Stop and read the error if one fails.
Missing optional ffprobe or icloudpd is fine; the image worker is required.

## 4. Test external drives

Replace `D:` and `E:` with your drive letters:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -Command "& '.\scripts\windows\test.ps1' -ExternalDrives 'D:', 'E:'"
```

This writes fresh test files under `iarchive-sandbox\run-<id>` on each selected
drive. Existing archives are untouched. An existing sandbox without the ownership
marker is refused. Files are retained after the test.

The copies in this test stay on each source volume. For copying between two
physical drives, follow [two-drive tests](windows-two-drive-test.md).

If full checks already passed for this code, use the existing build for smoke
checks only:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -Command "& '.\scripts\windows\test.ps1' -SmokeOnly -ExternalDrives 'D:', 'E:'"
```

The summary then marks automated checks as skipped. Rerun full checks after code
changes. `-Command` is used because PowerShell 5.1's `-File` cannot pass this
array of drive arguments.

## Results and storage

A successful run prints `PASS` and its `summary.json` path. Require
`passed: true` and `automated_checks: PASSED` for a full run.

```text
.windows-sandbox/     project tools, caches and temporary files
image-worker/.venv/   image worker
target/               compiled binaries
.smoke/windows-<id>/  state, previews, reports and snapshots
```

Drive runs also retain generated media, bundles and partials under the marked
sandbox. Tests deliberately corrupt their own fixtures, so do not reuse them as
a clean archive. Nothing is cleaned up automatically.

A test failure retains reports when available. A build failure may occur before
reports are created; use the console error. Passing tests do not prove that your
real archive or iCloud library is fully backed up.

## Troubleshooting

### Scripts are disabled

Use the `powershell.exe -ExecutionPolicy RemoteSigned` commands above. No saved
policy change or administrator terminal is needed. Organization policies may
still restrict scripts.

### Setup passed, but Cargo is missing

Open a fresh terminal in the checkout. An old terminal may still have the PATH
from before installation. For the standard Rust installation, you can also use:

```powershell
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test.ps1
```

For a custom installation, use its Cargo launcher directory instead.

### A prerequisite cannot be installed

Fix the reported installer error, open a fresh terminal, and rerun setup.
Installed compatible tools will be reused. WinGet comes from
[App Installer](https://learn.microsoft.com/en-us/windows/package-manager/winget/).

For manual installation, run only what you need:

```powershell
winget install --exact --id Microsoft.VisualStudio.2022.BuildTools --source winget --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --norestart"
winget install --exact --id Rustlang.Rustup --source winget
winget install --exact --id astral-sh.uv --source winget
```

If C++ tools are installed but the SDK is missing:

```powershell
winget install --exact --id Microsoft.WindowsSDK.10.0.22621 --source winget --custom /norestart
```

For uv older than 0.12.3:

```powershell
winget upgrade --exact --id astral-sh.uv --source winget
```

These installers can change Windows outside the checkout.
[Rust prerequisites](https://rust-lang.github.io/rustup/installation/windows-msvc.html) · [uv installation](https://docs.astral.sh/uv/getting-started/installation/)

## CI

CI checks prerequisite discovery and simulated installation in both PowerShell
versions. Its setup uses `-SkipPrerequisites` to prevent real installers.
Normal setup does not need that option.

CI runs full tests and same-volume smoke checks on `C:`. Reports are uploaded as
`windows-script-reports`. This does not test clean-machine installation or real
external-drive behavior.

Continue with the [full workflow](workflows.md) to use the CLI yourself.
