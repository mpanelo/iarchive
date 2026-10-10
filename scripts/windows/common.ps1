# Shared helpers for the Windows entry points. No commands run on dot-source.
function Assert-WindowsHost {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
        -not [Environment]::Is64BitProcess) {
        throw 'Use 64-bit Windows PowerShell 5.1 or PowerShell 7 on Windows.'
    }
}

function Require-Application {
    param([string]$Name)
    if (-not (Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue)) {
        throw "Missing $Name. Run scripts\windows\setup.ps1 -InstallPrerequisites or install the prerequisites manually."
    }
}

function Invoke-CheckedNative {
    param([string]$Program, [string[]]$ArgumentList)
    Write-Host "> $Program $($ArgumentList -join ' ')"
    & $Program @ArgumentList | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE."
    }
}

function Assert-SafeDirectory {
    param([string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $ancestor = $full
    while ($ancestor) {
        $directoryErrors = @()
        $item = Get-Item -LiteralPath $ancestor -Force -ErrorAction SilentlyContinue -ErrorVariable directoryErrors
        foreach ($failure in $directoryErrors) {
            if ($failure.CategoryInfo.Category -ne [Management.Automation.ErrorCategory]::ObjectNotFound) {
                throw "Cannot inspect directory ancestor safely: $ancestor"
            }
        }
        if ($null -ne $item) {
            if (-not $item.PSIsContainer -or
                ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Directory or ancestor is a file, symlink, or reparse point: $ancestor"
            }
        }
        $ancestor = [IO.Path]::GetDirectoryName($ancestor)
    }
    return $full
}

function New-FreshDirectory {
    param([string]$Path)
    $full = Assert-SafeDirectory $Path
    if (Test-Path -LiteralPath $full) {
        throw "Test destination already exists: $full"
    }
    New-Item -ItemType Directory -Path $full -ErrorAction Stop | Out-Null
    return $full
}

function Get-ExternalDriveRoot {
    param([string]$Drive)
    Assert-Check ($Drive -match '^[A-Za-z]:\\?$') "Supply drive roots only (D: or D:\), not an archive directory: $Drive"
    $root = Assert-SafeDirectory ($Drive.Substring(0, 2).ToUpperInvariant() + '\')
    Assert-Check (Test-Path -LiteralPath $root -PathType Container) "Drive is unavailable: $root"
    $info = [IO.DriveInfo]::new($root)
    Assert-Check ($info.IsReady -and $info.DriveType -in @([IO.DriveType]::Fixed, [IO.DriveType]::Removable)) "Use an available local fixed/removable drive: $root"
    return $root
}

function Write-JsonFile {
    param([string]$Path, $Value)
    [IO.File]::WriteAllText($Path, (ConvertTo-Json -InputObject $Value -Depth 30),
        [Text.UTF8Encoding]::new($false))
}

function Assert-Check {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Get-SyntheticSnapshot {
    param([string]$Root)
    $rootPath = Assert-SafeDirectory $Root
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($rootPath)
    $records = @()
    while ($pending.Count) {
        foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Reparse point in synthetic fixture: $($item.FullName)"
            }
            if ($item.PSIsContainer) {
                $pending.Push($item.FullName)
            } else {
                $records += [pscustomobject]@{
                    path = $item.FullName.Substring($rootPath.Length).TrimStart('\', '/').Replace('\', '/')
                    bytes = $item.Length
                    mtime_ticks = $item.LastWriteTimeUtc.Ticks
                    sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
        }
    }
    return $records | Sort-Object path
}

function Assert-UnchangedSnapshot {
    param([string]$Root, [object[]]$Before)
    $after = @(Get-SyntheticSnapshot $Root)
    $beforeJson = ConvertTo-Json -InputObject $Before -Depth 5 -Compress
    $afterJson = ConvertTo-Json -InputObject $after -Depth 5 -Compress
    Assert-Check ($beforeJson -eq $afterJson) "Synthetic media changed during scanning: $Root"
}

function Invoke-IarchiveJson {
    param([string]$Exe, [string]$State, [string]$Reports, [string]$Name,
        [string[]]$ArgumentList, [int]$ExpectedExit = 0)
    Write-Host "> iarchive $($ArgumentList -join ' ') (expected exit $ExpectedExit)"
    $lines = @(& $Exe --state-dir $State --json @ArgumentList)
    $code = $LASTEXITCODE
    $raw = $lines -join "`n"
    $reportPath = Join-Path $Reports "$Name.json"
    if ([string]::IsNullOrWhiteSpace($raw)) {
        Write-JsonFile $reportPath ([pscustomobject]@{ exit_code = $code; stdout = ''; note = 'See console stderr for the CLI error.' })
    } else {
        [IO.File]::WriteAllText($reportPath, $raw, [Text.UTF8Encoding]::new($false))
    }
    Assert-Check ($code -eq $ExpectedExit) "iarchive $Name returned $code; expected $ExpectedExit. See $Reports."
    if ([string]::IsNullOrWhiteSpace($raw)) {
        Assert-Check ($ExpectedExit -ne 0) "iarchive $Name emitted no JSON."
        return $null
    }
    return $raw | ConvertFrom-Json
}

function Test-ScriptGuards {
    param([string]$Root)
    foreach ($invalid in @('D:\photos', 'D:photos', '..', '\\server\share', 'D:\..\E:\')) {
        $refused = $false
        try { $null = Get-ExternalDriveRoot $invalid } catch { $refused = $true }
        Assert-Check $refused "Unsafe drive argument was accepted: $invalid"
    }
    $refused = $false
    try { Invoke-CheckedNative $env:ComSpec @('/c', 'exit', '7') } catch { $refused = $true }
    Assert-Check $refused 'Native command failure did not stop execution.'
    $guards = New-FreshDirectory (Join-Path $Root 'guards')
    $fixture = New-FreshDirectory (Join-Path $guards 'fixture')
    $outside = New-FreshDirectory (Join-Path $guards 'outside')
    [IO.File]::WriteAllText((Join-Path $outside 'synthetic.txt'), 'outside synthetic fixture')
    $junction = Join-Path $fixture 'junction'
    Invoke-CheckedNative $env:ComSpec @('/c', 'mklink', '/J', $junction, $outside)
    foreach ($operation in @('snapshot', 'destination')) {
        $refused = $false
        try {
            if ($operation -eq 'snapshot') { $null = Get-SyntheticSnapshot $fixture }
            else { $null = New-FreshDirectory (Join-Path $junction 'new') }
        } catch { $refused = $true }
        Assert-Check $refused "Junction was followed by $operation."
    }
    Assert-Check (-not (Test-Path -LiteralPath (Join-Path $outside 'new'))) 'Guard test wrote through a junction.'
}

function Invoke-SyntheticSmoke {
    param([string]$Exe, [string]$MediaParent, [string]$Artifacts)
    $media = Join-Path $MediaParent 'media'
    $state = Join-Path $Artifacts 'state'
    $reports = New-FreshDirectory (Join-Path $Artifacts 'reports')
    Invoke-CheckedNative uv @('run', '--locked', '--project', 'image-worker', 'python',
        'fixtures/generate.py', '--output', $media, '--clean', '--with-heic')
    $before = @(Get-SyntheticSnapshot $media)
    Write-JsonFile (Join-Path $reports 'source-before.json') $before
    $count = $before.Count
    $doctor = Invoke-IarchiveJson $Exe $state $reports 'doctor' @('doctor', '--primary', $media)
    Assert-Check ([bool]$doctor.image_worker.available) 'The image worker is unavailable.'
    $null = Invoke-IarchiveJson $Exe $state $reports 'dry-run' @('--dry-run', 'import', '--source', $media, '--cycle', 'demo')
    Assert-Check (-not (Test-Path -LiteralPath $state)) 'Dry-run created application state.'
    $null = Invoke-IarchiveJson $Exe $state $reports 'import' @('import', '--source', $media, '--cycle', 'demo')
    $null = Invoke-IarchiveJson $Exe $state $reports 'import-again' @('import', '--source', $media, '--cycle', 'demo')
    $index = Invoke-IarchiveJson $Exe $state $reports 'index' @('index', '--cycle', 'demo')
    Assert-Check ($index.status.file_count -eq $count -and $index.hashes_computed -eq $count) 'Initial inventory/hash count is incorrect.'
    Assert-Check (@($index.exact_duplicate_groups).Count -gt 0) 'Exact duplicate fixture was not detected.'
    $cached = Invoke-IarchiveJson $Exe $state $reports 'index-cached' @('index', '--cycle', 'demo')
    Assert-Check ($cached.hash_cache_hits -eq $count -and $cached.hashes_computed -eq 0) 'Index cache did not reuse unchanged files.'
    $deep = Invoke-IarchiveJson $Exe $state $reports 'index-deep' @('index', '--cycle', 'demo', '--deep')
    Assert-Check ($deep.hash_cache_hits -eq 0 -and $deep.hashes_computed -eq $count) 'Deep index reused cached hashes.'
    $audit = Invoke-IarchiveJson $Exe $state $reports 'audit' @('audit', '--cycle', 'demo')
    Assert-Check ($audit.status.stage -eq 'AUDITED' -and $audit.decode_errors -eq 0) 'Clean fixture audit did not complete.'
    foreach ($category in @('exact', 'similar', 'dark')) {
        Assert-Check (@($audit.candidates | Where-Object { $_.type -eq $category }).Count -gt 0) "Missing $category candidates."
    }
    Assert-Check ($audit.status.local_replica -eq 'NOT VERIFIED' -and $audit.status.cloud_coverage -eq 'NOT VERIFIED') 'Report incorrectly claims backup or cloud verification.'
    $cachedAudit = Invoke-IarchiveJson $Exe $state $reports 'audit-cached' @('audit', '--cycle', 'demo')
    Assert-Check ($cachedAudit.analysis_cache_hits -eq $count) 'Audit cache did not reuse unchanged images.'
    $null = Invoke-IarchiveJson $Exe $state $reports 'status' @('status', '--cycle', 'demo')
    $null = Invoke-IarchiveJson $Exe $state $reports 'report' @('report', '--cycle', 'demo')
    $manifest = Join-Path $Artifacts 'manifest.jsonl'
    $null = Invoke-IarchiveJson $Exe $state $reports 'export' @('export-manifest', '--cycle', 'demo', '--output', $manifest)
    $records = @(Get-Content -LiteralPath $manifest -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json })
    $files = @($records | Where-Object { $_.record_type -eq 'file' })
    Assert-Check ($records.Count -eq $count + 1 -and $files.Count -eq $count) 'Manifest file count is incorrect.'
    foreach ($source in $before) {
        $matching = @($files | Where-Object { $_.relative_path -ceq $source.path -and $_.sha256 -eq $source.sha256 })
        Assert-Check ($matching.Count -eq 1) "Manifest differs from independently hashed fixture: $($source.path)"
    }
    $null = Invoke-IarchiveJson $Exe $state $reports 'export-overwrite-refused' @('export-manifest', '--cycle', 'demo', '--output', $manifest) 1
    $unsafeExport = Join-Path $media 'manifest.jsonl'
    $null = Invoke-IarchiveJson $Exe $state $reports 'export-media-refused' @('export-manifest', '--cycle', 'demo', '--output', $unsafeExport) 1
    Assert-Check (-not (Test-Path -LiteralPath $unsafeExport)) 'Export wrote inside media.'
    Assert-UnchangedSnapshot $media $before

    # Only these generated fixtures are mutated to exercise incomplete reports.
    [IO.File]::WriteAllBytes((Join-Path $media 'corrupt.heic'), [Text.Encoding]::ASCII.GetBytes('synthetic invalid HEIC'))
    [IO.File]::WriteAllBytes((Join-Path $media 'copy.partial'), [Text.Encoding]::ASCII.GetBytes('synthetic partial copy'))
    $negativeBefore = @(Get-SyntheticSnapshot $media)
    $bad = Invoke-IarchiveJson $Exe $state $reports 'audit-incomplete' @('audit', '--cycle', 'demo') 2
    Assert-Check ($bad.decode_errors -eq 1 -and -not $bad.status.inventory_complete) 'Corrupt/partial media was treated as complete.'
    foreach ($group in $bad.candidates) {
        foreach ($member in $group.members) {
            Assert-Check ($member.relative_path -notin @('corrupt.heic', 'copy.partial')) 'Corrupt/partial media became a review candidate.'
        }
    }
    Assert-UnchangedSnapshot $media $negativeBefore
    $disconnected = Join-Path $MediaParent 'media-disconnected'
    Rename-Item -LiteralPath $media -NewName 'media-disconnected'
    try {
        $missing = Invoke-IarchiveJson $Exe $state $reports 'source-unavailable' @('index', '--cycle', 'demo') 2
        Assert-Check (-not $missing.status.inventory_complete) 'Unavailable source was treated as complete.'
    } finally {
        Rename-Item -LiteralPath $disconnected -NewName 'media'
    }
    Assert-UnchangedSnapshot $media $negativeBefore
    Write-JsonFile (Join-Path $reports 'source-after.json') @(Get-SyntheticSnapshot $media)
    return [pscustomobject]@{ media = $media; reports = $reports; manifest = $manifest; passed = $true }
}
