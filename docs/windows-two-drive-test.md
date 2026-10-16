# Windows two-drive tests

Use two connected drives and a regular PowerShell terminal in the checkout.
Replace `D:` and `E:` with your drive letters. Run commands separately and stop
if one fails.

Tests write fresh generated files under `iarchive-sandbox\run-<id>` on each drive.
They do not scan your photo archive. Keep both drives connected.

## 1. Allow scripts in this session

Run once in your PowerShell window or tab:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

This lasts only for the session. Repeat it in each new window or tab.

## 2. Update and test

Run these in the same window or tab:

```powershell
git pull --ff-only
.\scripts\windows\setup.ps1
.\scripts\windows\test.ps1 -ExternalDrives 'D:', 'E:'
```

If setup installed tools, open a fresh tab before testing and repeat the policy
command.
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

## 5. Review, quarantine and restore

```powershell
.\scripts\windows\test-review-two-drives.ps1 -PrimaryDrive 'D:' -SecondaryDrive 'E:'
```

This uses five generated files. It proves their metadata groups, flags two images,
quarantines four associated files, verifies both copies, then restores the
original paths, hashes and timestamps. The secondary media must stay unchanged.
It also resumes a simulated download in a second cycle, checks cache reuse and
cross-cycle matches, and copies it without changing the earlier archive.
Wait for `PASS` and `download_recovery: PASSED` in the summary.

## 6. Keep the results

Keep all printed summaries, their `reports` folders and both drive-run folders.
Require `passed: true` in both drive summaries and `interruption.status: PASSED`
in the interruption summary. Require `passed: true` in the review summary too.
No cleanup is required.

Passing these checks proves the tested synthetic behavior, not complete iCloud
coverage or permission to delete media. Do not unplug drives holding archives.
