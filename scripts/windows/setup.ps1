#Requires -Version 5.1
<#
.SYNOPSIS
Prepare the checkout's Rust/Python tools and build iarchive on Windows.
Detect and reuse installed prerequisites; install missing tools through WinGet.
Prerequisite installers can change Windows outside the checkout. Project tools
and caches stay inside the checkout.
.PARAMETER SkipPrerequisites
Check prerequisites without installing or upgrading any of them.
#>
[CmdletBinding()]
param([switch]$SkipPrerequisites)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
. (Join-Path $PSScriptRoot 'common.ps1')
. (Join-Path $PSScriptRoot 'prerequisites.ps1')
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$environment = $null
$previousPath = $env:PATH
$previousEncoding = [Console]::OutputEncoding
Push-Location $repoRoot
try {
    Assert-WindowsHost
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    Initialize-WindowsPrerequisites -SkipPrerequisites:$SkipPrerequisites
    foreach ($tool in @('rustup', 'cargo', 'uv')) { Require-Application $tool }
    Assert-UvVersion
    $environment = Enter-ProjectEnvironment $repoRoot
    Write-Host "Project tools and caches: $($environment.root)"
    Invoke-CheckedNative rustup @('toolchain', 'install', $env:RUSTUP_TOOLCHAIN,
        '--profile', 'minimal', '--component', 'rustfmt', '--component', 'clippy',
        '--no-self-update', '--no-update')
    $deny = Join-Path $env:CARGO_HOME 'bin\cargo-deny.exe'
    if (-not (Test-Path -LiteralPath $deny)) {
        Invoke-CheckedNative cargo @('install', '--locked', 'cargo-deny', '--version', '0.20.2')
    }
    Invoke-CheckedNative cargo @('deny', '--version')
    Invoke-CheckedNative uv @('sync', '--locked', '--project', 'image-worker', '--group', 'dev', '--python', '3.12')
    Invoke-CheckedNative uv @('run', '--locked', '--project', 'image-worker', 'python', 'scripts/setup_metadata.py')
    Invoke-CheckedNative cargo @('build', '--release', '--locked')
    Write-Host 'Setup complete. Run .\scripts\windows\test.ps1 next.'
} catch {
    Write-Error "$($_.Exception.Message) If linking fails, install Desktop development with C++ and a Windows SDK, then reopen PowerShell." -ErrorAction Continue
    exit 1
} finally {
    Restore-ProjectEnvironment $environment
    $env:PATH = $previousPath
    [Console]::OutputEncoding = $previousEncoding
    Pop-Location
}
exit 0
