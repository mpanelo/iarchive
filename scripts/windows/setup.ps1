#Requires -Version 5.1
<#
.SYNOPSIS
Prepare the checkout's Rust/Python tools and build iarchive on Windows.
Requires existing rustup, uv, and MSVC prerequisites. Project tools and caches
stay inside the checkout. No system installers or persistent settings changes.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
. (Join-Path $PSScriptRoot 'common.ps1')
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$environment = $null
Push-Location $repoRoot
try {
    Assert-WindowsHost
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
    Invoke-CheckedNative cargo @('build', '--release', '--locked')
    Write-Host 'Setup complete. Run .\scripts\windows\test.ps1 next.'
} catch {
    Write-Error "$($_.Exception.Message) If linking fails, install Desktop development with C++ and a Windows SDK, then reopen PowerShell." -ErrorAction Continue
    exit 1
} finally {
    Restore-ProjectEnvironment $environment
    Pop-Location
}
exit 0
