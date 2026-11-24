param(
    [Parameter(Mandatory = $true)]
    [string]$Artifacts
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $Artifacts -Force | Out-Null

# Microsoft supports this Azure-licensed GRID package for NCasT4_v3 and
# Windows Server 2025. Do not substitute cloud-specific packages from elsewhere.
# https://learn.microsoft.com/en-us/azure/virtual-machines/windows/n-series-driver-setup
$uri = 'https://download.microsoft.com/download/0f21fd13-2390-4c79-876a-87ae3dc724c6/596.86_grid_win10_win11_server2022_server2025_dch_64bit_international_azure_swl.exe'
$expectedHash = '242780736af5af2a3329e287b496645d5feaabe0cc249820b8037e60f10d7ae3'
$vmSize = Invoke-RestMethod `
    -Uri 'http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01&format=text' `
    -Headers @{ Metadata = 'true' } -NoProxy -TimeoutSec 10
$os = Get-CimInstance Win32_OperatingSystem
if ($vmSize -ne 'Standard_NC4as_T4_v3' -or
    $os.Caption -notmatch 'Windows Server 2025' -or $os.Version -notlike '10.0.26100*') {
    throw 'This graphics trial is limited to the verified Azure NC4as_T4_v3 Windows Server 2025 runner.'
}
$existingDrivers = @(Get-CimInstance Win32_PnPSignedDriver |
    Where-Object { $_.DriverProviderName -match 'NVIDIA' })
if ($existingDrivers.Count -ne 0) {
    throw 'The runner already has an NVIDIA driver. Refusing to replace an existing installation.'
}

$installer = Join-Path $env:RUNNER_TEMP 'houdini-azure-grid-596.86.exe'
Invoke-WebRequest -Uri $uri -OutFile $installer
$actualHash = (Get-FileHash -Path $installer -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) { throw 'Azure GRID installer checksum mismatch.' }
$signature = Get-AuthenticodeSignature -FilePath $installer
@{
    source = $env:GITHUB_SHA
    runner = $env:RUNNER_NAME
    azure_vm_size = $vmSize
    os = $os | Select-Object Caption, Version, BuildNumber
    installer_url = $uri
    installer_sha256 = $actualHash
    signature_status = [string]$signature.Status
    signer = $signature.SignerCertificate.Subject
    driver_version = '596.86'
} | ConvertTo-Json -Depth 5 | Set-Content "$Artifacts/installer-provenance.json"
if ($signature.Status -ne 'Valid' -or
    $signature.SignerCertificate.Subject -notmatch 'O=NVIDIA Corporation(?:,|$)') {
    throw 'Azure GRID installer must have a valid NVIDIA Authenticode signature.'
}

try {
    # Fresh ephemeral VM only. Never reboot or persist a custom image in this job.
    $process = Start-Process -FilePath $installer -ArgumentList '-s', '-noreboot' `
        -PassThru -Wait -RedirectStandardOutput "$Artifacts/installer.stdout.log" `
        -RedirectStandardError "$Artifacts/installer.stderr.log"
    $process.ExitCode | Set-Content "$Artifacts/installer-exit-code.txt"
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "Azure GRID installer failed with exit code $($process.ExitCode)."
    }

    $deadline = (Get-Date).AddSeconds(60)
    do {
        $devices = @(Get-CimInstance Win32_PnPEntity |
            Where-Object { $_.PNPDeviceID -like 'PCI\VEN_10DE*' })
        $drivers = @(Get-CimInstance Win32_PnPSignedDriver |
            Where-Object { $_.DriverProviderName -match 'NVIDIA' })
        $ready = $devices.Count -gt 0 -and $drivers.Count -gt 0 -and
            @($devices | Where-Object { $_.ConfigManagerErrorCode -ne 0 }).Count -eq 0
        if ($ready -or (Get-Date) -ge $deadline) { break }
        Start-Sleep -Seconds 2
    } while ($true)

    @{
        devices = @($devices | Select-Object Name, Status, ConfigManagerErrorCode)
        drivers = @($drivers | Select-Object DeviceName, DriverVersion, DriverProviderName, IsSigned)
        video = @(Get-CimInstance Win32_VideoController |
            Select-Object Name, DriverVersion, CurrentHorizontalResolution, CurrentVerticalResolution)
        initialized = $ready
        installer_requested_reboot = $process.ExitCode -eq 3010
    } | ConvertTo-Json -Depth 6 | Tee-Object -FilePath "$Artifacts/graphics-after-install.json"
    if (-not $ready) {
        throw 'NVIDIA GPU is not initialized. A preinstalled/rebooted runner image may be required.'
    }
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    $smiPath = if ($smi) { $smi.Source } else { "$env:WINDIR/System32/nvidia-smi.exe" }
    if (-not (Test-Path $smiPath)) {
        $smiPath = "$env:ProgramFiles/NVIDIA Corporation/NVSMI/nvidia-smi.exe"
    }
    if (-not (Test-Path $smiPath)) { throw 'NVIDIA management utility remains unavailable.' }
    & $smiPath --query-gpu=name,driver_version,driver_model.current,driver_model.pending --format=csv |
        Tee-Object -FilePath "$Artifacts/nvidia-smi.txt"
    if ($LASTEXITCODE -ne 0) { throw 'NVIDIA driver query failed.' }
} finally {
    # Retain provenance and diagnostics, not the 748 MB installer in artifacts.
    Remove-Item $installer -Force -ErrorAction SilentlyContinue
}
