#Requires -Version 5.1
# Synthetic discovery and installer tests; never invoke a real installer.
[CmdletBinding()]
param([switch]$CheckInstalled)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$windowsScripts = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $windowsScripts 'common.ps1')
$repo = [IO.Path]::GetFullPath((Join-Path $windowsScripts '..\..'))
$fixtures = New-FreshDirectory (Join-Path $repo ('.smoke\prerequisite-tests-' + [guid]::NewGuid().ToString('N')))
$passed = 0

function Test-Case {
    param([string]$Name, [scriptblock]$Body)
    & {
        . (Join-Path $windowsScripts 'common.ps1')
        . (Join-Path $windowsScripts 'prerequisites.ps1')
        & $Body
    }
    Write-Host "PASS: $Name"
    $script:passed++
}

foreach ($path in Get-ChildItem -LiteralPath $windowsScripts -Filter '*.ps1' -Recurse) {
    $tokens = $null; $errors = $null
    $null = [Management.Automation.Language.Parser]::ParseFile($path.FullName, [ref]$tokens, [ref]$errors)
    Assert-Check ($errors.Count -eq 0) ($errors | Out-String)
}

Test-Case 'Version probes distinguish compatible, old, broken, and absent uv' {
    function Find-PrerequisiteApplication { param($Name) if ($present) { 'synthetic-uv' } }
    function Invoke-PrerequisiteProbe { param($Program, $ArgumentList) $response }
    $present = $true
    $response = [pscustomobject]@{ code = 0; text = 'uv 0.13.0 (synthetic)' }
    Assert-Check ((Get-PrerequisiteVersion uv) -ge [version]'0.12.3') 'Compatible uv rejected.'
    $response.text = 'uv 0.11.0'
    Assert-Check ((Get-PrerequisiteVersion uv) -lt [version]'0.12.3') 'Old uv accepted.'
    $response.code = 7
    Assert-Check ($null -eq (Get-PrerequisiteVersion uv)) 'Broken uv accepted.'
    $response.code = 0; $response.text = 'not a version'
    Assert-Check ($null -eq (Get-PrerequisiteVersion uv)) 'Unparseable uv accepted.'
    $present = $false
    Assert-Check ($null -eq (Get-PrerequisiteVersion uv)) 'Absent uv accepted.'
}

Test-Case 'MSVC discovery reuses Community and rejects missing x64 link tools' {
    $vsRoot = New-FreshDirectory (Join-Path $fixtures 'Visual Studio Community')
    $version = '14.40.33807'
    $aux = New-FreshDirectory (Join-Path $vsRoot 'VC\Auxiliary\Build')
    [IO.File]::WriteAllText((Join-Path $aux 'Microsoft.VCToolsVersion.default.txt'), $version)
    $bin = New-FreshDirectory (Join-Path $vsRoot "VC\Tools\MSVC\$version\bin\Hostx64\x64")
    [IO.File]::WriteAllText((Join-Path $bin 'cl.exe'), 'synthetic compiler, never executed')
    function Get-VswherePath { 'synthetic-vswhere' }
    function Invoke-PrerequisiteProbe {
        param($Program, $ArgumentList)
        Assert-Check ('*' -in $ArgumentList) 'Discovery excluded full Visual Studio editions.'
        return [pscustomobject]@{ code = 0; text = (ConvertTo-Json -InputObject @(@{ installationPath = $vsRoot })) }
    }
    Assert-Check ($null -eq (Get-MsvcInstallation)) 'MSVC without x64 linker accepted.'
    [IO.File]::WriteAllText((Join-Path $bin 'link.exe'), 'synthetic linker, never executed')
    Assert-Check ((Get-MsvcInstallation) -eq $vsRoot) 'Community C++ tools were not reused.'
}

Test-Case 'Discovery failures do not trigger a new Visual Studio installation' {
    function Get-VswherePath { 'synthetic-vswhere' }
    function Invoke-PrerequisiteProbe { param($Program, $ArgumentList) [pscustomobject]@{ code = 9; text = '' } }
    $refused = $false
    try { $null = Get-MsvcInstallation } catch { $refused = $_.Exception.Message -match 'discovery failed' }
    Assert-Check $refused 'Failed discovery was treated as missing software.'
}

Test-Case 'Build Tools discovery handles empty and registered installations' {
    function Get-VswherePath { 'synthetic-vswhere' }
    function Invoke-PrerequisiteProbe {
        param($Program, $ArgumentList)
        return [pscustomobject]@{ code = 0; text = $json }
    }
    $json = '[]'
    Assert-Check ($null -eq (Get-BuildToolsInstance)) 'Absent Build Tools were detected.'
    $json = '[{"installationPath":"C:\\Synthetic Build Tools","channelId":"VisualStudio.17.Release"}]'
    $instance = Get-BuildToolsInstance
    Assert-Check ($instance.installationPath -eq 'C:\Synthetic Build Tools' -and $instance.channelId -eq 'VisualStudio.17.Release') 'Build Tools instance metadata was lost.'
}

