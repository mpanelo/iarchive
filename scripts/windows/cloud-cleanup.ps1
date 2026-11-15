#Requires -Version 5.1
<#
.SYNOPSIS
Guide a saved review through verified-drive cleanup, or read-only recovery.
Account access and removal each require an explicit y/N confirmation.
#>
[CmdletBinding()]
param([switch]$Recover, [string]$Draft, [string]$Album, [string]$Run)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
. (Join-Path $PSScriptRoot 'common.ps1')
. (Join-Path $PSScriptRoot 'cloud-cleanup-common.ps1')
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
try {
    Invoke-CloudCleanup -RepoRoot $repoRoot -Recover:$Recover -Draft $Draft -Album $Album -Run $Run
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
exit 0
