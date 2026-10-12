# Windows two-drive acceptance test

Run these checks with two drives connected. They use generated files in fresh
`iarchive-sandbox\run-<id>` folders, never your photo archive. Keep the drives
connected throughout the tests. Both drive tests must pass.

## 1. Open a test tab

From the checkout in Windows Terminal, open a new PowerShell tab:

```powershell
wt -w 0 new-tab -d "$PWD" powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -NoExit
```

Run the remaining commands in that new tab. No administrator access is needed
for the tests. The execution policy applies only to this session.

Without Windows Terminal, run this once in your current PowerShell window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

Run each command separately. Stop if a command fails.

## 2. Update, set up and test

```powershell
git pull --ff-only
.\scripts\windows\setup.ps1
```

Setup may install missing tools. If it does, open a fresh terminal in the checkout
and repeat step 1 before continuing. See [Windows setup](windows-testing.md) for
installation details and troubleshooting.

Replace `D:` and `E:` with your drive letters:

```powershell
.\scripts\windows\test.ps1 -ExternalDrives 'D:', 'E:'
```

Keep the printed summary. It must show `passed: true` and
`automated_checks: PASSED`.

## 3. Test copying and recovery

```powershell
.\scripts\windows\test-two-drives.ps1 -PrimaryDrive 'D:' -SecondaryDrive 'E:'
```

This checks copying, repeat runs, file hashes, backup recovery, corruption,
missing files and an unavailable folder. It requires Windows to report different
physical disks behind the two drive letters.

On exFAT, renaming a folder can change its identity. The script checks that the
old cycle rejects the change, compares the restored files, then verifies them
in a new cycle.

Wait for `PASS` before continuing.

## 4. Test interruption and resume

```powershell
.\scripts\windows\test-two-drive-interruption.ps1 -PrimaryDrive 'D:' -SecondaryDrive 'E:'
```

This creates a 512 MiB file, stops its own copy process while the copy is
incomplete, resumes it, and checks the finished file. It also checks that the
unfinished copy stays outside the media folder and remains unchanged.

If the copy finishes too quickly to interrupt, retry with a larger file:

```powershell
.\scripts\windows\test-two-drive-interruption.ps1 -PrimaryDrive 'D:' -SecondaryDrive 'E:' -InterruptionMiB 1024
```

The supported range is 64–2048 MiB. Each retry uses fresh folders and keeps the
previous results. A failed interruption test means acceptance is incomplete;
retrying this script does not require rerunning step 3.

Do not unplug a drive. This tests a stopped process, not a USB disconnect.

## 5. Keep the results

Both scripts print `PASS` and a summary path:

```text
.smoke\two-drive-<id>\summary.json
.smoke\two-drive-interruption-<id>\summary.json
```

Both summaries must show `passed: true`. The interruption summary must also show
`interruption.status: PASSED`.

Keep the automated test summary from step 2, both drive-test summaries, their
`reports` folders and the generated folders on both drives. If a test fails,
keep its console error and any saved reports for review. No cleanup is needed.

These tests check generated files on local drives. They do not prove iCloud
coverage or give permission to delete archive files.
