# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Connection settings are supplied by the workflow's private configuration.
param([switch]$Cleanup)

$ErrorActionPreference = 'Stop'
if (-not $env:RUNNER_TEMP) { throw 'RUNNER_TEMP is required for isolated tunnel state.' }
$directory = Join-Path $env:RUNNER_TEMP 'houdini-windows-license'
$config = Join-Path $directory 'config'
$log = Join-Path $directory 'ssm.log'
$errorLog = Join-Path $directory 'ssm.error.log'
$state = Join-Path $directory 'process.json'
$status = Join-Path $env:RUNNER_TEMP 'houdini-windows-license-status.txt'
$env:AWS_CONFIG_FILE = $config
$env:AWS_PAGER = ''

function Write-TunnelFailureCategory {
    # SSM output contains private connection details. Upload only this summary.
    $text = (Get-Content -LiteralPath $log, $errorLog -Raw -ErrorAction SilentlyContinue) -join "`n"
    $category = 'Unclassified; inspect the private tunnel logs.'
    foreach ($known in 'AccessDenied', 'ExpiredToken', 'TargetNotConnected',
        'InvalidDocument', 'SessionManagerPlugin', 'UnauthorizedOperation') {
        if ($text -match [regex]::Escape($known)) { $category = $known; break }
    }
    "SideFX license tunnel failure: $category" |
        Tee-Object -FilePath $status | Write-Output
}

function Stop-OwnedTunnel {
    if (-not (Test-Path -LiteralPath $directory)) { return }
    $terminationFailed = $false
    try {
        # Preserve only known transport diagnostics before deleting private
        # session output. Session IDs, credentials and license data stay private.
        $text = (Get-Content -LiteralPath $log, $errorLog -Raw -ErrorAction SilentlyContinue) -join "`n"
        $diagnostics = @{}
        foreach ($signature in @(
            'Connection to destination port failed',
            'Connection refused', 'Failed to connect', 'Waiting for connections',
            'Connection accepted', 'AccessDenied', 'ExpiredToken',
            'TargetNotConnected', 'InvalidDocument', 'Session timed out'
        )) {
            $diagnostics[$signature] = [regex]::Matches(
                $text, [regex]::Escape($signature), 'IgnoreCase'
            ).Count
        }
        $artifactDirectory = Join-Path $env:RUNNER_TEMP 'houdini-gui-artifacts'
        New-Item -ItemType Directory -Path $artifactDirectory -Force | Out-Null
        $diagnostics | ConvertTo-Json |
            Set-Content (Join-Path $artifactDirectory 'ssm-transport-summary.json')
        if (Test-Path -LiteralPath $log) {
            $match = [regex]::Match(
                (Get-Content -LiteralPath $log -Raw),
                'SessionId:\s*([A-Za-z0-9._-]+)'
            )
            if ($match.Success) {
                $terminationLog = Join-Path $directory 'termination.private.log'
                & aws ssm terminate-session --session-id $match.Groups[1].Value --profile license *> $terminationLog
                $terminationFailed = $LASTEXITCODE -ne 0
                $terminationCategory = if ($terminationFailed) { 'Unclassified' } else { 'Success' }
                $terminationText = Get-Content -LiteralPath $terminationLog -Raw
                foreach ($category in 'AccessDenied', 'ExpiredToken', 'InvalidSessionId',
                    'SessionDoesNotExist', 'Unable to locate credentials') {
                    if ($terminationText -match [regex]::Escape($category)) {
                        $terminationCategory = $category
                        break
                    }
                }
                # Capture only the error category; IDs and private AWS output
                # stay outside the uploaded artifact directory.
                @{
                    attempted = $true
                    succeeded = -not $terminationFailed
                    category = $terminationCategory
                } | ConvertTo-Json |
                    Set-Content (Join-Path $artifactDirectory 'ssm-cleanup-summary.json')
            }
        }
    } finally {
        if (Test-Path -LiteralPath $state) {
            $owned = Get-Content -LiteralPath $state -Raw | ConvertFrom-Json
            $process = Get-Process -Id $owned.pid -ErrorAction SilentlyContinue
            # A reused PID belongs to another process; never terminate it.
            if ($process -and $process.StartTime.ToUniversalTime().Ticks -eq $owned.start_ticks) {
                & taskkill.exe /PID $owned.pid /T /F *> $null
                if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $owned.pid -ErrorAction SilentlyContinue)) {
                    throw 'The owned license tunnel process tree could not be stopped.'
                }
            }
        }
    }
    if ($terminationFailed) {
        # Retain state for a later cleanup retry. Do not upload private logs.
        throw 'Remote SideFX license-session termination failed.'
    }
    if (Test-Path -LiteralPath $directory) {
        Remove-Item -LiteralPath $directory -Recurse -Force
    }
}

