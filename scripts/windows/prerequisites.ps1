# Prerequisite discovery and installation. No commands run on dot-source.
function Find-PrerequisiteApplication {
    param([string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    return $null
}

function Invoke-PrerequisiteProbe {
    param([string]$Program, [string[]]$ArgumentList)
    $output = @(& $Program @ArgumentList)
    return [pscustomobject]@{ code = $LASTEXITCODE; text = ($output -join "`n") }
}

function Update-SessionToolPath {
    $paths = @($env:PATH, [Environment]::GetEnvironmentVariable('Path', 'User'),
        [Environment]::GetEnvironmentVariable('Path', 'Machine'))
    if ($env:CARGO_HOME) { $paths += Join-Path $env:CARGO_HOME 'bin' }
    if ($env:USERPROFILE) {
        $paths += Join-Path $env:USERPROFILE '.cargo\bin'
        $paths += Join-Path $env:USERPROFILE '.local\bin'
    }
    if ($env:LOCALAPPDATA) { $paths += Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links' }
    $env:PATH = ($paths | Where-Object { $_ }) -join [IO.Path]::PathSeparator
}

function Get-PrerequisiteVersion {
    param([string]$Name)
    $program = Find-PrerequisiteApplication $Name
    if (-not $program) { return $null }
    $result = Invoke-PrerequisiteProbe $program @('--version')
    if ($result.code -eq 0 -and $result.text -match ('^' + [regex]::Escape($Name) + ' (\d+\.\d+\.\d+)')) {
        return [version]$Matches[1]
    }
    return $null
}

function Get-VswherePath {
    $root = ${env:ProgramFiles(x86)}
    if (-not $root) { return $null }
    $path = Join-Path $root 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
    return $null
}

function Get-MsvcInstallation {
    $vswhere = Get-VswherePath
    if (-not $vswhere) { return $null }
    $result = Invoke-PrerequisiteProbe $vswhere @('-products', '*', '-version', '[16.0,)',
        '-prerelease', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
        '-format', 'json', '-utf8')
    Assert-Check ($result.code -eq 0) 'Visual Studio discovery failed; refusing to assume its tools are missing.'
    foreach ($instance in ($result.text | ConvertFrom-Json)) {
        $versionFile = Join-Path $instance.installationPath 'VC\Auxiliary\Build\Microsoft.VCToolsVersion.default.txt'
        if (-not (Test-Path -LiteralPath $versionFile -PathType Leaf)) { continue }
        $version = [IO.File]::ReadAllText($versionFile).Trim()
        if ($version -notmatch '^\d+\.\d+\.\d+$') { continue }
        $compiler = Join-Path $instance.installationPath "VC\Tools\MSVC\$version\bin\Hostx64\x64"
        if ((Test-Path -LiteralPath (Join-Path $compiler 'cl.exe') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $compiler 'link.exe') -PathType Leaf)) {
            return $instance.installationPath
        }
    }
    return $null
}

function Get-WindowsSdkRoots {
    foreach ($key in @('HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots')) {
        if (Test-Path -LiteralPath $key) {
            $properties = Get-ItemProperty -LiteralPath $key
            $root = $properties.PSObject.Properties['KitsRoot10']
            if ($root -and $root.Value) { $root.Value }
        }
    }
}

function Get-BuildToolsInstance {
    $vswhere = Get-VswherePath
    if (-not $vswhere) { return $null }
    $result = Invoke-PrerequisiteProbe $vswhere @('-latest', '-products', 'Microsoft.VisualStudio.Product.BuildTools',
        '-version', '[17.0,18.0)', '-format', 'json', '-utf8')
    Assert-Check ($result.code -eq 0) 'Build Tools discovery failed.'
    return ($result.text | ConvertFrom-Json | Select-Object -First 1)
}

function Get-WindowsSdkInstallation {
    foreach ($root in (Get-WindowsSdkRoots | Select-Object -Unique)) {
        $lib = Join-Path $root 'Lib'
        if (-not (Test-Path -LiteralPath $lib -PathType Container)) { continue }
        foreach ($version in (Get-ChildItem -LiteralPath $lib -Directory | Sort-Object Name -Descending)) {
            if ($version.Name -notmatch '^10\.0\.\d+\.\d+$') { continue }
            $required = @("Lib\$($version.Name)\um\x64\kernel32.lib",
                "Lib\$($version.Name)\ucrt\x64\ucrt.lib",
                "Include\$($version.Name)\um\Windows.h",
                "Include\$($version.Name)\ucrt\stdio.h",
                "Include\$($version.Name)\shared\sdkddkver.h")
            $missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $root $_) -PathType Leaf) })
            if (-not $missing.Count) { return Join-Path $root "Include\$($version.Name)" }
        }
    }
    return $null
}

