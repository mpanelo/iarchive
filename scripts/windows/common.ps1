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
        throw "Missing $Name. Run scripts\windows\setup.ps1 to install missing prerequisites; see docs/windows-testing.md."
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

function Assert-UvVersion {
    Require-Application uv
    $version = @(& uv --version) -join ' '
    Assert-Check ($LASTEXITCODE -eq 0 -and $version -match '^uv (\d+\.\d+\.\d+)') 'Cannot determine uv version.'
    Assert-Check ([version]$Matches[1] -ge [version]'0.12.3') 'Use uv 0.12.3 or newer for isolated Python installation settings.'
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

function Restore-ProjectEnvironment {
    param($Context)
    if ($null -ne $Context) {
        foreach ($name in $Context.previous.Keys) {
            if ($null -eq $Context.previous[$name]) {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
            } else {
                Set-Item -LiteralPath "Env:$name" -Value $Context.previous[$name]
            }
        }
    }
}

function Enter-ProjectEnvironment {
    param([string]$RepoRoot)
    $repo = Assert-SafeDirectory $RepoRoot
    $base = Assert-SafeDirectory (Join-Path $repo '.windows-sandbox')
    $directories = @{
        CARGO_HOME = (Join-Path $base 'cargo')
        CARGO_INSTALL_ROOT = (Join-Path $base 'cargo')
        RUSTUP_HOME = (Join-Path $base 'rustup')
        UV_CACHE_DIR = (Join-Path $base 'uv-cache')
        UV_PYTHON_INSTALL_DIR = (Join-Path $base 'python')
        UV_PYTHON_BIN_DIR = (Join-Path $base 'python-bin')
        UV_PROJECT_ENVIRONMENT = (Join-Path $repo 'image-worker\.venv')
        CARGO_TARGET_DIR = (Join-Path $repo 'target')
        TEMP = (Join-Path $base 'temp')
        TMP = (Join-Path $base 'temp')
        TMPDIR = (Join-Path $base 'temp')
    }
    # Inspect every destination before making any changes or launching tools.
    foreach ($path in $directories.Values) { $null = Assert-SafeDirectory $path }
    $null = Assert-SafeDirectory (Join-Path $repo '.smoke')
    foreach ($path in ($directories.Values | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $path)) {
            New-Item -ItemType Directory -Path $path | Out-Null
        }
    }
    $values = $directories.Clone()
    $values.RUSTUP_TOOLCHAIN = 'stable-x86_64-pc-windows-msvc'
    $values.UV_MANAGED_PYTHON = 'true'
    $values.UV_PYTHON_INSTALL_REGISTRY = 'false'
    $values.UV_PYTHON_NO_REGISTRY = 'true'
    $values.UV_PYTHON_INSTALL_BIN = 'false'
    $values.PATH = (Join-Path $directories.CARGO_HOME 'bin') + [IO.Path]::PathSeparator + $env:PATH
    $context = [pscustomobject]@{ root = $base; previous = @{} }
    try {
        foreach ($name in $values.Keys) {
            $context.previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, $values[$name], 'Process')
        }
        return $context
    } catch {
        Restore-ProjectEnvironment $context
        throw
    }
}

