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

## 2. Prepare a fresh source and destination

Start a child PowerShell session with a process-only execution policy. It inherits
the checkout location; keep this session open for all remaining steps:

```powershell
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned
```

Paste the following in that session. Change only the two drive-letter values.
The helpers refuse unavailable drives, linked ancestors, existing run directories
and an unrecognized sandbox marker.

```powershell
$ErrorActionPreference = 'Stop'
# Expected native failures below are checked explicitly, including in PowerShell 7.
$PSNativeCommandUseErrorActionPreference = $false
. .\scripts\windows\common.ps1
Assert-WindowsHost
$projectEnvironment = Enter-ProjectEnvironment $PWD.Path
$exe = Join-Path $PWD.Path 'target\release\iarchive.exe'
Assert-Check (Test-Path -LiteralPath $exe -PathType Leaf) 'Run setup first.'
$primaryDrive = Get-ExternalDriveRoot 'D:'
$secondaryDrive = Get-ExternalDriveRoot 'E:'
Assert-Check ($primaryDrive -ne $secondaryDrive) 'Choose two different drives.'
$runId = [guid]::NewGuid().ToString('N')
$primaryRun = Get-DriveTestDirectory $primaryDrive $runId
$secondaryRun = Get-DriveTestDirectory $secondaryDrive $runId
$source = Join-Path $primaryRun 'media'
$secondary = New-FreshDirectory (Join-Path $secondaryRun 'replica-media')
$primaryArtifacts = New-FreshDirectory (Join-Path $primaryRun 'artifacts')
$secondaryArtifacts = New-FreshDirectory (Join-Path $secondaryRun 'artifacts')
$review = New-FreshDirectory (Join-Path $PWD.Path ".smoke\two-drive-$runId")
$reports = New-FreshDirectory (Join-Path $review 'reports')
$state = Join-Path $review 'state'
$cycle = "drive-$runId"
Invoke-CheckedNative uv @('run', '--locked', '--project', 'image-worker',
    'python', 'fixtures/generate.py', '--output', $source, '--clean', '--with-heic')
# Exercise preservation of a sidecar, an unknown format and a zero-byte file.
[IO.File]::WriteAllText((Join-Path $source 'synthetic.xmp'), '<xmp>synthetic</xmp>')
[IO.File]::WriteAllBytes((Join-Path $source 'synthetic.xyz'), [byte[]]@(1, 2, 3))
[IO.File]::WriteAllBytes((Join-Path $source 'synthetic-empty.bin'), [byte[]]@())
$before = @(Get-SyntheticSnapshot $source)
Write-JsonFile (Join-Path $review 'source-before.json') $before
$replicaArgs = @('--cycle', $cycle, '--secondary', $secondary,
    '--primary-artifacts', $primaryArtifacts, '--secondary-artifacts', $secondaryArtifacts)
function Invoke-Test {
    param([string]$Name, [string[]]$Command, [int]$ExpectedExit = 0)
    Invoke-IarchiveJson $exe $state $reports $Name $Command $ExpectedExit
}
function Convert-ReportPath {
    param([string]$Path)
    if ($Path.StartsWith('\\?\UNC\')) { return '\\' + $Path.Substring(8) }
    if ($Path.StartsWith('\\?\')) { return $Path.Substring(4) }
    return $Path
}
Write-Host "Source: $source`nDestination: $secondary`nReports: $reports"
$null = Invoke-Test 'import' @('import', '--source', $source, '--cycle', $cycle)
$preview = Invoke-Test 'preview' (@('--dry-run', 'replicate') + $replicaArgs)
Assert-Check (-not $preview.complete) 'A preview must not claim completion.'
Assert-Check ($preview.planned_files -eq $before.Count) 'Unexpected planned file count.'
Assert-Check (@(Get-ChildItem -LiteralPath $secondary -Force).Count -eq 0) 'Preview wrote media.'
Assert-Check ($preview.independent_physical_drives -eq 'VERIFIED BY OS TOPOLOGY') 'Physical independence is not verified.'
```

