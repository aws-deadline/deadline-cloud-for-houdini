# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Adapted from the Nuke Windows CI tunnel lifecycle pattern. SideFX uses port
# 1715; Foundry's port and licensing configuration do not apply to Houdini.
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
$licensePort = 1715

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
    $terminationFailed = $false
    try {
        if (Test-Path -LiteralPath $log) {
            $match = [regex]::Match(
                (Get-Content -LiteralPath $log -Raw),
                'SessionId:\s*([A-Za-z0-9._-]+)'
            )
            if ($match.Success) {
                & aws ssm terminate-session --session-id $match.Groups[1].Value --profile license *> $null
                $terminationFailed = $LASTEXITCODE -ne 0
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

foreach ($name in 'AWS_REGION', 'LICENSE_ROLE_ARN', 'BASTION_INSTANCE_TAG') {
    $value = [Environment]::GetEnvironmentVariable($name, 'Process')
    if (-not $value) { throw "Missing license configuration: $name" }
    if ($value -match '[\r\n]') { throw "License configuration must be single-line: $name" }
}
Get-Command aws -ErrorAction Stop | Out-Null
Get-Command session-manager-plugin -ErrorAction Stop | Out-Null
if (Test-Path -LiteralPath $directory) {
    throw 'License tunnel state already exists; clean up the previous session first.'
}
if (Get-NetTCPConnection -State Listen -LocalPort $licensePort -ErrorAction SilentlyContinue) {
    throw 'The local SideFX license port is already in use.'
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
        '--document-name', 'DccInteg-PortForwardToLicenseServer',
        '--parameters', "portNumber=$licensePort,localPortNumber=$licensePort",
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
            if ($client.ConnectAsync('127.0.0.1', $licensePort).Wait(1000) -and $client.Connected) {
                'SideFX local license forwarder is listening; GUI license checkout is not yet verified.' |
                    Tee-Object -FilePath $status | Write-Output
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