if ($Cleanup) {
    Stop-OwnedTunnel
    exit 0
}

foreach ($name in 'AWS_REGION', 'LICENSE_ROLE_ARN', 'BASTION_INSTANCE_TAG',
    'LICENSE_PORT', 'LICENSE_SESSION_DOCUMENT') {
    $value = [Environment]::GetEnvironmentVariable($name, 'Process')
    if (-not $value) { throw "Missing license configuration: $name" }
    if ($value -match '[\r\n]') { throw "License configuration must be single-line: $name" }
}
$licensePort = 0
if (-not [int]::TryParse($env:LICENSE_PORT, [ref]$licensePort) -or
    $licensePort -lt 1 -or $licensePort -gt 65535) {
    throw 'Invalid license connection configuration.'
}
$sessionDocument = $env:LICENSE_SESSION_DOCUMENT
if ($sessionDocument -notmatch '^[A-Za-z0-9_.-]{3,128}$') {
    throw 'Invalid license session configuration.'
}
Get-Command aws -ErrorAction Stop | Out-Null
Get-Command session-manager-plugin -ErrorAction Stop | Out-Null
if (Test-Path -LiteralPath $directory) {
    throw 'License tunnel state already exists; clean up the previous session first.'
}
# Allocate a separate loopback port without replacing an existing listener
# or stopping the process that owns it.
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
try {
    $listener.Start()
    $localPort = $listener.LocalEndpoint.Port
} finally {
    $listener.Stop()
}
New-Item -ItemType Directory -Path $directory | Out-Null

try {
    # Credentials come from the job's short-lived OIDC environment. The role
    # profile contains no access keys; do not write credentials to disk.
    @"
[profile license]
role_arn = $env:LICENSE_ROLE_ARN
credential_source = Environment
region = $env:AWS_REGION
"@ | Set-Content -LiteralPath $config
    $instances = & aws ec2 describe-instances --profile license `
        --filters "Name=tag:Name,Values=$env:BASTION_INSTANCE_TAG" 'Name=instance-state-name,Values=running' `
        --query 'Reservations[].Instances[].InstanceId' --output json 2>$errorLog
    if ($LASTEXITCODE -ne 0) { throw 'License bastion lookup failed.' }
    $bastions = @($instances | ConvertFrom-Json)
    if ($bastions.Count -ne 1 -or $bastions[0] -notmatch '^i-[0-9a-f]{8}([0-9a-f]{9})?$') {
        throw 'Expected exactly one running license bastion.'
    }
    $process = Start-Process aws -ArgumentList @(
        'ssm', 'start-session', '--target', $bastions[0],
        '--document-name', $sessionDocument,
        '--parameters', "portNumber=$licensePort,localPortNumber=$localPort",
        '--profile', 'license'
    ) -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $errorLog -PassThru
    @{pid = $process.Id; start_ticks = $process.StartTime.ToUniversalTime().Ticks} |
        ConvertTo-Json | Set-Content -LiteralPath $state

    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while ([DateTime]::UtcNow -lt $deadline) {
        $process.Refresh()
        if ($process.HasExited) { throw 'License tunnel exited before readiness.' }
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            if ($client.ConnectAsync('127.0.0.1', $localPort).Wait(1000) -and $client.Connected) {
                $env:HOUDINI_GUI_LICENSE_PORT = "$localPort"
                if ($env:GITHUB_ENV) {
                    "HOUDINI_GUI_LICENSE_PORT=$localPort" | Out-File $env:GITHUB_ENV -Append
                }
                'SideFX local license forwarder is listening; GUI license checkout is not yet verified.' |
                    Tee-Object -FilePath $status -Append | Write-Output
                exit 0
            }
        } catch {
            # Keep polling during SSM startup; ConnectAsync can fault on refusal.
        } finally {
            $client.Dispose()
        }
        Start-Sleep -Milliseconds 250
    }
    throw 'The SideFX license tunnel did not become ready within 60 seconds.'
} catch {
    Write-TunnelFailureCategory
    Stop-OwnedTunnel
    throw
}
