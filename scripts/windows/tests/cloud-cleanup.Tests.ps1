#Requires -Version 5.1
# Exercise launcher scoping and argument forwarding. No Python or installers run.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\common.ps1')
. (Join-Path $PSScriptRoot '..\cloud-cleanup-common.ps1')
Assert-WindowsHost
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$runRoot = New-FreshDirectory (Join-Path $repoRoot ('.smoke\cloud-launcher-tests-' + [guid]::NewGuid().ToString('N')))
$fixture = New-FreshDirectory (Join-Path $runRoot 'checkout with spaces')
$script:expectedEnvironment = Join-Path $fixture 'tools\cloud-probe\.venv'
$script:captured = @()
$script:failCommand = $false
function Assert-UvVersion { }
function Invoke-CloudPython {
    param([string[]]$ArgumentList)
    Assert-Check ($env:UV_PROJECT_ENVIRONMENT -eq $script:expectedEnvironment) 'Wrong cloud environment.'
    Assert-Check ($env:UV_CACHE_DIR -eq (Join-Path $fixture '.windows-sandbox\uv-cache')) 'Cache escaped the checkout.'
    Assert-Check ($env:PYTHONUTF8 -eq '1') 'Python UTF-8 mode was not set.'
    Assert-Check ($PWD.Path -eq $fixture) 'Launcher did not enter the checkout.'
    $script:captured = @($ArgumentList)
    if ($script:failCommand) { throw 'Synthetic Python failure.' }
}

$before = @{}
foreach ($name in @('CARGO_HOME', 'RUSTUP_HOME', 'UV_CACHE_DIR', 'UV_PROJECT_ENVIRONMENT', 'PYTHONUTF8', 'TEMP', 'PATH')) {
    $before[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$oldLocation = $PWD.Path
$oldEncoding = [Console]::OutputEncoding.CodePage
$draft = 'D:\synthetic artifacts\draft.json'
$attempt = 'E:\synthetic artifacts\cleanup\run'
foreach ($mode in @('cleanup', 'recovery', 'failure')) {
    $script:failCommand = $mode -eq 'failure'
    $failed = $false
    try {
        if ($mode -eq 'recovery') {
            Invoke-CloudCleanup -RepoRoot $fixture -Recover -Run $attempt
        } else {
            Invoke-CloudCleanup -RepoRoot $fixture -Draft $draft -Album 'Synthetic test album'
        }
    } catch {
        if ($_.Exception.Message -ne 'Synthetic Python failure.') { throw }
        $failed = $true
    }
    Assert-Check ($failed -eq $script:failCommand) 'Wrong launcher failure result.'
    Assert-Check ($script:captured -contains '--locked') 'Dependency lock was not enforced.'
    Assert-Check ($script:captured -contains 'tools/cloud-cleanup/run.py') 'Wrong Python entry point.'
    if ($mode -eq 'recovery') {
        Assert-Check ($script:captured -contains '--recover' -and $script:captured -contains $attempt) 'Recovery arguments changed.'
        Assert-Check ($script:captured -notcontains '--iarchive' -and $script:captured -notcontains '--album') 'Recovery included cleanup-only options.'
    } else {
        Assert-Check ($script:captured -contains $draft -and $script:captured -contains 'Synthetic test album') 'Spaced arguments were split.'
        Assert-Check ($script:captured -contains (Join-Path $fixture 'target\release\iarchive.exe')) 'Wrong verifier executable.'
    }
    foreach ($name in $before.Keys) {
        Assert-Check ([Environment]::GetEnvironmentVariable($name, 'Process') -ceq $before[$name]) "Environment was not restored: $name"
    }
    Assert-Check ($PWD.Path -eq $oldLocation -and [Console]::OutputEncoding.CodePage -eq $oldEncoding) 'Console or location was not restored.'
}
foreach ($invalid in @(@{ Recover = $true; Draft = $draft }, @{ Run = $attempt })) {
    $rejected = $false
    try { Invoke-CloudCleanup -RepoRoot $fixture @invalid } catch { $rejected = $true }
    Assert-Check $rejected 'Invalid mode combination was accepted.'
}
Write-Host 'PASS: cloud launcher scoping, arguments and failure recovery. No accounts accessed.'