The fixture should contain 12 files. No `--allow-unverified-devices` is used:
this test must establish distinct backing disks according to Windows. Two drive
letters alone are insufficient. If topology discovery or capacity preflight
refuses the operation, stop and retain `preview.json` plus the console error.
We can discuss a separately acknowledged byte-only test if the drivers cannot
provide the required information. OS topology remains dependent on driver accuracy.

## 3. Copy, rerun and independently compare bytes

```powershell
$copied = Invoke-Test 'replicate' (@('replicate') + $replicaArgs)
Assert-Check ($copied.complete -and $copied.copied_files -eq $before.Count) 'Copy did not complete.'
$rerun = Invoke-Test 'replicate-rerun' (@('replicate') + $replicaArgs)
Assert-Check ($rerun.complete -and $rerun.copied_files -eq 0) 'Rerun copied matching files.'
$verified = Invoke-Test 'verify' (@('verify', '--deep') + $replicaArgs)
Assert-Check ($verified.complete -and $verified.verified_files -eq $before.Count) 'Deep verification failed.'
Assert-Check ($verified.local_replica -eq 'LOCAL BYTES VERIFIED') 'Local bytes were not verified.'
Assert-Check ($verified.cloud_coverage -eq 'NOT VERIFIED') 'Unexpected cloud claim.'
Assert-UnchangedSnapshot $source $before
$after = @(Get-SyntheticSnapshot $secondary)
Write-JsonFile (Join-Path $review 'secondary-after.json') $after
$expectedBytes = ConvertTo-Json -InputObject @($before | Select-Object path, bytes, sha256) -Compress
$actualBytes = ConvertTo-Json -InputObject @($after | Select-Object path, bytes, sha256) -Compress
Assert-Check ($expectedBytes -eq $actualBytes) 'Independent PowerShell hashes differ.'
```

This independently reads both file sets using PowerShell SHA-256. Source bytes
and modification times must stay unchanged. Target timestamps are retained in
the snapshot for review; different filesystems can have different precision.
Require exact relative paths, lengths and hashes regardless of timestamp precision.

## 4. Check all bundles and recover from each drive

```powershell
$bundles = @($verified.artifact_bundles | ForEach-Object { Convert-ReportPath $_ })
Assert-Check ($bundles.Count -eq 3) 'Expected state and two drive bundles.'
$summaryHashes = @()
foreach ($bundle in $bundles) {
    foreach ($name in @('manifest.jsonl', 'manifest.sqlite', 'summary.json')) {
        Assert-Check (Test-Path -LiteralPath (Join-Path $bundle $name) -PathType Leaf) "Missing $name."
    }
    $manifestHash = (Get-FileHash -LiteralPath (Join-Path $bundle 'manifest.jsonl') -Algorithm SHA256).Hash.ToLowerInvariant()
    $databaseHash = (Get-FileHash -LiteralPath (Join-Path $bundle 'manifest.sqlite') -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-Check ($manifestHash -eq $verified.manifest_sha256) 'Manifest digest differs.'
    Assert-Check ($databaseHash -eq $verified.database_backup_sha256) 'Database digest differs.'
    $summaryHashes += (Get-FileHash -LiteralPath (Join-Path $bundle 'summary.json') -Algorithm SHA256).Hash
}
Assert-Check (@($summaryHashes | Select-Object -Unique).Count -eq 1) 'Summary bundles differ.'
$number = 0
foreach ($artifactRoot in @($primaryArtifacts, $secondaryArtifacts)) {
    $driveBundle = @($bundles | Where-Object { [IO.Path]::GetDirectoryName($_) -ieq $artifactRoot })
    Assert-Check ($driveBundle.Count -eq 1) 'Missing drive recovery bundle.'
    $number++
    $recovered = New-FreshDirectory (Join-Path $review "recovered-$number")
    Copy-Item -LiteralPath (Join-Path $driveBundle[0] 'manifest.sqlite') -Destination $recovered
    $recheck = Invoke-IarchiveJson $exe $recovered $reports "recovery-$number" (@('verify', '--deep') + $replicaArgs)
    Assert-Check ($recheck.complete -and $recheck.verified_files -eq $before.Count) 'Restored state failed verification.'
}
```

