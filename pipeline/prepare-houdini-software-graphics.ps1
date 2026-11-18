# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Isolated LLVMpipe and CPU Vulkan trial, following Mesa's Windows deployment docs.
# Never install an ICD, edit the registry, or replace system graphics DLLs.
param(
    [ValidateSet('lavapipe', 'dozen-warp')]
    [string]$VulkanDriver = 'dozen-warp'
)
$ErrorActionPreference = 'Stop'
if (-not $env:RUNNER_TEMP -or -not $env:HOUDINI_EXECUTABLE) {
    throw 'A temporary runner and an isolated Houdini installation are required.'
}
$runtime = [IO.Path]::GetFullPath($env:HOUDINI_EXECUTABLE)
$temporaryRoot = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\') + '\'
if (-not $runtime.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Software graphics can only be staged inside this runner temporary directory.'
}
$bin = Split-Path $runtime
$directory = Join-Path $env:RUNNER_TEMP 'houdini-mesa-26.2.6'
if (Test-Path $directory) { throw 'Software graphics trial storage already exists.' }
New-Item -ItemType Directory -Path $directory | Out-Null
$archive = Join-Path $directory 'mesa.7z'
$url = 'https://github.com/pal1000/mesa-dist-win/releases/download/26.2.6/mesa3d-26.2.6-release-mingw.7z'
$checksum = 'da0f71d9181b672c1918f3c342f11118c687f6059a46b16502845160733cc388'
Invoke-WebRequest -Uri $url -OutFile $archive
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $checksum) {
    throw 'Software graphics archive checksum mismatch.'
}
foreach ($name in 'opengl32.dll', 'libgallium_wgl.dll', 'houdini.exe.local') {
    if (Test-Path (Join-Path $bin $name)) {
        throw "Refusing to replace an existing application-local file: $name"
    }
}
& 7z x $archive "x64/*" "-o$directory" -y *> $null
if ($LASTEXITCODE -ne 0) { throw 'Software graphics extraction failed.' }
foreach ($name in 'opengl32.dll', 'libgallium_wgl.dll') {
    Copy-Item -LiteralPath (Join-Path "$directory/x64" $name) -Destination (Join-Path $bin $name)
}
# DLL redirection is local to this temporary executable and its directory.
New-Item -ItemType File -Path "$runtime.local" | Out-Null
$manifest = if ($VulkanDriver -eq 'lavapipe') { 'lvp_icd.x86_64.json' } else { 'dzn_icd.x86_64.json' }
$driverDll = if ($VulkanDriver -eq 'lavapipe') { 'vulkan_lvp.dll' } else { 'vulkan_dzn.dll' }
$icd = Join-Path "$directory/x64" $manifest
if (-not (Test-Path $icd) -or -not (Test-Path "$directory/x64/$driverDll")) {
    throw 'The verified software graphics package lacks its Vulkan CPU driver.'
}
foreach ($entry in @('GALLIUM_DRIVER=llvmpipe', 'LIBGL_ALWAYS_SOFTWARE=true',
    'HOUDINI_GUI_GRAPHICS_MODE=mesa-software', "VK_DRIVER_FILES=$icd",
    "VK_ICD_FILENAMES=$icd", 'VK_LOADER_DEBUG=all', "HOUDINI_GUI_VULKAN_ICD=$icd",
    "HOUDINI_GUI_VULKAN_DRIVER=$driverDll", 'DZN_DEBUG=warp',
    "PATH=$directory/x64;$env:PATH")) {
    $name, $value = $entry.Split('=', 2)
    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    if ($env:GITHUB_ENV) { $entry | Out-File $env:GITHUB_ENV -Append }
}
@{
    distribution = 'mesa-dist-win'
    version = '26.2.6'
    archive_sha256 = $checksum
    deployment = 'application-local'
    opengl_driver = 'llvmpipe'
    vulkan_driver = $VulkanDriver
} | ConvertTo-Json | Set-Content "$env:RUNNER_TEMP/houdini-gui-artifacts/software-graphics.json"