function Get-DriveTestDirectory {
    param([string]$DriveRoot, [string]$RunId)
    Assert-Check ($RunId -match '^[a-f0-9]{32}$') 'Invalid test run identifier.'
    $base = Assert-SafeDirectory (Join-Path $DriveRoot 'iarchive-sandbox')
    $marker = Join-Path $base '.iarchive-test-root'
    if (Test-Path -LiteralPath $base) {
        $item = Get-Item -LiteralPath $marker -Force
        Assert-Check (-not $item.PSIsContainer -and
            -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) 'Unsafe drive test marker.'
        Assert-Check ([IO.File]::ReadAllText($marker) -ceq 'iarchive synthetic drive tests v1') "Unrecognized test directory: $base"
    } else {
        $null = New-FreshDirectory $base
        [IO.File]::WriteAllText($marker, 'iarchive synthetic drive tests v1')
    }
    return New-FreshDirectory (Join-Path $base "run-$RunId")
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
    $environmentRepo = New-FreshDirectory (Join-Path $guards 'environment repo')
    $originalCargo = $env:CARGO_HOME
    $originalPath = $env:PATH
    $originalTemp = $env:TEMP
    $originalRegistry = $env:UV_PYTHON_INSTALL_REGISTRY
    $nestedEnvironment = $null
    try {
        $nestedEnvironment = Enter-ProjectEnvironment $environmentRepo
        Assert-Check ($env:CARGO_HOME -eq (Join-Path $environmentRepo '.windows-sandbox\cargo')) 'Cargo escaped the project environment.'
        Assert-Check ($env:TEMP -eq (Join-Path $environmentRepo '.windows-sandbox\temp')) 'Temporary files escaped the project environment.'
        Assert-Check ($env:UV_PYTHON_INSTALL_REGISTRY -eq 'false') 'Python registry registration was enabled.'
        # Exercise the same finally path used after failed native commands.
        Invoke-CheckedNative $env:ComSpec @('/c', 'exit', '7')
        throw 'Expected native command failure.'
    } catch {
        Assert-Check ($_.Exception.Message -match 'failed with exit code 7') 'Unexpected environment guard failure.'
    } finally {
        Restore-ProjectEnvironment $nestedEnvironment
    }
    Assert-Check ($env:CARGO_HOME -eq $originalCargo -and $env:PATH -eq $originalPath -and
        $env:TEMP -eq $originalTemp -and $env:UV_PYTHON_INSTALL_REGISTRY -eq $originalRegistry) 'Process environment was not restored after failure.'
    $driveFixture = New-FreshDirectory (Join-Path $guards 'drive')
    $unmarked = New-FreshDirectory (Join-Path $driveFixture 'iarchive-sandbox')
    $refused = $false
    try { $null = Get-DriveTestDirectory $driveFixture ([guid]::NewGuid().ToString('N')) } catch { $refused = $true }
    Assert-Check $refused 'An existing unmarked directory was accepted for drive tests.'
    Assert-Check (@(Get-ChildItem -LiteralPath $unmarked -Force).Count -eq 0) 'Drive guard modified an existing directory.'
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

function Invoke-ReplicaSmoke {
    param([string]$Exe, [string]$Media, [string]$MediaParent, [string]$State, [string]$Reports)
    $secondary = New-FreshDirectory (Join-Path $MediaParent 'replica-media')
    $primaryArtifacts = New-FreshDirectory (Join-Path $MediaParent 'replica-primary-artifacts')
    $secondaryArtifacts = New-FreshDirectory (Join-Path $MediaParent 'replica-secondary-artifacts')
    $arguments = @('--cycle', 'demo', '--secondary', $secondary,
        '--primary-artifacts', $primaryArtifacts, '--secondary-artifacts', $secondaryArtifacts,
        '--allow-unverified-devices')
    # These two directories are deliberately on one volume: test byte preservation,
    # never claim physically independent backups from this fixture.
    $plan = Invoke-IarchiveJson $Exe $State $Reports 'replica-dry-run' (@('--dry-run', 'replicate') + $arguments)
    Assert-Check (-not $plan.complete -and $plan.planned_files -gt 0) 'Replica dry-run reported completion or no planned files.'
    Assert-Check (@(Get-ChildItem -LiteralPath $secondary -Force).Count -eq 0) 'Replica dry-run wrote media.'
    $first = Invoke-IarchiveJson $Exe $State $Reports 'replicate' (@('replicate') + $arguments)
    Assert-Check ($first.complete -and $first.copied_files -eq $plan.planned_files -and
        $first.local_replica -eq 'LOCAL BYTES VERIFIED' -and
        $first.independent_physical_drives -eq 'NOT VERIFIED') 'Synthetic byte replication or independence reporting is incorrect.'
    $again = Invoke-IarchiveJson $Exe $State $Reports 'replicate-again' (@('replicate') + $arguments)
    Assert-Check ($again.complete -and $again.copied_files -eq 0) 'Replica rerun recopied matching files.'
    $verified = Invoke-IarchiveJson $Exe $State $Reports 'verify-deep' (@('verify', '--deep') + $arguments)
    Assert-Check ($verified.complete -and $verified.verified_files -eq $plan.planned_files) 'Deep verification did not read every file.'
    $sourceFiles = @(Get-SyntheticSnapshot $Media)
    $replicaFiles = @(Get-SyntheticSnapshot $secondary)
    Assert-Check ((ConvertTo-Json -InputObject $sourceFiles -Depth 5 -Compress) -eq
        (ConvertTo-Json -InputObject $replicaFiles -Depth 5 -Compress)) 'Replica bytes or modification times differ.'
    Assert-Check (@($verified.artifact_bundles).Count -eq 3) 'Verified artifacts were not saved in three locations.'
    foreach ($bundle in $verified.artifact_bundles) {
        # Rust canonical paths use verbatim prefixes; these short fixture paths
        # also need to work through Windows PowerShell 5.1 filesystem providers.
        $bundlePath = $bundle
        if ($bundle.StartsWith('\\?\UNC\')) { $bundlePath = '\\' + $bundle.Substring(8) }
        elseif ($bundle.StartsWith('\\?\')) { $bundlePath = $bundle.Substring(4) }
        foreach ($name in @('manifest.jsonl', 'manifest.sqlite', 'summary.json')) {
            Assert-Check (Test-Path -LiteralPath (Join-Path $bundlePath $name) -PathType Leaf) "Missing replica artifact: $name"
        }
        Assert-Check ((Get-FileHash -LiteralPath (Join-Path $bundlePath 'manifest.jsonl') -Algorithm SHA256).Hash.ToLowerInvariant() -eq
            $verified.manifest_sha256) 'Replica manifest digest differs.'
        Assert-Check ((Get-FileHash -LiteralPath (Join-Path $bundlePath 'manifest.sqlite') -Algorithm SHA256).Hash.ToLowerInvariant() -eq
            $verified.database_backup_sha256) 'Replica database backup digest differs.'
    }
    $victim = Join-Path $secondary $sourceFiles[0].path
    [IO.File]::WriteAllBytes($victim, [Text.Encoding]::ASCII.GetBytes('synthetic replica corruption'))
    $bad = Invoke-IarchiveJson $Exe $State $Reports 'verify-corrupt' (@('verify', '--deep') + $arguments) 2
    Assert-Check (-not $bad.complete -and $bad.hash_mismatches -gt 0) 'Corrupt replica retained verification.'
    $saved = Invoke-IarchiveJson $Exe $State $Reports 'replica-status-incomplete' @('status', '--cycle', 'demo')
    Assert-Check ($saved.replica_state -eq 'INCOMPLETE') 'Failed verification left saved completion valid.'
    return [pscustomobject]@{ media = $secondary; reports = $Reports; passed = $true }
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

    $replicaResult = Invoke-ReplicaSmoke $Exe $media $MediaParent $state $reports
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
    return [pscustomobject]@{ media = $media; reports = $reports; manifest = $manifest; replication = $replicaResult; passed = $true }
}