The standalone backups capture verified files in `PUBLISHING`, before the final
publication record. They require fresh verification after restore. Each recovery
uses a new empty state directory, with no old WAL/SHM files mixed into it.

## 5. Prove failures invalidate completion

These commands alter only the generated destination. Expected application exit
code 2 means a finding was correctly rejected, not a PowerShell test failure.
The helpers stop on any unexpected exit code.

First corrupt one generated file and confirm replication refuses to overwrite it:

```powershell
$relative = $before[0].path.Replace('/', '\')
$victim = Join-Path $secondary $relative
$original = [IO.File]::ReadAllBytes((Join-Path $source $relative))
[IO.File]::WriteAllText($victim, 'deliberate synthetic corruption')
try {
    $bad = Invoke-Test 'corrupt-verify' (@('verify', '--deep') + $replicaArgs) 2
    Assert-Check (-not $bad.complete -and $bad.hash_mismatches -gt 0) 'Corruption was accepted.'
    $saved = Invoke-Test 'corrupt-status' @('status', '--cycle', $cycle)
    Assert-Check ($saved.replica_state -eq 'INCOMPLETE') 'Saved completion remained verified.'
    $refused = Invoke-Test 'conflict-refused' (@('replicate') + $replicaArgs) 2
    Assert-Check ($refused.copied_files -eq 0) 'Conflict preflight copied files.'
    Assert-Check ([IO.File]::ReadAllText($victim) -eq 'deliberate synthetic corruption') 'Conflict was overwritten.'
} finally {
    # Explicit test repair of our own generated file, not application conflict handling.
    [IO.File]::WriteAllBytes($victim, $original)
}
$null = Invoke-Test 'repaired-verify' (@('verify', '--deep') + $replicaArgs)
```

Next move one replica file outside media, verify the missing-file finding, and
let replication supply a new copy. Retain the moved file as a test artifact:

```powershell
Move-Item -LiteralPath $victim -Destination (Join-Path $secondaryArtifacts 'removed-synthetic-file')
$missing = Invoke-Test 'missing-verify' (@('verify', '--deep') + $replicaArgs) 2
Assert-Check (-not $missing.complete -and $missing.missing_files -eq 1) 'Missing file was accepted.'
$repaired = Invoke-Test 'missing-replicate' (@('replicate') + $replicaArgs)
Assert-Check ($repaired.complete -and $repaired.copied_files -eq 1) 'Missing file was not recopied.'
$null = Invoke-Test 'missing-repaired-verify' (@('verify', '--deep') + $replicaArgs)
```

Finally simulate an unavailable root, restoring the same folder in `finally`:

```powershell
$unavailable = Join-Path $secondaryRun 'temporarily-unavailable'
Move-Item -LiteralPath $secondary -Destination $unavailable
try {
    $missingRoot = Invoke-Test 'unavailable-verify' (@('verify', '--deep') + $replicaArgs) 2
    Assert-Check (-not $missingRoot.complete) 'Unavailable root was accepted.'
    Assert-Check (-not (Test-Path -LiteralPath $secondary)) 'Missing root was recreated.'
} finally {
    Move-Item -LiteralPath $unavailable -Destination $secondary
}
$final = Invoke-Test 'final-verify' (@('verify', '--deep') + $replicaArgs)
Assert-Check ($final.complete) 'Final verification failed.'
Assert-UnchangedSnapshot $source $before
```

## 6. Optional manual interruption and resume

Run this separately after the preceding checks pass. It adds a generated 512 MiB
binary file in a new source, and uses an empty destination and a different cycle.
It tests raw bytes, not video decoding. Capacity preflight still applies.

