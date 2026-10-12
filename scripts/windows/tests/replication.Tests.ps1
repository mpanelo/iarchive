#Requires -Version 5.1
<# Synthetic workflow tests on one volume; no physical-drive claim. #>
[CmdletBinding()]
param([string]$Exe)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$windowsScripts = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$repo = [IO.Path]::GetFullPath((Join-Path $windowsScripts '..\..'))
. (Join-Path $windowsScripts 'common.ps1')
. (Join-Path $windowsScripts 'replica-testing.ps1')
if (-not $Exe) { $Exe = Join-Path $repo 'target\release\iarchive.exe' }
$Exe = [IO.Path]::GetFullPath($Exe)
Assert-Check (Test-Path -LiteralPath $Exe -PathType Leaf) 'Build iarchive before running these tests.'
$fixtures = New-FreshDirectory (Join-Path $repo ('.smoke\replica-script-tests-' + [guid]::NewGuid().ToString('N')))
$passed = 0
$oldConsoleEncoding = [Console]::OutputEncoding

function Test-Case {
    param([string]$Name, [scriptblock]$Body)
    & $Body
    Write-Host "PASS: $Name"
    $script:passed++
}

function New-TestLayout {
    param([string]$Name)
    $root = New-FreshDirectory (Join-Path $fixtures $Name)
    $layout = @{ state = (Join-Path $root 'state') }
    foreach ($name in @('source', 'secondary', 'primary-artifacts', 'secondary-artifacts', 'reports')) {
        $layout[$name] = New-FreshDirectory (Join-Path $root $name)
    }
    $photo = 'scene ' + [char]0x2603 + '.heic'
    [IO.File]::WriteAllBytes((Join-Path $layout.source $photo), [byte[]]@(1, 2, 3))
    [IO.File]::WriteAllText((Join-Path $layout.source 'scene.aae'), '<synthetic>edit</synthetic>')
    [IO.File]::WriteAllBytes((Join-Path $layout.source 'empty.xyz'), [byte[]]@())
    return $layout
}

