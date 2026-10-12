# Windows two-drive tests

Use two connected drives and a regular PowerShell terminal in the checkout.
Replace `D:` and `E:` with your drive letters. Run commands separately and stop
if one fails.

Tests write fresh generated files under `iarchive-sandbox\run-<id>` on each drive.
They do not scan your photo archive. Keep both drives connected.

## 1. Open a test tab

In Windows Terminal:

```powershell
wt -w 0 new-tab -d "$PWD" powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -NoExit
```

Without Windows Terminal, set policy for the current process:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

## 2. Update and test

Run these in the test tab:

```powershell
git pull --ff-only
.\scripts\windows\setup.ps1
.\scripts\windows\test.ps1 -ExternalDrives 'D:', 'E:'
```

If setup installed tools, open a fresh test tab before testing.
Require `passed: true` and `automated_checks: PASSED` in the printed summary.
See [setup help](windows-testing.md) if needed.

## 3. Copy and recover

```powershell
.\scripts\windows\test-two-drives.ps1 -PrimaryDrive 'D:' -SecondaryDrive 'E:'
```

This checks copying, reruns, hashes, both backup recoveries, corruption, missing
files and an unavailable folder. It requires Windows to report distinct physical
disks. On exFAT, a restored folder may get a new identity; the script checks the
old cycle's refusal, then verifies the unchanged files in a new cycle.

Wait for `PASS`.

## 4. Interrupt and resume

```powershell
.\scripts\windows\test-two-drive-interruption.ps1 -PrimaryDrive 'D:' -SecondaryDrive 'E:'
```

This stops its own copy process while a generated 512 MiB file is incomplete,
then resumes and verifies the copy. The old partial must stay unchanged outside
media. It does not test unplugging a drive.

If the copy finishes too quickly, retry with a larger file:

```powershell
.\scripts\windows\test-two-drive-interruption.ps1 -PrimaryDrive 'D:' -SecondaryDrive 'E:' -InterruptionMiB 1024
```

Sizes from 64–2048 MiB are supported. Retries use fresh folders; step 3 need not
be repeated. Both drive scripts must pass.

## 5. Keep the results

Keep all printed summaries, their `reports` folders and both drive-run folders.
Require `passed: true` in both drive summaries and `interruption.status: PASSED`
in the interruption summary. No cleanup is required.

Passing these checks proves the tested synthetic behavior, not complete iCloud
coverage or permission to delete media. Do not unplug drives holding archives.