```powershell
$interruptSource = New-FreshDirectory (Join-Path $primaryRun 'interrupt-source')
$interruptTarget = New-FreshDirectory (Join-Path $secondaryRun 'interrupt-target')
$interruptCycle = "interrupt-$runId"
$stream = [IO.File]::Open((Join-Path $interruptSource 'synthetic-large.bin'), [IO.FileMode]::CreateNew)
try { $stream.SetLength(512MB) } finally { $stream.Dispose() }
$interruptArgs = @('--cycle', $interruptCycle, '--secondary', $interruptTarget,
    '--primary-artifacts', $primaryArtifacts, '--secondary-artifacts', $secondaryArtifacts)
$null = Invoke-Test 'interrupt-import' @('import', '--source', $interruptSource, '--cycle', $interruptCycle)
$null = Invoke-Test 'interrupt-preview' (@('--dry-run', 'replicate') + $interruptArgs)
Write-Host "Watch copy partials under $secondaryArtifacts"
& $exe --state-dir $state --json replicate @interruptArgs
```

Watch `artifacts\partials-*\*.partial` under the printed secondary artifact path
in a second window. Press **Ctrl+C** in the command window while a partial is
growing. If the copy completes first, that attempt did not test interruption.
Do not alter a partial or place it in media. After interrupting, run:

```powershell
$interrupted = Invoke-Test 'interrupted-status' @('status', '--cycle', $interruptCycle)
Assert-Check ($interrupted.replica_state -ne 'VERIFIED') 'Interrupted run claimed verification.'
$resumed = Invoke-Test 'interrupt-resume' (@('replicate') + $interruptArgs)
Assert-Check ($resumed.complete) 'Resume did not complete.'
$deep = Invoke-Test 'interrupt-deep' (@('verify', '--deep') + $interruptArgs)
Assert-Check ($deep.complete -and $deep.verified_files -eq 1) 'Resumed bytes failed deep verification.'
$sourceHash = (Get-FileHash -LiteralPath (Join-Path $interruptSource 'synthetic-large.bin') -Algorithm SHA256).Hash
$targetHash = (Get-FileHash -LiteralPath (Join-Path $interruptTarget 'synthetic-large.bin') -Algorithm SHA256).Hash
Assert-Check ($sourceHash -eq $targetHash) 'Resumed file differs.'
```

Confirm the interrupted partial remains outside media. Resume restarts unfinished
files from byte zero; it does not trust partial contents. Finished files are
independently rehashed. An interruption before a partial appears exercises a
different stage and does not prove interrupted-copy recovery.

Do not physically unplug a drive holding your archives during this test. A USB
disconnect can affect the filesystem beyond these directories. A real disconnect
test needs disposable drives; Ctrl+C and folder renaming do not establish that
hardware behavior.

## 7. Record the outcome

Keep the commit ID, automated `summary.json`, the `.smoke\two-drive-<id>` directory,
and both printed drive-run paths. Report which numbered step failed, the command
report and the console error. Generated reports contain local paths.

| Check | Required result |
| --- | --- |
| Automated Windows checks | Full run passes, including each selected drive. |
| Two-drive topology | `VERIFIED BY OS TOPOLOGY`; no acknowledgment flag. |
| Copy and repeat | All files copied initially; zero copies on repeat. |
| Deep verification | All paths, lengths and independent hashes match. |
| Source preservation | Source hashes and modification times unchanged. |
| Bundles and restore | All three bundles match; restore from either drive succeeds. |
| Failure handling | Findings exit 2, invalidate completion and preserve conflicts. |
| Final repaired fixture | Deep verification passes again. |
| Optional interruption | Record separately whether a growing copy was interrupted and resumed. |

Finish the child session with `exit`; its environment settings disappear. No
cleanup command is required. These results establish local synthetic behavior.
They do not establish full iCloud coverage, completed iPhone uploads, Live Photo
relationships, offsite protection or permission to remove anything from iCloud.
