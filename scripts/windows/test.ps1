#Requires -Version 5.1
<#
.SYNOPSIS
Run quality checks and synthetic CLI tests, optionally on selected drives.
.PARAMETER ExternalDrives
Drive roots only, such as 'D:', 'E:'. Creates fresh iarchive-sandbox\run-* directories
on explicitly selected volumes. No existing archive is scanned or modified.
.PARAMETER SmokeOnly
Reuse a release build after the full checks have already passed. Automated
checks are explicitly marked skipped in the summary.
#>
[CmdletBinding()]
param(
    [string[]]$ExternalDrives = @(),
    [switch]$SmokeOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
. (Join-Path $PSScriptRoot 'common.ps1')
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$oldWorkerRequirement = $env:IARCHIVE_REQUIRE_WORKER_TESTS
$oldConsoleEncoding = [Console]::OutputEncoding
$runRoot = $null
$environment = $null
Push-Location $repoRoot
try {
    Assert-WindowsHost
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $environment = Enter-ProjectEnvironment $repoRoot
    Write-Host "Project tools and caches: $($environment.root)"
    Assert-UvVersion
    $driveRoots = @()
    foreach ($drive in $ExternalDrives) {
        $root = Get-ExternalDriveRoot $drive
        if ($root -notin $driveRoots) { $driveRoots += $root }
    }
    if (-not $SmokeOnly) {
        Require-Application cargo
        Invoke-CheckedNative uv @('sync', '--locked', '--project', 'image-worker', '--group', 'dev', '--python', '3.12')
        Invoke-CheckedNative cargo @('fmt', '--all', '--', '--check')
        Invoke-CheckedNative cargo @('clippy', '--workspace', '--all-targets', '--all-features', '--locked', '--', '-D', 'warnings')
        $env:IARCHIVE_REQUIRE_WORKER_TESTS = '1'
        Invoke-CheckedNative cargo @('test', '--workspace', '--all-features', '--locked')
        Invoke-CheckedNative cargo @('deny', '--locked', '--all-features', 'check')
        Invoke-CheckedNative uv @('run', '--locked', '--project', 'image-worker', '--group', 'dev', 'pytest', 'image-worker/tests')
        Invoke-CheckedNative uv @('run', '--locked', '--project', 'image-worker', 'python', '-m', 'unittest', 'discover', '-s', 'tests', '-v')
        $workerEnvironment = $env:UV_PROJECT_ENVIRONMENT
        try {
            $env:UV_PROJECT_ENVIRONMENT = Assert-SafeDirectory (Join-Path $repoRoot 'tools\cloud-probe\.venv')
            Invoke-CheckedNative uv @('run', '--locked', '--project', 'tools/cloud-probe', '--python', '3.12', 'python', '-m', 'unittest', 'discover', '-s', 'tools/cloud-probe', '-v')
        } finally {
            $env:UV_PROJECT_ENVIRONMENT = $workerEnvironment
        }
        Invoke-CheckedNative cargo @('build', '--release', '--locked')
    } else {
        Write-Host 'SmokeOnly: skipping automated quality checks and build.'
    }
    $exe = Join-Path $repoRoot 'target\release\iarchive.exe'
    Assert-Check (Test-Path -LiteralPath $exe -PathType Leaf) 'Run setup.ps1 or the full test.ps1 first to build iarchive.'
    $runId = [guid]::NewGuid().ToString('N')
    $runRoot = New-FreshDirectory (Join-Path $repoRoot ".smoke\windows-$runId")
    Write-Host "Reports will be retained in $runRoot"
    Test-ScriptGuards $runRoot
    $local = New-FreshDirectory (Join-Path $runRoot 'local smoke test')
    $results = @(Invoke-SyntheticSmoke $exe $local $local)
    foreach ($root in $driveRoots) {
        $mediaParent = Get-DriveTestDirectory $root $runId
        Write-Host "Testing generated media only: $mediaParent"
        $artifacts = New-FreshDirectory (Join-Path $runRoot ("drive-" + $root.Substring(0, 1)))
        $results += Invoke-SyntheticSmoke $exe $mediaParent $artifacts
    }
    Write-JsonFile (Join-Path $runRoot 'summary.json') ([pscustomobject]@{
        passed = $true
        automated_checks = $(if ($SmokeOnly) { 'SKIPPED (SmokeOnly)' } else { 'PASSED' })
        project_tools = $environment.root
        checkout = $repoRoot
        powershell = $PSVersionTable.PSVersion.ToString()
        smoke_tests = $results
    })
    Write-Host "PASS. Results: $runRoot\summary.json"
    Write-Host 'Synthetic files are retained. No archive replication or cloud coverage was verified.'
} catch {
    if ($runRoot) {
        Write-JsonFile (Join-Path $runRoot 'summary.json') ([pscustomobject]@{
            passed = $false
            error = $_.Exception.Message
        })
    }
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
} finally {
    Restore-ProjectEnvironment $environment
    $env:IARCHIVE_REQUIRE_WORKER_TESTS = $oldWorkerRequirement
    [Console]::OutputEncoding = $oldConsoleEncoding
    Pop-Location
}
exit 0
