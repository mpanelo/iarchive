#Requires -Version 5.1
<#
.SYNOPSIS
Interrupt and resume a synthetic cross-drive copy, then deeply verify it.
.PARAMETER PrimaryDrive
Writable drive root, such as D:. Only fresh marked test directories are used.
.PARAMETER SecondaryDrive
Another drive root, such as E:. Windows must report a distinct backing disk.
.PARAMETER InterruptionMiB
Size of the generated binary file; increase if the copy is too fast.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PrimaryDrive,
    [Parameter(Mandatory = $true)][string]$SecondaryDrive,
    [ValidateRange(64, 2048)][int]$InterruptionMiB = 512
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
. (Join-Path $PSScriptRoot 'common.ps1')
. (Join-Path $PSScriptRoot 'replica-testing.ps1')
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$environment = $null
$runRoot = $null
$oldConsoleEncoding = [Console]::OutputEncoding
$summary = [ordered]@{
    passed = $false
    checkout = $repoRoot
    commit = $null
    powershell = $PSVersionTable.PSVersion.ToString()
    primary_run = $null
    secondary_run = $null
    interruption = [pscustomobject]@{ status = 'FAILED'; fixture_mib = $InterruptionMiB }
}
Push-Location $repoRoot
try {
    Assert-WindowsHost
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $environment = Enter-ProjectEnvironment $repoRoot
    $exe = Join-Path $repoRoot 'target\release\iarchive.exe'
    Assert-Check (Test-Path -LiteralPath $exe -PathType Leaf) 'Run setup.ps1 and the full test.ps1 first.'
    $primaryRoot = Get-ExternalDriveRoot $PrimaryDrive
    $secondaryRoot = Get-ExternalDriveRoot $SecondaryDrive
    Assert-Check ($primaryRoot -ne $secondaryRoot) 'Choose two different drive roots.'
    $runId = [guid]::NewGuid().ToString('N')
    $runRoot = New-FreshDirectory (Join-Path $repoRoot ".smoke\two-drive-interruption-$runId")
    $reports = New-FreshDirectory (Join-Path $runRoot 'reports')
    Write-Host "Reports will be retained in $runRoot"
    if (Get-Command git -CommandType Application -ErrorAction SilentlyContinue) {
        $commit = @(& git rev-parse HEAD) -join ''
        if ($LASTEXITCODE -eq 0) { $summary.commit = $commit }
    }
    $primaryRun = Get-DriveTestDirectory $primaryRoot $runId
    $summary.primary_run = $primaryRun
    $secondaryRun = Get-DriveTestDirectory $secondaryRoot $runId
    $summary.secondary_run = $secondaryRun
    $state = Join-Path $runRoot 'state'
    Write-Host "Generated primary run: $primaryRun"
    Write-Host "Generated secondary run: $secondaryRun"
    $summary.interruption = Invoke-ReplicaInterruption $exe $primaryRun $secondaryRun $state $reports "interrupt-$runId" $InterruptionMiB
    $summary.passed = $true
    Write-JsonFile (Join-Path $runRoot 'summary.json') $summary
    Write-Host "PASS. Results: $runRoot\summary.json"
} catch {
    $summary['error'] = $_.Exception.Message
    if ($runRoot) { Write-JsonFile (Join-Path $runRoot 'summary.json') $summary }
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
} finally {
    Restore-ProjectEnvironment $environment
    [Console]::OutputEncoding = $oldConsoleEncoding
    Pop-Location
}
exit 0