function Get-SetupPrerequisites {
    $uvVersion = Get-PrerequisiteVersion uv
    return [pscustomobject]@{
        rust = [bool]((Get-PrerequisiteVersion rustup) -and (Find-PrerequisiteApplication cargo))
        uv = [bool]($uvVersion -and $uvVersion -ge [version]'0.12.3')
        uv_installed = [bool](Find-PrerequisiteApplication uv)
        msvc = Get-MsvcInstallation
        sdk = Get-WindowsSdkInstallation
    }
}

function Install-WindowsPrerequisite {
    param([string]$Name, [bool]$UvInstalled)
    $arguments = @('install', '--exact', '--source', 'winget', '--id')
    switch ($Name) {
        'Rustup and Cargo' { $arguments += 'Rustlang.Rustup' }
        'uv >= 0.12.3' {
            if ($UvInstalled) { $arguments[0] = 'upgrade' }
            $arguments += 'astral-sh.uv'
        }
        'MSVC C++ tools' {
            $instance = Get-BuildToolsInstance
            $options = '--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --norestart'
            if ($instance) {
                # A registered Build Tools package may lack the C++ workload.
                # Invoke its bootstrapper in modify mode instead of installing
                # another instance or relying on WinGet's already-installed exit.
                Assert-Check ($instance.installationPath -notmatch '["\r\n]' -and
                    $instance.channelId -match '^[A-Za-z0-9.]+$') 'Unsafe Visual Studio installation metadata.'
                $options = 'modify --installPath "' + $instance.installationPath + '" --channelId ' + $instance.channelId + ' ' + $options
                $arguments += 'Microsoft.VisualStudio.2022.BuildTools'
                $arguments += @('--force', '--override', $options)
                break
            }
            $arguments += @('Microsoft.VisualStudio.2022.BuildTools', '--override',
                $options)
        }
        'Windows SDK' { $arguments += @('Microsoft.WindowsSDK.10.0.22621', '--custom', '/norestart') }
        default { throw "Unknown prerequisite: $Name" }
    }
    Write-Host "Installing $Name. This can change Windows outside the checkout; installer agreements and elevation remain interactive."
    Invoke-CheckedNative winget $arguments
}

function Get-MissingPrerequisites {
    param($Status)
    if (-not $Status.msvc) { 'MSVC C++ tools' }
    if (-not $Status.sdk) { 'Windows SDK' }
    if (-not $Status.rust) { 'Rustup and Cargo' }
    if (-not $Status.uv) { 'uv >= 0.12.3' }
}

function Initialize-WindowsPrerequisites {
    param([switch]$SkipPrerequisites)
    Update-SessionToolPath
    $status = Get-SetupPrerequisites
    $missing = @(Get-MissingPrerequisites $status)
    if (-not $missing.Count) {
        Write-Host 'All prerequisites are installed and compatible; skipping installation.'
        return
    }
    if ($SkipPrerequisites) {
        throw "Missing or incompatible prerequisites: $($missing -join ', '). Rerun setup without -SkipPrerequisites or install them manually."
    }
    if (-not (Find-PrerequisiteApplication winget)) {
        throw "Missing or incompatible prerequisites: $($missing -join ', '). WinGet is unavailable; install Microsoft's App Installer or use the manual commands in docs/windows-testing.md."
    }
    foreach ($name in $missing) {
        # An earlier installer (notably the C++ workload) may also supply the SDK.
        $status = Get-SetupPrerequisites
        if ($name -notin @(Get-MissingPrerequisites $status)) { continue }
        Install-WindowsPrerequisite $name $status.uv_installed
        Update-SessionToolPath
        $status = Get-SetupPrerequisites
        Assert-Check ($name -notin @(Get-MissingPrerequisites $status)) "$name is still unavailable after installation. Reopen PowerShell and rerun setup; complete any installer restart request first."
    }
    $remaining = @(Get-MissingPrerequisites (Get-SetupPrerequisites))
    Assert-Check (-not $remaining.Count) "Prerequisites remain unavailable: $($remaining -join ', ')."
}
