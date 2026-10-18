# Shared synthetic acceptance checks. No commands run on dot-source.
function Convert-ReplicaReportPath {
    param([string]$Path)
    if ($Path.StartsWith('\\?\UNC\')) { return '\\' + $Path.Substring(8) }
    if ($Path.StartsWith('\\?\')) { return $Path.Substring(4) }
    return $Path
}

function Assert-ReplicaComplete {
    param($Report, [int]$Count)
    Assert-Check ($Report.complete -and $Report.verified_files -eq $Count) 'Deep byte verification did not complete.'
    Assert-Check ($Report.local_replica -eq 'LOCAL BYTES VERIFIED') 'Local bytes were not verified.'
    Assert-Check ($Report.cloud_coverage -eq 'NOT VERIFIED') 'Unexpected cloud claim.'
}

function Invoke-ReplicaAcceptance {
    param([string]$Exe, [string]$Source, [string]$Secondary,
        [string]$PrimaryArtifacts, [string]$SecondaryArtifacts,
        [string]$State, [string]$Reports, [string]$Cycle,
        [bool]$RequireIndependentDrives = $true)
    $before = @(Get-SyntheticSnapshot $Source)
    Assert-Check ($before.Count -gt 0) 'Synthetic source is empty.'
    Write-JsonFile (Join-Path $Reports 'source-before.json') $before
    $replicaArgs = @('--cycle', $Cycle, '--secondary', $Secondary,
        '--primary-artifacts', $PrimaryArtifacts, '--secondary-artifacts', $SecondaryArtifacts)
    # The entry script always requires independence. CI exercises the workflow
    # on one volume without claiming physical-drive coverage.
    if (-not $RequireIndependentDrives) { $replicaArgs += '--allow-unverified-devices' }
    function Invoke-ReplicaCommand {
        param([string]$Name, [string[]]$Command, [int]$ExpectedExit = 0)
        Invoke-IarchiveJson $Exe $State $Reports $Name $Command $ExpectedExit
    }

    Write-Host 'Checking dry-run and physical-drive topology.'
    $null = Invoke-ReplicaCommand 'import' @('import', '--source', $Source, '--cycle', $Cycle)
    $preview = Invoke-ReplicaCommand 'preview' (@('--dry-run', 'replicate') + $replicaArgs)
    Assert-Check (-not $preview.complete -and $preview.planned_files -eq $before.Count) 'Unexpected preview result.'
    foreach ($root in @($Secondary, $PrimaryArtifacts, $SecondaryArtifacts)) {
        Assert-Check (@(Get-ChildItem -LiteralPath $root -Force).Count -eq 0) "Preview wrote files under $root."
    }
    if ($RequireIndependentDrives) {
        Assert-Check ($preview.independent_physical_drives -eq 'VERIFIED BY OS TOPOLOGY') 'Physical independence is not verified.'
    }

    Write-Host 'Copying, checking a safe rerun and comparing independent hashes.'
    $copied = Invoke-ReplicaCommand 'replicate' (@('replicate') + $replicaArgs)
    Assert-ReplicaComplete $copied $before.Count
    Assert-Check ($copied.copied_files -eq $before.Count) 'Initial copy count differs.'
    $rerun = Invoke-ReplicaCommand 'replicate-rerun' (@('replicate') + $replicaArgs)
    Assert-ReplicaComplete $rerun $before.Count
    Assert-Check ($rerun.copied_files -eq 0) 'Rerun copied matching files.'
    $verified = Invoke-ReplicaCommand 'verify' (@('verify', '--deep') + $replicaArgs)
    Assert-ReplicaComplete $verified $before.Count
    $after = @(Get-SyntheticSnapshot $Secondary)
    Write-JsonFile (Join-Path $Reports 'secondary-after.json') $after
    $expectedBytes = ConvertTo-Json -InputObject @($before | Select-Object path, bytes, sha256) -Compress
    $actualBytes = ConvertTo-Json -InputObject @($after | Select-Object path, bytes, sha256) -Compress
    Assert-Check ($expectedBytes -eq $actualBytes) 'Independent PowerShell hashes differ.'
    Assert-UnchangedSnapshot $Source $before

    Write-Host 'Checking bundle digests and recovering from each drive backup.'
    $bundles = @($verified.artifact_bundles | ForEach-Object { Convert-ReplicaReportPath $_ })
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
    foreach ($artifactRoot in @($PrimaryArtifacts, $SecondaryArtifacts)) {
        $driveBundle = @($bundles | Where-Object { [IO.Path]::GetDirectoryName($_) -ieq $artifactRoot })
        Assert-Check ($driveBundle.Count -eq 1) 'Missing drive recovery bundle.'
        $number++
        $recovered = New-FreshDirectory (Join-Path ([IO.Path]::GetDirectoryName($State)) "recovered-$number")
        Copy-Item -LiteralPath (Join-Path $driveBundle[0] 'manifest.sqlite') -Destination $recovered
        $recheck = Invoke-IarchiveJson $Exe $recovered $Reports "recovery-$number" (@('verify', '--deep') + $replicaArgs)
        Assert-ReplicaComplete $recheck $before.Count
    }

    Write-Host 'Checking corruption, no-clobber conflicts and missing files.'
    $relative = $before[0].path.Replace('/', [IO.Path]::DirectorySeparatorChar)
    $victim = Join-Path $Secondary $relative
    $original = [IO.File]::ReadAllBytes((Join-Path $Source $relative))
    [IO.File]::WriteAllText($victim, 'deliberate synthetic corruption')
    try {
        $bad = Invoke-ReplicaCommand 'corrupt-verify' (@('verify', '--deep') + $replicaArgs) 2
        Assert-Check (-not $bad.complete -and $bad.hash_mismatches -gt 0) 'Corruption was accepted.'
        $saved = Invoke-ReplicaCommand 'corrupt-status' @('status', '--cycle', $Cycle)
        Assert-Check ($saved.replica_state -eq 'INCOMPLETE') 'Saved completion remained verified.'
        $refused = Invoke-ReplicaCommand 'conflict-refused' (@('replicate') + $replicaArgs) 2
        Assert-Check (-not $refused.complete -and $refused.copied_files -eq 0) 'Conflict preflight copied files.'
        Assert-Check ([IO.File]::ReadAllText($victim) -eq 'deliberate synthetic corruption') 'Conflict was overwritten.'
    } finally {
        # Explicit repair of our generated fixture, not application conflict handling.
        [IO.File]::WriteAllBytes($victim, $original)
    }
    $null = Invoke-ReplicaCommand 'repaired-verify' (@('verify', '--deep') + $replicaArgs)
    Move-Item -LiteralPath $victim -Destination (Join-Path $SecondaryArtifacts 'removed-synthetic-file')
    $missing = Invoke-ReplicaCommand 'missing-verify' (@('verify', '--deep') + $replicaArgs) 2
    Assert-Check (-not $missing.complete -and $missing.missing_files -eq 1) 'Missing file was accepted.'
    $repaired = Invoke-ReplicaCommand 'missing-replicate' (@('replicate') + $replicaArgs)
    Assert-ReplicaComplete $repaired $before.Count
    Assert-Check ($repaired.copied_files -eq 1) 'Missing file was not recopied.'

    Write-Host 'Checking an unavailable root, then restoring and verifying it.'
    $unavailable = Join-Path ([IO.Path]::GetDirectoryName($Secondary)) 'temporarily-unavailable'
    Assert-Check (-not (Test-Path -LiteralPath $unavailable)) 'Unavailable-root test path already exists.'
    Move-Item -LiteralPath $Secondary -Destination $unavailable
    try {
        $missingRoot = Invoke-ReplicaCommand 'unavailable-verify' (@('verify', '--deep') + $replicaArgs) 2
        Assert-Check (-not $missingRoot.complete) 'Unavailable root was accepted.'
        Assert-Check (-not (Test-Path -LiteralPath $Secondary)) 'Missing root was recreated.'
    } finally {
        Move-Item -LiteralPath $unavailable -Destination $Secondary
    }
    $restored = Invoke-IarchiveJson $Exe $State $Reports 'restored-verify' (@('verify', '--deep') + $replicaArgs) @(0, 2)
    $finalCycle = $Cycle
    $finalState = $State
    $identityChanged = $false
    if (-not $restored.complete) {
        # exFAT can change a directory file ID when it is renamed away and back.
        # Preserve the old binding and accept only its explicit identity refusal.
        Assert-Check (@($restored.issues).Count -eq 1 -and $restored.issues[0] -eq 'source or bound secondary root/volume changed; no media was read') 'Restored root failed for a reason other than its identity.'
        Assert-Check ($restored.verified_files -eq 0 -and $restored.copied_files -eq 0) 'Changed root identity was accepted.'
        $identityChanged = $true
        Assert-UnchangedSnapshot $Source $before
        $restoredBytes = ConvertTo-Json -InputObject @(Get-SyntheticSnapshot $Secondary | Select-Object path, bytes, sha256) -Compress
        Assert-Check ($restoredBytes -eq $expectedBytes) 'Restored replica bytes differ.'
        $finalCycle = "$Cycle-restored"
        # One state database cannot register the same media under two cycles.
        $finalState = New-FreshDirectory (Join-Path ([IO.Path]::GetDirectoryName($State)) 'restored-state')
        Write-Host 'Restored directory identity changed; verifying in a fresh state directory.'
        $null = Invoke-IarchiveJson $Exe $finalState $Reports 'restored-import' @('import', '--source', $Source, '--cycle', $finalCycle)
        $replicaArgs[1] = $finalCycle
    } else {
        Assert-ReplicaComplete $restored $before.Count
    }
    $final = Invoke-IarchiveJson $Exe $finalState $Reports 'final-verify' (@('verify', '--deep') + $replicaArgs)
    Assert-ReplicaComplete $final $before.Count
    Assert-UnchangedSnapshot $Source $before
    return [pscustomobject]@{
        passed = $true
        files = $before.Count
        source = $Source
        secondary = $Secondary
        independent_physical_drives = $final.independent_physical_drives
        recovery_backups = $number
        restored_identity_changed = $identityChanged
        final_cycle = $finalCycle
        final_state = $finalState
        final_report = (Join-Path $Reports 'final-verify.json')
    }
}

function New-ReplicaProcess {
    param([string]$Exe, [string[]]$ArgumentList)
    $process = [Diagnostics.Process]::new()
    $process.StartInfo.FileName = $Exe
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    $process.StartInfo.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
    if ($null -ne $process.StartInfo.PSObject.Properties['ArgumentList']) {
        foreach ($argument in $ArgumentList) { $process.StartInfo.ArgumentList.Add($argument) }
    } else {
        # Windows PowerShell 5.1 lacks ArgumentList. Quote each Windows argv value,
        # doubling backslashes before quotes and at the end of a quoted value.
        $process.StartInfo.Arguments = (@($ArgumentList | ForEach-Object {
            '"' + [regex]::Replace([regex]::Replace($_, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
        }) -join ' ')
    }
    return $process
}

function Invoke-InterruptedCopy {
    param([string]$Exe, [string[]]$ArgumentList, [string]$PartialRoot,
        [long]$ExpectedBytes, [string]$Reports, [int]$TimeoutSeconds = 180)
    $process = New-ReplicaProcess $Exe $ArgumentList
    $started = $false
    $stdout = $null; $stderr = $null
    $observed = $null
    try {
        $started = $process.Start()
        Assert-Check $started 'Could not start the interruption test process.'
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while (-not $process.HasExited -and $timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            foreach ($directory in Get-ChildItem -LiteralPath $PartialRoot -Directory -Filter 'partials-*') {
                Assert-Check (-not ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) 'Linked partial directory.'
                foreach ($partial in Get-ChildItem -LiteralPath $directory.FullName -File -Filter '*.partial') {
                    Assert-Check (-not ($partial.Attributes -band [IO.FileAttributes]::ReparsePoint)) 'Linked copy partial.'
                    if ($partial.Length -gt 0 -and $partial.Length -lt $ExpectedBytes) {
                        $observed = $partial.FullName
                        # This Process object owns exactly the child started above.
                        $process.Kill()
                        break
                    }
                }
                if ($observed) { break }
            }
            if ($observed) { break }
            Start-Sleep -Milliseconds 20
        }
        Assert-Check ($null -ne $observed) 'No in-progress partial was caught; try a larger interruption fixture.'
        Assert-Check ($process.WaitForExit(10000)) 'Interrupted child did not exit.'
        Assert-Check ($process.ExitCode -ne 0) 'Child completed before it could be interrupted.'
        $partial = Get-Item -LiteralPath $observed
        Assert-Check ($partial.Length -gt 0 -and $partial.Length -lt $ExpectedBytes) 'Copy finished before interruption.'
        return [pscustomobject]@{
            process_id = $process.Id
            exit_code = $process.ExitCode
            partial = $observed
            partial_bytes = $partial.Length
            partial_sha256 = (Get-FileHash -LiteralPath $observed -Algorithm SHA256).Hash
        }
    } finally {
        try {
            if ($started) {
                if (-not $process.HasExited) { $process.Kill() }
                Assert-Check ($process.WaitForExit(10000)) 'Could not stop the owned test process.'
                [IO.File]::WriteAllText((Join-Path $Reports 'interrupt-process.stdout.log'), $stdout.GetAwaiter().GetResult())
                [IO.File]::WriteAllText((Join-Path $Reports 'interrupt-process.stderr.log'), $stderr.GetAwaiter().GetResult())
            }
        } finally {
            $process.Dispose()
        }
    }
}

function Invoke-ReplicaInterruption {
    param([string]$Exe, [string]$PrimaryRun, [string]$SecondaryRun,
        [string]$State, [string]$Reports, [string]$Cycle, [int]$SizeMiB = 512,
        [bool]$RequireIndependentDrives = $true)
    $source = New-FreshDirectory (Join-Path $PrimaryRun 'interrupt-source')
    $secondary = New-FreshDirectory (Join-Path $SecondaryRun 'interrupt-target')
    $primaryArtifacts = New-FreshDirectory (Join-Path $PrimaryRun 'interrupt-artifacts')
    $secondaryArtifacts = New-FreshDirectory (Join-Path $SecondaryRun 'interrupt-artifacts')
    $bytes = [long]$SizeMiB * 1MB
    $file = Join-Path $source 'synthetic-large.bin'
    $stream = [IO.File]::Open($file, [IO.FileMode]::CreateNew)
    try { $stream.SetLength($bytes) } finally { $stream.Dispose() }
    $before = @(Get-SyntheticSnapshot $source)
    Write-JsonFile (Join-Path $Reports 'interrupt-source-before.json') $before
    $replicaArgs = @('--cycle', $Cycle, '--secondary', $secondary,
        '--primary-artifacts', $primaryArtifacts, '--secondary-artifacts', $secondaryArtifacts)
    if (-not $RequireIndependentDrives) { $replicaArgs += '--allow-unverified-devices' }
    $null = Invoke-IarchiveJson $Exe $State $Reports 'interrupt-import' @('import', '--source', $source, '--cycle', $Cycle)
    $null = Invoke-IarchiveJson $Exe $State $Reports 'interrupt-preview' (@('--dry-run', 'replicate') + $replicaArgs)
    Write-Host "Interrupting only our test child while copying $SizeMiB MiB of generated bytes."
    $stopped = Invoke-InterruptedCopy $Exe (@('--state-dir', $State, '--json', 'replicate') + $replicaArgs) $secondaryArtifacts $bytes $Reports
    Write-JsonFile (Join-Path $Reports 'interrupt-observation.json') $stopped
    Assert-Check (@(Get-ChildItem -LiteralPath $secondary -Force).Count -eq 0) 'Target was published before interruption.'
    $saved = Invoke-IarchiveJson $Exe $State $Reports 'interrupted-status' @('status', '--cycle', $Cycle)
    Assert-Check ($saved.replica_state -eq 'RUNNING' -and $saved.local_replica -eq 'NOT VERIFIED') 'Interrupted copy claimed completion.'
    $resumed = Invoke-IarchiveJson $Exe $State $Reports 'interrupt-resume' (@('replicate') + $replicaArgs)
    Assert-ReplicaComplete $resumed 1
    $verified = Invoke-IarchiveJson $Exe $State $Reports 'interrupt-deep' (@('verify', '--deep') + $replicaArgs)
    Assert-ReplicaComplete $verified 1
    Assert-UnchangedSnapshot $source $before
    Assert-Check ((Get-FileHash -LiteralPath (Join-Path $secondary 'synthetic-large.bin') -Algorithm SHA256).Hash.ToLowerInvariant() -eq $before[0].sha256) 'Resumed bytes differ.'
    Assert-Check ((Get-FileHash -LiteralPath $stopped.partial -Algorithm SHA256).Hash -eq $stopped.partial_sha256) 'Retained partial changed after resume.'
    return [pscustomobject]@{
        status = 'PASSED'
        method = 'Terminate owned process during an incomplete copy'
        fixture_bytes = $bytes
        retained_partial = $stopped.partial
        report = (Join-Path $Reports 'interrupt-deep.json')
    }
}
