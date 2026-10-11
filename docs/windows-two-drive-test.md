# Windows two-drive acceptance test

Use this plan after updating the checkout. It tests generated files on two drives,
not your photo archive. Keep both drives connected and stop other programs writing
to the test directories. Run commands sequentially from the checkout in a regular,
64-bit PowerShell window. Windows Home is supported.

The directories below are scoped storage, not OS isolation. Only fresh marked
`iarchive-sandbox\run-<id>` directories receive drive writes. Existing archives are
not inputs. Leave the generated files and reports in place until we review them.

## 1. Update, build and run automated checks

```powershell
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw 'Stop: checkout update failed.' }
git rev-parse HEAD
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\setup.ps1
if ($LASTEXITCODE -ne 0) { throw 'Stop: setup failed.' }
```

If setup installed prerequisites, open a fresh terminal in the checkout before
continuing. Replace `D:` and `E:` below with your two drive letters:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -Command "& '.\scripts\windows\test.ps1' -ExternalDrives 'D:', 'E:'"
if ($LASTEXITCODE -ne 0) { throw 'Stop: automated tests failed.' }
```

Save the printed `summary.json` location. Require `passed: true` and
`automated_checks: PASSED`. This covers inventory, deep hashing, image review,
safe exports and same-volume replica checks on each drive. Those script fixtures
end with deliberate corruption; do not reuse them for the clean test below.
See [setup and troubleshooting](windows-testing.md) if a command fails.

## 2. Run the two-drive script

Replace the example letters with your primary and secondary drive letters:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test-two-drives.ps1 -PrimaryDrive 'D:' -SecondaryDrive 'E:'
if ($LASTEXITCODE -ne 0) { throw 'Stop: two-drive acceptance failed.' }
```

The script automates the remaining acceptance checks:

- Generate a fresh 12-file source containing PNG, JPEG, HEIC, a sidecar, an
  unknown format and a zero-byte file. Create an empty replica on the other drive.
- Dry-run replication and require Windows to report distinct physical backing
  disks before copying. No acknowledgment flag relaxes this requirement.
- Copy every file, rerun with zero additional copies, and deeply verify both
  roots. Independently compare relative paths, sizes and SHA-256 hashes using
  PowerShell, and confirm source bytes and modification times stay unchanged.
- Check the three manifest/database/summary bundles, then recover into separate
  empty state directories from each drive's standalone SQLite backup and verify
  again. Backups capture `PUBLISHING` and need that fresh verification.
- Corrupt a generated replica file, confirm verification invalidates completion,
  and confirm replication preserves the conflict. Explicitly repair that fixture.
- Move a generated replica file outside media, reject the missing-file finding,
  and recopy it. Rename the generated replica folder to simulate an unavailable
  root, confirm it is not recreated, restore it, and finish with deep verification.
- Save the commit ID, paths, command reports, snapshots and an overall summary.

These writes are confined to new test-run directories and project storage.
Directories with linked ancestors or unrecognized ownership markers are refused.
The script installs no prerequisites and does not run the full quality suite;
complete step 1 first. It calculates the checkout location from the script path
and restores its process environment and working directory when it finishes.

Two drive letters alone are insufficient. If topology discovery or capacity
preflight refuses the operation, stop and retain `preview.json` plus the console
error. We can discuss a separately acknowledged byte-only test if the drivers
cannot provide the required information. OS topology depends on driver accuracy.

## 3. Optional interruption and resume

To include an automated interruption check, use:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test-two-drives.ps1 -PrimaryDrive 'D:' -SecondaryDrive 'E:' -TestInterruption
if ($LASTEXITCODE -ne 0) { throw 'Stop: acceptance or interruption check failed.' }
```

This runs all checks in a fresh run and adds a separate generated 512 MiB binary
file. It watches its own copy process, terminates that exact child after observing
an incomplete `.partial` file, checks that no target was published or marked
verified, resumes the copy, and independently verifies the result. It also checks
that the retained partial remains unchanged outside media. Unfinished files start
again from byte zero; partial contents are never trusted. This tests raw-byte
copying, not video decoding.

The observation window is bounded to 180 seconds. If a copy finishes before the
script can interrupt it, the check fails rather than claiming coverage. Retry
with a larger fixture, subject to available space and the normal capacity margin:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\windows\test-two-drives.ps1 -PrimaryDrive 'D:' -SecondaryDrive 'E:' -TestInterruption -InterruptionMiB 1024
```

The supported fixture range is 64–2048 MiB. Each retry creates fresh directories;
previous runs and interrupted partials are retained. Process termination tests
abrupt interruption; it does not test graceful Ctrl+C handling or a USB disconnect.
Do not physically unplug a drive holding your archives during these checks. A
real disconnect test needs disposable drives because it can affect the filesystem
beyond the generated directories.

## 4. Read and retain the results

A successful run prints `PASS` and its summary location:

```text
.smoke\two-drive-<id>\summary.json
```

Require `passed: true`. The summary records the checkout commit, PowerShell
version, both drive-run paths and the completed checks. `checks` includes the
verified file count, physical topology result, two successful backup recoveries
and the final verification report path. `interruption.status` is `NOT REQUESTED`
by default or `PASSED` when the optional check succeeds. A requested interruption
failure makes the overall run fail, even if the base checks already passed.

On failure the script exits 1 and retains a summary when its run directory has
been created. Expected application findings exit 2 internally; the script checks
them explicitly and continues only when the expected rejection occurred.

Keep the automated test summary from step 1, this two-drive summary, the retained
`reports` directory and both printed drive-run paths. Reports contain local paths.
Per-command reports include `preview.json`, `verify.json`, `recovery-1.json`,
`recovery-2.json`, the negative cases and `final-verify.json`. Optional interruption
reports include the observed partial and the child process's stdout/stderr logs.
Source and target snapshots retain timestamps; target precision can differ by
filesystem, while relative paths, lengths and hashes must match exactly.

No cleanup command is required. These results establish local synthetic behavior,
not full iCloud coverage, completed iPhone uploads, Live Photo relationships,
offsite protection or permission to remove anything from iCloud. CI exercises the
shared workflow on one volume under PowerShell 7 and Windows PowerShell 5.1;
physical-drive behavior still requires this run on your Windows machine.
