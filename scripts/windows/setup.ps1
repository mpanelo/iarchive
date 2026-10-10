#Requires -Version 5.1
<#
.SYNOPSIS
Prepare the checkout's Rust/Python tools and build iarchive on Windows.
.PARAMETER InstallPrerequisites
Use WinGet to install missing Rust, uv, and Visual Studio C++ build tools.
Installer agreements and elevation remain interactive; a restart may be required.
#>
[CmdletBinding()]
param([switch]$InstallPrerequisites)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
. (Join-Path $PSScriptRoot 'common.ps1')
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$previousPath = $env:PATH
Push-Location $repoRoot
try {
    Assert-WindowsHost
    if ($InstallPrerequisites) {
        Require-Application winget
        $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
        $cpp = @()
        if (Test-Path -LiteralPath $vswhere) {
            $cpp = @(& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath)
            Assert-Check ($LASTEXITCODE -eq 0) 'Visual Studio discovery failed.'
        }
        if (-not $cpp.Count) {
            Invoke-CheckedNative winget @('install', '--exact', '--id', 'Microsoft.VisualStudio.2022.BuildTools',
                '--source', 'winget', '--override', '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --norestart')
        }
        foreach ($tool in @(@('rustup', 'Rustlang.Rustup'), @('uv', 'astral-sh.uv'))) {
            if (-not (Get-Command $tool[0] -CommandType Application -ErrorAction SilentlyContinue)) {
                Invoke-CheckedNative winget @('install', '--exact', '--id', $tool[1], '--source', 'winget')
            }
        }
        $env:PATH = @($previousPath, [Environment]::GetEnvironmentVariable('Path', 'User'),
            [Environment]::GetEnvironmentVariable('Path', 'Machine'),
            (Join-Path $env:USERPROFILE '.cargo\bin'), (Join-Path $env:USERPROFILE '.local\bin')) -join ';'
    }
    foreach ($tool in @('rustup', 'rustc', 'cargo', 'uv')) { Require-Application $tool }
    $compiler = @(& rustc -vV) -join "`n"
    Assert-Check ($LASTEXITCODE -eq 0 -and $compiler -match 'host: x86_64-pc-windows-msvc') 'Use the Windows x64 MSVC Rust toolchain installed by rustup.'
    Invoke-CheckedNative rustup @('component', 'add', 'rustfmt', 'clippy')
    $cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE '.cargo' }
    $deny = Join-Path $cargoHome 'bin\cargo-deny.exe'
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
    $env:PATH = $previousPath
    Pop-Location
}
exit 0
