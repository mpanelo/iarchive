#Requires -Version 5.1
<#
.SYNOPSIS
Walk through inventory, audit, copying, browser review and verification using
fresh synthetic files in the checkout. Requires setup.ps1 first.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
. (Join-Path $PSScriptRoot 'common.ps1')
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$environment = $null
$previousEncoding = [Console]::OutputEncoding
$previousPythonUtf8 = $env:PYTHONUTF8
Push-Location $repoRoot
try {
    Assert-WindowsHost
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $env:PYTHONUTF8 = '1'
    $environment = Enter-ProjectEnvironment $repoRoot
    Assert-UvVersion
    $python = Join-Path $repoRoot 'image-worker\.venv\Scripts\python.exe'
    Assert-Check (Test-Path -LiteralPath $python -PathType Leaf) 'Run setup.ps1 first.'
    # Call directly so the guided prompts and Ctrl+C reach the shared console.
    & $python (Join-Path $repoRoot 'scripts\demo.py')
    if ($LASTEXITCODE -ne 0) { throw "Demo stopped with exit code $LASTEXITCODE." }
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
} finally {
    Restore-ProjectEnvironment $environment
    [Console]::OutputEncoding = $previousEncoding
    $env:PYTHONUTF8 = $previousPythonUtf8
    Pop-Location
}
exit 0