Test-Case 'SDK detection requires headers and both x64 libraries' {
    $sdkRoot = New-FreshDirectory (Join-Path $fixtures 'Windows Kits')
    function Get-WindowsSdkRoots { $sdkRoot }
    $version = '10.0.22621.0'
    $required = @("Lib\$version\um\x64\kernel32.lib", "Lib\$version\ucrt\x64\ucrt.lib",
        "Include\$version\um\Windows.h", "Include\$version\ucrt\stdio.h", "Include\$version\shared\sdkddkver.h")
    foreach ($relative in $required[0..3]) {
        $path = Join-Path $sdkRoot $relative
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($path)) -Force | Out-Null
        [IO.File]::WriteAllText($path, 'synthetic SDK file')
    }
    Assert-Check ($null -eq (Get-WindowsSdkInstallation)) 'Incomplete SDK accepted.'
    $last = Join-Path $sdkRoot $required[4]
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($last)) -Force | Out-Null
    [IO.File]::WriteAllText($last, 'synthetic SDK file')
    Assert-Check ((Get-WindowsSdkInstallation) -eq (Join-Path $sdkRoot "Include\$version")) 'Complete SDK was not reused.'
}

Test-Case 'Complete prerequisites skip all installers and do not require WinGet' {
    function Update-SessionToolPath {}
    function Get-SetupPrerequisites { [pscustomobject]@{ rust = $true; uv = $true; uv_installed = $true; msvc = 'Community'; sdk = 'SDK' } }
    function Find-PrerequisiteApplication { throw 'WinGet discovery must not run.' }
    function Install-WindowsPrerequisite { throw 'Installer must not run.' }
    Initialize-WindowsPrerequisites
    Initialize-WindowsPrerequisites -SkipPrerequisites
}

Test-Case 'A missing SDK installs only the SDK and reruns skip installation' {
    $script:sdkInstalled = $false
    $script:installCount = 0
    function Update-SessionToolPath {}
    function Get-SetupPrerequisites {
        [pscustomobject]@{ rust = $true; uv = $true; uv_installed = $true; msvc = 'Enterprise'; sdk = $(if ($script:sdkInstalled) { 'SDK' } else { $null }) }
    }
    function Find-PrerequisiteApplication { param($Name) 'synthetic-winget' }
    function Install-WindowsPrerequisite {
        param($Name, $UvInstalled)
        Assert-Check ($Name -eq 'Windows SDK') 'An installed prerequisite was changed.'
        $script:sdkInstalled = $true; $script:installCount++
    }
    Initialize-WindowsPrerequisites
    Initialize-WindowsPrerequisites
    Assert-Check ($script:installCount -eq 1) 'SDK was installed more than once.'
}

Test-Case 'C++ installation supplying an SDK avoids a second SDK installer' {
    $script:cppInstalled = $false
    $script:installCount = 0
    function Update-SessionToolPath {}
    function Get-SetupPrerequisites {
        [pscustomobject]@{ rust = $true; uv = $true; uv_installed = $true;
            msvc = $(if ($script:cppInstalled) { 'MSVC' } else { $null }); sdk = $(if ($script:cppInstalled) { 'SDK' } else { $null }) }
    }
    function Find-PrerequisiteApplication { param($Name) 'synthetic-winget' }
    function Install-WindowsPrerequisite {
        param($Name, $UvInstalled)
        Assert-Check ($Name -eq 'MSVC C++ tools') 'Redundant SDK installation attempted.'
        $script:cppInstalled = $true; $script:installCount++
    }
    Initialize-WindowsPrerequisites
    Assert-Check ($script:installCount -eq 1) 'Redundant installation occurred.'
}

Test-Case 'Old uv is upgraded without touching installed Rust or C++ tools' {
    $script:uvUpgraded = $false
    function Update-SessionToolPath {}
    function Get-SetupPrerequisites { [pscustomobject]@{ rust = $true; uv = $script:uvUpgraded; uv_installed = $true; msvc = 'MSVC'; sdk = 'SDK' } }
    function Find-PrerequisiteApplication { param($Name) 'synthetic-winget' }
    function Invoke-CheckedNative {
        param($Program, $ArgumentList)
        Assert-Check ($Program -eq 'winget' -and $ArgumentList[0] -eq 'upgrade' -and 'astral-sh.uv' -in $ArgumentList) 'Old uv was not upgraded selectively.'
        $script:uvUpgraded = $true
    }
    Initialize-WindowsPrerequisites
}

