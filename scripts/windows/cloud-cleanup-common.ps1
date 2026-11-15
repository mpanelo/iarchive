# Shared launcher implementation. Dot-sourcing this file launches nothing.
function Invoke-CloudPython {
    param([string[]]$ArgumentList)
    # Keep the console attached for getpass, MFA, y/N prompts and Ctrl+C.
    & uv @ArgumentList
    if ($LASTEXITCODE -ne 0) { throw "Cleanup stopped with exit code $LASTEXITCODE. Inspect its retained records before proceeding." }
}

function Invoke-CloudCleanup {
    param([string]$RepoRoot, [switch]$Recover, [string]$Draft, [string]$Album, [string]$Run)
    if (($Recover -and ($Draft -or $Album)) -or ($Run -and -not $Recover)) {
        throw 'Use -Draft and -Album for cleanup, or -Recover with an optional -Run directory.'
    }
    $environment = $null
    $oldEncoding = [Console]::OutputEncoding
    $oldPythonUtf8 = $env:PYTHONUTF8
    Push-Location $RepoRoot
    try {
        Assert-WindowsHost
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $env:PYTHONUTF8 = '1'
        $environment = Enter-ProjectEnvironment $RepoRoot
        Assert-UvVersion
        $env:UV_PROJECT_ENVIRONMENT = Assert-SafeDirectory (Join-Path $RepoRoot 'tools\cloud-probe\.venv')
        $arguments = @('run', '--locked', '--project', 'tools/cloud-probe', '--python', '3.12',
            'python', 'tools/cloud-cleanup/run.py')
        if ($Recover) {
            $arguments += '--recover'
            if ($Run) { $arguments += @('--run', $Run) }
        } else {
            $arguments += @('--iarchive', (Join-Path $RepoRoot 'target\release\iarchive.exe'))
            if ($Draft) { $arguments += @('--draft', $Draft) }
            if ($Album) { $arguments += @('--album', $Album) }
        }
        Invoke-CloudPython $arguments
    } finally {
        Restore-ProjectEnvironment $environment
        [Console]::OutputEncoding = $oldEncoding
        $env:PYTHONUTF8 = $oldPythonUtf8
        Pop-Location
    }
}