try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    foreach ($path in Get-ChildItem -LiteralPath $windowsScripts -Filter '*.ps1' -Recurse) {
        $tokens = $null; $errors = $null
        $null = [Management.Automation.Language.Parser]::ParseFile($path.FullName, [ref]$tokens, [ref]$errors)
        Assert-Check ($errors.Count -eq 0) ($errors | Out-String)
    }
    Test-Case 'Replica acceptance workflow' {
        $layout = New-TestLayout 'workflow with spaces'
        $result = Invoke-ReplicaAcceptance $Exe $layout.source $layout.secondary $layout['primary-artifacts'] $layout['secondary-artifacts'] $layout.state $layout.reports 'synthetic' -RequireIndependentDrives $false
        Assert-Check ($result.passed -and $result.files -eq 3 -and $result.recovery_backups -eq 2) 'Workflow did not cover both recoveries.'
        Assert-Check ($result.independent_physical_drives -eq 'NOT VERIFIED') 'Same-volume workflow claimed physical independence.'
        Assert-Check (-not $result.restored_identity_changed -and $result.final_cycle -eq 'synthetic') 'Stable root unexpectedly needed a new cycle.'
    }
    Test-Case 'Restored root identity changes' {
        $layout = New-TestLayout 'changed identity'
        # Retain the renamed directory and restore its bytes into a new directory,
        # forcing a new file ID even on NTFS without relying on exFAT in CI.
        function Move-Item {
            param([string]$LiteralPath, [string]$Destination)
            if ([IO.Path]::GetFileName($LiteralPath) -eq 'temporarily-unavailable') {
                $null = New-FreshDirectory $Destination
                Get-ChildItem -LiteralPath $LiteralPath -Force | Microsoft.PowerShell.Management\Copy-Item -Destination $Destination -Recurse
            } else {
                Microsoft.PowerShell.Management\Move-Item -LiteralPath $LiteralPath -Destination $Destination
            }
        }
        $result = Invoke-ReplicaAcceptance $Exe $layout.source $layout.secondary $layout['primary-artifacts'] $layout['secondary-artifacts'] $layout.state $layout.reports 'synthetic' -RequireIndependentDrives $false
        Assert-Check ($result.passed -and $result.restored_identity_changed -and $result.final_cycle -eq 'synthetic-restored') 'Changed identity did not require a fresh cycle.'
        $old = Get-Content -LiteralPath (Join-Path $layout.reports 'restored-verify.json') -Raw | ConvertFrom-Json
        Assert-Check (-not $old.complete -and $old.verified_files -eq 0) 'Old binding accepted the replacement root.'
        $final = Get-Content -LiteralPath $result.final_report -Raw | ConvertFrom-Json
        Assert-ReplicaComplete $final 3
        Assert-Check ($final.copied_files -eq 0 -and $final.cycle -eq 'synthetic-restored') 'Fresh verification copied media or reused the old cycle.'
    }
    Test-Case 'Same-volume topology refusal' {
        $layout = New-TestLayout 'topology'
        $refused = $false
        try {
            $null = Invoke-ReplicaAcceptance $Exe $layout.source $layout.secondary $layout['primary-artifacts'] $layout['secondary-artifacts'] $layout.state $layout.reports 'synthetic'
        } catch {
            $refused = $_.Exception.Message -match 'preview returned 2; expected 0'
        }
        Assert-Check $refused 'Physical-drive guard did not stop acceptance.'
        $preview = Get-Content -LiteralPath (Join-Path $layout.reports 'preview.json') -Raw | ConvertFrom-Json
        Assert-Check (@($preview.issues | Where-Object { $_ -match 'physical drive independence is NOT VERIFIED' }).Count -gt 0) 'Another failure masked the topology guard.'
        Assert-Check (@(Get-ChildItem -LiteralPath $layout.secondary -Force).Count -eq 0) 'Topology refusal copied media.'
    }
    Test-Case 'Interruption entry point guards' {
        $shell = (Get-Process -Id $PID).Path
        $entry = Join-Path $windowsScripts 'test-two-drive-interruption.ps1'
        foreach ($case in @(
            @{ args = @('-PrimaryDrive', 'C:', '-SecondaryDrive', 'C:'); error = 'Choose two different drive roots' },
            @{ args = @('-PrimaryDrive', 'C:', '-SecondaryDrive', 'C:', '-InterruptionMiB', '1'); error = '64' }
        )) {
            $process = New-ReplicaProcess $shell (@('-NoProfile', '-ExecutionPolicy', 'RemoteSigned', '-File', $entry) + $case.args)
            try {
                Assert-Check ($process.Start()) 'Could not launch interruption entry point.'
                $stdout = $process.StandardOutput.ReadToEndAsync()
                $stderr = $process.StandardError.ReadToEndAsync()
                Assert-Check ($process.WaitForExit(30000)) 'Entry point guard timed out.'
                $output = $stdout.GetAwaiter().GetResult() + $stderr.GetAwaiter().GetResult()
                Assert-Check ($process.ExitCode -ne 0 -and $output -match $case.error) 'Unrelated failure masked the entry point guard.'
                Assert-Check ($output -notmatch 'Reports will be retained|Interrupting only') 'Rejected arguments started a test run.'
            } finally {
                if (-not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(10000) }
                $process.Dispose()
            }
        }
    }
    Test-Case 'Interrupt only owned child' {
        $root = New-FreshDirectory (Join-Path $fixtures 'process with spaces')
        $partials = New-FreshDirectory (Join-Path $root 'partials-test')
        $reports = New-FreshDirectory (Join-Path $root 'reports')
        $childScript = Join-Path $root ('synthetic child ' + [char]0x2603 + '.ps1')
        [IO.File]::WriteAllText($childScript, @'
param([string]$Partial)
[Console]::Out.WriteLine('child-pid:' + $PID)
[Console]::Out.WriteLine('synthetic stdout')
[Console]::Error.WriteLine('synthetic stderr')
[IO.File]::WriteAllBytes($Partial, [byte[]]::new(1MB))
Start-Sleep -Seconds 30
'@)
        $shell = (Get-Process -Id $PID).Path
        $otherPartial = Join-Path $root 'unrelated.partial'
        $other = New-ReplicaProcess $shell @('-NoProfile', '-File', $childScript, '-Partial', $otherPartial)
        $otherStarted = $false
        try {
            $otherStarted = $other.Start()
            Assert-Check $otherStarted 'Could not start unrelated synthetic child.'
            $partial = Join-Path $partials 'owned.partial'
            $stopped = Invoke-InterruptedCopy $shell @('-NoProfile', '-File', $childScript, '-Partial', $partial) $root 8MB $reports 15
            Assert-Check (-not $other.HasExited -and $stopped.process_id -ne $other.Id) 'Unrelated process was stopped.'
            Assert-Check ($stopped.partial -eq $partial -and $stopped.partial_bytes -eq 1MB) 'Partial observation was lost.'
            Assert-Check ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -eq $stopped.partial_sha256) 'Retained partial digest differs.'
            Assert-Check ((Get-Content -LiteralPath (Join-Path $reports 'interrupt-process.stderr.log') -Raw) -match 'synthetic stderr') 'Child error output was lost.'
            # A failed observation must also stop its owned child and retain logs.
            $emptyRoot = New-FreshDirectory (Join-Path $root 'no observed partial')
            $timeoutReports = New-FreshDirectory (Join-Path $root 'timeout reports')
            $timedOut = $false
            try {
                $null = Invoke-InterruptedCopy $shell @('-NoProfile', '-File', $childScript, '-Partial', (Join-Path $root 'unobserved.partial')) $emptyRoot 8MB $timeoutReports 3
            } catch {
                $timedOut = $_.Exception.Message -match 'No in-progress partial was caught'
            }
            Assert-Check $timedOut 'Failed observation was accepted.'
            $output = Get-Content -LiteralPath (Join-Path $timeoutReports 'interrupt-process.stdout.log') -Raw
            Assert-Check ($output -match 'child-pid:(\d+)') 'Failed child log was lost.'
            $childId = [int]$Matches[1]
            Assert-Check ($null -eq (Get-Process -Id $childId -ErrorAction SilentlyContinue)) 'Failed observation left its child running.'
            Assert-Check (-not $other.HasExited) 'Failure cleanup stopped an unrelated child.'
        } finally {
            if ($otherStarted -and -not $other.HasExited) { $other.Kill(); $null = $other.WaitForExit(10000) }
            $other.Dispose()
        }
    }
    Write-Host "PASS: $passed replica script cases. Synthetic fixtures retained in $fixtures"
} finally {
    [Console]::OutputEncoding = $oldConsoleEncoding
}
exit 0