Test-Case 'SkipPrerequisites reports missing tools without invoking installers' {
    function Update-SessionToolPath {}
    function Get-SetupPrerequisites { [pscustomobject]@{ rust = $false; uv = $true; uv_installed = $true; msvc = 'MSVC'; sdk = 'SDK' } }
    function Find-PrerequisiteApplication { throw 'WinGet must not run in check-only mode.' }
    function Install-WindowsPrerequisite { throw 'Installer must not run in check-only mode.' }
    $refused = $false
    try { Initialize-WindowsPrerequisites -SkipPrerequisites } catch { $refused = $_.Exception.Message -match 'Rustup and Cargo' }
    Assert-Check $refused 'Missing Rust was not reported.'
}

Test-Case 'Missing WinGet reports prerequisites before attempting installation' {
    function Update-SessionToolPath {}
    function Get-SetupPrerequisites { [pscustomobject]@{ rust = $false; uv = $true; uv_installed = $true; msvc = 'MSVC'; sdk = 'SDK' } }
    function Find-PrerequisiteApplication { param($Name) $null }
    function Install-WindowsPrerequisite { throw 'Installer must not run without WinGet.' }
    $refused = $false
    try { Initialize-WindowsPrerequisites } catch { $refused = $_.Exception.Message -match 'WinGet is unavailable' }
    Assert-Check $refused 'Missing WinGet did not produce instructions.'
}

Test-Case 'Failed installers and unchanged detection stop setup' {
    function Update-SessionToolPath {}
    function Get-SetupPrerequisites { [pscustomobject]@{ rust = $false; uv = $true; uv_installed = $true; msvc = 'MSVC'; sdk = 'SDK' } }
    function Find-PrerequisiteApplication { param($Name) 'synthetic-winget' }
    function Install-WindowsPrerequisite { param($Name, $UvInstalled) throw 'Synthetic installer failure' }
    $refused = $false
    try { Initialize-WindowsPrerequisites } catch { $refused = $_.Exception.Message -eq 'Synthetic installer failure' }
    Assert-Check $refused 'Installer error was ignored.'
    function Install-WindowsPrerequisite { param($Name, $UvInstalled) }
    $refused = $false
    try { Initialize-WindowsPrerequisites } catch { $refused = $_.Exception.Message -match 'still unavailable' }
    Assert-Check $refused 'Installer success without usable tools was accepted.'
}

Test-Case 'Installer commands use exact packages and prevent forced reboots' {
    function Get-BuildToolsInstance { $null }
    function Invoke-CheckedNative {
        param($Program, $ArgumentList)
        Assert-Check ($Program -eq 'winget' -and '--exact' -in $ArgumentList -and 'winget' -in $ArgumentList) 'Ambiguous installer source or package.'
        Assert-Check ('--accept-package-agreements' -notin $ArgumentList -and '--accept-source-agreements' -notin $ArgumentList) 'Agreements were accepted silently.'
        $script:lastArguments = $ArgumentList
    }
    Install-WindowsPrerequisite 'MSVC C++ tools' $false
    Assert-Check ($script:lastArguments[-1] -match '--norestart' -and $script:lastArguments[-1] -match 'Workload.VCTools') 'Missing C++ workload or reboot suppression.'
    Install-WindowsPrerequisite 'Windows SDK' $false
    Assert-Check ('Microsoft.WindowsSDK.10.0.22621' -in $script:lastArguments -and '/norestart' -in $script:lastArguments) 'SDK command is incorrect.'
    Install-WindowsPrerequisite 'Rustup and Cargo' $false
    Assert-Check ('Rustlang.Rustup' -in $script:lastArguments) 'Rustup command is incorrect.'
    Install-WindowsPrerequisite 'uv >= 0.12.3' $false
    Assert-Check ($script:lastArguments[0] -eq 'install' -and 'astral-sh.uv' -in $script:lastArguments) 'Missing uv command is incorrect.'
    function Get-BuildToolsInstance { [pscustomobject]@{ installationPath = 'C:\Synthetic Build Tools'; channelId = 'VisualStudio.17.Release' } }
    Install-WindowsPrerequisite 'MSVC C++ tools' $false
    Assert-Check ('--force' -in $script:lastArguments -and $script:lastArguments[-1] -match '^modify --installPath "C:\\Synthetic Build Tools"') 'Existing Build Tools without C++ were not modified.'
}

Write-Host "PASS: $passed prerequisite test cases. Synthetic fixtures retained in $fixtures"

if ($CheckInstalled) {
    Assert-WindowsHost
    . (Join-Path $windowsScripts 'prerequisites.ps1')
    $previousPath = $env:PATH
    try {
        Update-SessionToolPath
        $missing = @(Get-MissingPrerequisites (Get-SetupPrerequisites))
        Assert-Check (-not $missing.Count) "Preconfigured runner prerequisites were not discovered: $($missing -join ', ')."
        Initialize-WindowsPrerequisites -SkipPrerequisites
        Write-Host 'PASS: real installed prerequisites discovered without running installers.'
    } finally { $env:PATH = $previousPath }
}
