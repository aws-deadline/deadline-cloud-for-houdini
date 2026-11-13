# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Run only on a disposable CI runner, or with explicit authorization to change
# and restore the workstation's hserver route. A process-local SESI_LMHOST does
# not redirect an existing Windows hserver service.
param(
    [string]$Python,
    [string]$Artifacts,
    [switch]$GuiOnly,
    [switch]$Restore
)
$ErrorActionPreference = 'Stop'
if (-not $env:RUNNER_TEMP) { throw 'RUNNER_TEMP is required for license restoration state.' }
$state = Join-Path $env:RUNNER_TEMP 'houdini-gui-license-route.json'
function Restore-HoudiniRoute {
    if (-not (Test-Path -LiteralPath $state)) { return }
    $route = Get-Content -LiteralPath $state -Raw | ConvertFrom-Json
    & $route.hserver -S $route.original *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Original Houdini license route restoration failed.' }
    Remove-Item -LiteralPath $state
}
if ($Restore) { Restore-HoudiniRoute; exit 0 }
if (-not $Python -or -not $Artifacts) { throw 'Python and Artifacts are required.' }
if (Test-Path -LiteralPath $state) { throw 'Restore the previous license route before starting.' }
New-Item -ItemType Directory -Force -Path $Artifacts | Out-Null
$hserver = Join-Path (Split-Path $env:HOUDINI_EXECUTABLE) 'hserver.exe'
$before = & $hserver -l 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the original hserver route.' }
$match = [regex]::Match($before, 'License Server:\s+([^\r\n]+)')
if (-not $match.Success) { throw 'Cannot determine the original hserver route.' }
$original = $match.Groups[1].Value.Trim()
@{hserver = $hserver; original = $original} | ConvertTo-Json | Set-Content -LiteralPath $state
$credentials = @{}
$testExit = 1
try {
    & $hserver -S 'http://127.0.0.1:1715' *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot configure this runner for the SideFX tunnel.' }
    foreach ($entry in Get-ChildItem Env:) {
        if ($entry.Name -like 'AWS_*' -or $entry.Name -like 'ACTIONS_ID_TOKEN_*') {
            $credentials[$entry.Name] = $entry.Value
            Remove-Item "Env:\$($entry.Name)"
        }
    }
    $env:HOUDINI_GUI_REPO = (Get-Location).Path
    $env:HOUDINI_GUI_PYTHON_VERSION = '3.13'
    $env:PYTHONPATH = Join-Path $env:HOUDINI_GUI_REPO 'test/houdini_submitter_ui/_python'
    $env:HOUDINI_PATH = "$(Join-Path $env:HOUDINI_GUI_REPO 'src/deadline/houdini_submitter');&"
    $env:HOUDINI_USER_PREF_DIR = Join-Path $Artifacts 'baseline-prefs-__HVER__'
    $env:PATH = "$(Split-Path $Python);$(Split-Path $env:HOUDINI_EXECUTABLE);$env:PATH"
    $ErrorActionPreference = 'Continue'
    $selection = @()
    if ($GuiOnly) { $selection += '--gui-only' }
    & $Python pipeline/run-houdini-gui-baseline.py --artifacts $Artifacts @selection *> "$Artifacts/pytest.log"
    $testExit = $LASTEXITCODE
    "pytest_exit=$testExit" | Set-Content "$Artifacts/exit.txt"
    Get-Content "$Artifacts/pytest.log"
} finally {
    $ErrorActionPreference = 'Stop'
    foreach ($name in $credentials.Keys) {
        [Environment]::SetEnvironmentVariable($name, $credentials[$name], 'Process')
    }
    Restore-HoudiniRoute
    'Original hserver route restored.' | Set-Content "$Artifacts/license-restoration.txt"
}
exit $testExit
