#Requires -Version 5.1
<#
.SYNOPSIS
Check metadata groups, quarantine and restore using fresh synthetic files on two drives.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PrimaryDrive,
    [Parameter(Mandatory = $true)][string]$SecondaryDrive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
. (Join-Path $PSScriptRoot 'common.ps1')
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$environment = $null
$runRoot = $null
$oldEncoding = [Console]::OutputEncoding
$oldPythonUtf8 = $env:PYTHONUTF8
Push-Location $repoRoot
try {
    Assert-WindowsHost
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $env:PYTHONUTF8 = '1'
    $environment = Enter-ProjectEnvironment $repoRoot
    $exe = Join-Path $repoRoot 'target\release\iarchive.exe'
    Assert-Check (Test-Path -LiteralPath $exe -PathType Leaf) 'Run setup.ps1 first.'
    $primaryRoot = Get-ExternalDriveRoot $PrimaryDrive
    $secondaryRoot = Get-ExternalDriveRoot $SecondaryDrive
    Assert-Check ($primaryRoot -ne $secondaryRoot) 'Choose two different drive roots.'
    $id = [guid]::NewGuid().ToString('N')
    $runRoot = New-FreshDirectory (Join-Path $repoRoot ".smoke\review-two-drive-$id")
    $reports = New-FreshDirectory (Join-Path $runRoot 'reports')
    $primary = Get-DriveTestDirectory $primaryRoot $id
    $secondaryRun = Get-DriveTestDirectory $secondaryRoot $id
    $source = Join-Path $primary 'media'
    $secondary = New-FreshDirectory (Join-Path $secondaryRun 'media')
    $primaryArtifacts = New-FreshDirectory (Join-Path $primary 'artifacts')
    $secondaryArtifacts = New-FreshDirectory (Join-Path $secondaryRun 'artifacts')
    $state = Join-Path $runRoot 'state'
    $quarantine = Join-Path $primary 'quarantine'
    Invoke-CheckedNative uv @('run', '--locked', '--project', 'image-worker', 'python', 'fixtures/relationships.py', '--output', $source)
    $before = @(Get-SyntheticSnapshot $source)
    $null = Invoke-IarchiveJson $exe $state $reports 'import' @('import', '--cycle', 'synthetic-review', '--source', $source)
    $audit = Invoke-IarchiveJson $exe $state $reports 'audit' @('audit', '--cycle', 'synthetic-review', '--categories', 'dark') @(2)
    Assert-Check ($audit.decoded_images -eq 4 -and $audit.decode_errors -eq 0) 'Synthetic images did not decode.'
    $metadata = Invoke-IarchiveJson $exe $state $reports 'relationships' @('relationships', '--cycle', 'synthetic-review')
    Assert-Check ($metadata.complete -and $metadata.groups.Count -eq 3) 'Metadata relationships were not proved.'
    Assert-UnchangedSnapshot $source $before
    $replicaArgs = @('--cycle', 'synthetic-review', '--secondary', $secondary,
        '--primary-artifacts', $primaryArtifacts, '--secondary-artifacts', $secondaryArtifacts)
    $replica = Invoke-IarchiveJson $exe $state $reports 'replicate' (@('replicate') + $replicaArgs)
    Assert-Check ($replica.complete -and $replica.independent_physical_drives -eq 'VERIFIED BY OS TOPOLOGY') 'Use two independently identified physical drives.'
    $backup = @(Get-SyntheticSnapshot $secondary)
    # Record synthetic notes through the same authenticated API as the gallery.
    $stdout = Join-Path $reports 'review-session.json'
    $stderr = Join-Path $reports 'review-session.stderr'
    $process = $null
    try {
        $start = [Diagnostics.ProcessStartInfo]::new()
        $start.FileName = $exe
        $start.Arguments = '--state-dir "' + $state + '" --json review --cycle synthetic-review'
        $start.UseShellExecute = $false
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $process = [Diagnostics.Process]::new()
        $process.StartInfo = $start
        Assert-Check ($process.Start()) 'Review server did not start.'
        $lineTask = $process.StandardOutput.ReadLineAsync()
        Assert-Check ($lineTask.Wait(15000)) 'Review server timed out.'
        $line = $lineTask.Result
        [IO.File]::WriteAllText($stdout, $line)
        Assert-Check (-not [string]::IsNullOrWhiteSpace($line)) 'Review server emitted no URL.'
        $url = ($line | ConvertFrom-Json).review_url
        $parts = $url.Split('#')
        $base = $parts[0].TrimEnd('/')
        $headers = @{ Authorization = 'Bearer ' + $parts[1]; Origin = $base }
        $view = Invoke-RestMethod -Uri "$base/api/review" -Headers $headers
        foreach ($name in @('live.heic', 'cropped.jpg')) {
            $group = @($view.groups | Where-Object { $_.kind -eq 'dark' -and $_.members.relative_path -contains $name })[0]
            $file = @($group.members | Where-Object { $_.relative_path -eq $name })[0]
            $body = @{ fingerprint = $group.fingerprint; action = 'flag_selected'; selected = @($file.id) } | ConvertTo-Json
            $null = Invoke-RestMethod -Method Post -Uri "$base/api/decision" -Headers $headers -ContentType 'application/json' -Body $body
        }
    } finally {
        if ($process) {
            if (-not $process.HasExited) { $process.Kill() }
            $process.WaitForExit()
            [IO.File]::WriteAllText($stderr, $process.StandardError.ReadToEnd())
            $process.Dispose()
        }
    }
    # The stopped background process can leave committed WAL records to checkpoint.
    $null = Invoke-IarchiveJson $exe $state $reports 'review-status' @('status', '--cycle', 'synthetic-review')
    $preview = Invoke-IarchiveJson $exe $state $reports 'quarantine-preview' @('--dry-run', 'quarantine', '--cycle', 'synthetic-review', '--quarantine-root', $quarantine)
    Assert-Check (-not $preview.blocked -and $preview.files.Count -eq 4) 'Quarantine preview must include four associated files.'
    Assert-UnchangedSnapshot $source $before
    $moved = Invoke-IarchiveJson $exe $state $reports 'quarantine' @('quarantine', '--cycle', 'synthetic-review', '--quarantine-root', $quarantine, '--confirm', $preview.confirmation)
    Assert-Check ($moved.complete -and $moved.file_count -eq 4) 'Quarantine failed.'
    Assert-UnchangedSnapshot $secondary $backup
    $deep = Invoke-IarchiveJson $exe $state $reports 'verify-quarantined' (@('verify', '--deep') + $replicaArgs)
    Assert-Check ($deep.complete -and $deep.verified_files -eq 5) 'Quarantined files were not accounted for.'
    $null = Invoke-IarchiveJson $exe $state $reports 'restore-preview' @('--dry-run', 'restore', '--quarantine-id', $moved.quarantine_id)
    $restored = Invoke-IarchiveJson $exe $state $reports 'restore' @('restore', '--quarantine-id', $moved.quarantine_id)
    Assert-Check $restored.complete 'Restore failed.'
    Assert-UnchangedSnapshot $source $before
    Assert-UnchangedSnapshot $secondary $backup
    $null = Invoke-IarchiveJson $exe $state $reports 'verify-restored' (@('verify', '--deep') + $replicaArgs)
    # A later download resumes in its own cycle without changing earlier archives.
    $oldReport = Invoke-IarchiveJson $exe $state $reports 'earlier-cycle-before' @('report', '--cycle', 'synthetic-review')
    $nextSource = New-FreshDirectory (Join-Path $primary 'next-media')
    $nextSecondary = New-FreshDirectory (Join-Path $secondaryRun 'next-media')
    Copy-Item -LiteralPath (Join-Path $source 'portrait.jpg') -Destination (Join-Path $nextSource 'ready.jpg')
    Copy-Item -LiteralPath (Join-Path $source 'cropped.jpg') -Destination (Join-Path $nextSource 'incoming.jpg.partial')
    $null = Invoke-IarchiveJson $exe $state $reports 'next-import' @('import', '--cycle', 'synthetic-next', '--source', $nextSource)
    $nextAuditArgs = @('audit', '--cycle', 'synthetic-next')
    $interrupted = Invoke-IarchiveJson $exe $state $reports 'next-interrupted' $nextAuditArgs @(2)
    Assert-Check (-not $interrupted.status.inventory_complete -and $interrupted.status.file_errors -eq 1) 'Partial download must block completion.'
    Assert-Check ($interrupted.decoded_images -eq 1 -and $interrupted.status.cloud_coverage -eq 'NOT VERIFIED') 'Interrupted download summary is incorrect.'
    $nextReplicaArgs = @('--cycle', 'synthetic-next', '--secondary', $nextSecondary,
        '--primary-artifacts', $primaryArtifacts, '--secondary-artifacts', $secondaryArtifacts)
    $blocked = Invoke-IarchiveJson $exe $state $reports 'next-partial-preview' (@('--dry-run', 'replicate') + $nextReplicaArgs) @(2)
    Assert-Check (-not $blocked.complete -and @($blocked.issues | Where-Object { $_ -match 'incoming.jpg.partial' }).Count -gt 0) 'Replica must identify the incomplete file.'
    Assert-Check (@(Get-ChildItem -LiteralPath $nextSecondary -Force).Count -eq 0) 'Preview changed secondary media.'
    Move-Item -LiteralPath (Join-Path $nextSource 'incoming.jpg.partial') -Destination (Join-Path $nextSource 'incoming.jpg')
    $nextBefore = @(Get-SyntheticSnapshot $nextSource)
    $recovered = Invoke-IarchiveJson $exe $state $reports 'next-recovered' $nextAuditArgs
    Assert-Check ($recovered.status.inventory_complete -and $recovered.status.missing_files -eq 0 -and $recovered.status.file_errors -eq 0) 'Completed download did not recover.'
    Assert-Check ($recovered.decoded_images -eq 1 -and $recovered.analysis_cache_hits -eq 1 -and
        $recovered.hashes_computed -eq 1 -and $recovered.hash_cache_hits -eq 1) 'Recovery did not reuse completed work.'
    $crossCycle = @($recovered.candidates | Where-Object {
        $_.type -eq 'exact' -and $_.members.cycle -contains 'synthetic-review' -and $_.members.cycle -contains 'synthetic-next'
    })
    Assert-Check ($crossCycle.Count -gt 0 -and $recovered.status.cloud_coverage -eq 'NOT VERIFIED') 'Cross-cycle review or cloud coverage is incorrect.'
    $repeated = Invoke-IarchiveJson $exe $state $reports 'next-repeated' $nextAuditArgs
    Assert-Check ($repeated.decoded_images -eq 0 -and $repeated.analysis_cache_hits -eq 2 -and
        $repeated.hashes_computed -eq 0 -and $repeated.hash_cache_hits -eq 2) 'Unchanged download was needlessly reprocessed.'
    $nextCopy = Invoke-IarchiveJson $exe $state $reports 'next-replicate' (@('replicate') + $nextReplicaArgs)
    Assert-Check ($nextCopy.complete -and $nextCopy.verified_files -eq 2 -and $nextCopy.cloud_coverage -eq 'NOT VERIFIED') 'Recovered download did not replicate safely.'
    $oldAfter = Invoke-IarchiveJson $exe $state $reports 'earlier-cycle-after' @('report', '--cycle', 'synthetic-review')
    Assert-Check ((ConvertTo-Json -InputObject $oldReport -Depth 30 -Compress) -ceq
        (ConvertTo-Json -InputObject $oldAfter -Depth 30 -Compress)) 'Later cycle changed earlier review state.'
    Assert-UnchangedSnapshot $source $before
    Assert-UnchangedSnapshot $secondary $backup
    Assert-UnchangedSnapshot $nextSource $nextBefore
    Write-JsonFile (Join-Path $runRoot 'summary.json') ([pscustomobject]@{
        passed = $true; primary_run = $primary; secondary_run = $secondaryRun
        metadata_groups = 3; quarantined_and_restored_files = 4
        physical_drives = $replica.independent_physical_drives
        cycles = 2; download_recovery = 'PASSED'; unchanged_analysis_cache_hits = 2
    })
    Write-Host "PASS. Results: $runRoot\summary.json"
} catch {
    if ($runRoot) { Write-JsonFile (Join-Path $runRoot 'summary.json') ([pscustomobject]@{ passed = $false; error = $_.Exception.Message }) }
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
} finally {
    Restore-ProjectEnvironment $environment
    [Console]::OutputEncoding = $oldEncoding
    $env:PYTHONUTF8 = $oldPythonUtf8
    Pop-Location
}
exit 0
