# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
param(
    [Parameter(Mandatory = $true)]
    [string]$Artifacts,
    # Image owners can use this preflight without allowing installation.
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $Artifacts -Force | Out-Null

# Microsoft supports this Azure-licensed GRID package for NCasT4_v3 and
# Windows Server 2025. Do not substitute cloud-specific packages from elsewhere.
# https://learn.microsoft.com/en-us/azure/virtual-machines/windows/n-series-driver-setup
$uri = 'https://download.microsoft.com/download/0f21fd13-2390-4c79-876a-87ae3dc724c6/596.86_grid_win10_win11_server2022_server2025_dch_64bit_international_azure_swl.exe'
$expectedHash = '242780736af5af2a3329e287b496645d5feaabe0cc249820b8037e60f10d7ae3'
$expectedWindowsDriverVersion = '32.0.15.9686'
$vmSize = Invoke-RestMethod `
    -Uri 'http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01&format=text' `
    -Headers @{ Metadata = 'true' } -NoProxy -TimeoutSec 10
$os = Get-CimInstance Win32_OperatingSystem
if ($vmSize -ne 'Standard_NC4as_T4_v3' -or
    $os.Caption -notmatch 'Windows Server 2025' -or $os.Version -notlike '10.0.26100*') {
    throw 'Graphics preparation is limited to the verified Azure NC4as_T4_v3 Windows Server 2025 runner.'
}
$existingDrivers = @(Get-CimInstance Win32_PnPSignedDriver |
    Where-Object { $_.DriverProviderName -match 'NVIDIA' })
$installer = $null
$installed = $false
$rebootRequested = $false
try {
    if ($existingDrivers.Count -eq 0) {
        if ($ValidateOnly) {
            throw 'No NVIDIA driver is installed. Validation-only mode does not install software.'
        }
        $installer = Join-Path $env:RUNNER_TEMP 'houdini-azure-grid-596.86.exe'
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                Invoke-WebRequest -Uri $uri -OutFile $installer
                break
            } catch {
                Remove-Item $installer -Force -ErrorAction SilentlyContinue
                if ($attempt -eq 3) { throw }
                Write-Warning "GRID download attempt $attempt failed; retrying the same pinned URL."
                Start-Sleep -Seconds 5
            }
        }
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
        # Fresh ephemeral VM only. Never reboot or persist a custom image in this job.
        $process = Start-Process -FilePath $installer -ArgumentList '-s', '-noreboot' `
            -PassThru -Wait -RedirectStandardOutput "$Artifacts/installer.stdout.log" `
            -RedirectStandardError "$Artifacts/installer.stderr.log"
        $process.ExitCode | Set-Content "$Artifacts/installer-exit-code.txt"
        $rebootRequested = $process.ExitCode -eq 3010
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Azure GRID installer failed with exit code $($process.ExitCode)."
        }
        if ($rebootRequested) {
            throw 'GRID installation requires a reboot. Provide a prepared runner image; this job never reboots.'
        }
        $installed = $true
    } else {
        Write-Host 'An NVIDIA driver is already installed; validating it without downloading or replacing software.'
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
        installer_requested_reboot = $rebootRequested
    } | ConvertTo-Json -Depth 6 | Tee-Object -FilePath "$Artifacts/graphics-after-install.json"
    if (-not $ready) {
        throw 'NVIDIA GPU is not initialized. A preinstalled/rebooted runner image may be required.'
    }
    $gpuDrivers = @($drivers | Where-Object { $_.DeviceName -eq 'NVIDIA Tesla T4' })
    if ($gpuDrivers.Count -ne 1 -or -not $gpuDrivers[0].IsSigned -or
        $gpuDrivers[0].DriverVersion -ne $expectedWindowsDriverVersion) {
        throw "Expected one signed Tesla T4 driver $expectedWindowsDriverVersion (GRID 596.86). Existing drivers are never replaced."
    }
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    $smiPath = if ($smi) { $smi.Source } else { "$env:WINDIR/System32/nvidia-smi.exe" }
    if (-not (Test-Path $smiPath)) {
        $smiPath = "$env:ProgramFiles/NVIDIA Corporation/NVSMI/nvidia-smi.exe"
    }
    if (-not (Test-Path $smiPath)) { throw 'NVIDIA management utility remains unavailable.' }
    $smiOutput = @(& $smiPath --query-gpu=name,driver_version,driver_model.current,driver_model.pending --format=csv,noheader,nounits)
    if ($LASTEXITCODE -ne 0) { throw 'NVIDIA driver query failed.' }
    $smiOutput | Tee-Object -FilePath "$Artifacts/nvidia-smi.txt"
    $gpus = @($smiOutput | ConvertFrom-Csv -Header 'name', 'driver_version', 'current', 'pending')
    if ($gpus.Count -ne 1 -or $gpus[0].name.Trim() -ne 'Tesla T4' -or
        $gpus[0].driver_version.Trim() -ne '596.86' -or
        $gpus[0].current.Trim() -ne 'WDDM' -or
        $gpus[0].pending.Trim() -ne 'WDDM') {
        throw 'Expected one Tesla T4 with driver 596.86 in current and pending WDDM mode. Graphics configuration is not changed automatically.'
    }
    @{
        source = $env:GITHUB_SHA
        runner = $env:RUNNER_NAME
        azure_vm_size = $vmSize
        os = $os | Select-Object Caption, Version, BuildNumber
        mode = $(if ($installed) { 'installed' } else { 'reused' })
        validate_only = [bool]$ValidateOnly
        driver_version = '596.86'
        driver_signed = [bool]$gpuDrivers[0].IsSigned
        driver_model = 'WDDM'
        validated = $true
    } | ConvertTo-Json -Depth 5 | Tee-Object -FilePath "$Artifacts/preparation.json"
} finally {
    # Retain provenance and diagnostics, not the 748 MB installer in artifacts.
    if ($installer) { Remove-Item $installer -Force -ErrorAction SilentlyContinue }
}
