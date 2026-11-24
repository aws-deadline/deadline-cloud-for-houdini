# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
param(
    [Parameter(Mandatory = $true)]
    [string]$Artifacts
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $Artifacts -Force | Out-Null
# This changes only the disposable runner user's display configuration.
# Never run this helper on a shared workstation.
# https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changedisplaysettingsexw
$vmSize = Invoke-RestMethod `
    -Uri 'http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01&format=text' `
    -Headers @{ Metadata = 'true' } -NoProxy -TimeoutSec 10
if ($vmSize -ne 'Standard_NC4as_T4_v3' -or
    $env:RUNNER_NAME -notlike 'aws-deadline_windows-2025_gpu-t4-4-core-*') {
    throw 'Display selection is restricted to this ephemeral GitHub GPU trial.'
}
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class HoudiniGpuDisplay {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct Device {
        public uint cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string Name;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string Description;
        public uint Flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string Id;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string Key;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct Mode {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        public ushort SpecVersion, DriverVersion, Size, DriverExtra;
        public uint Fields;
        public int X, Y;
        public uint DisplayOrientation, DisplayFixedOutput;
        public short Color, Duplex, YResolution, TTOption, Collate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string FormName;
        public ushort LogPixels;
        public uint BitsPerPel, Width, Height, DisplayFlags, DisplayFrequency;
        public uint ICMMethod, ICMIntent, MediaType, DitherType, Reserved1, Reserved2;
        public uint PanningWidth, PanningHeight;
    }
    public class Display {
        public string Name, Description;
        public bool Primary;
        public int X, Y;
        public uint Width, Height;
        internal Mode Configuration;
    }
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool EnumDisplayDevicesW(
        string device, uint index, ref Device result, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool EnumDisplaySettingsExW(
        string device, int index, ref Mode mode, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int ChangeDisplaySettingsExW(
        string device, ref Mode mode, IntPtr hwnd, uint flags, IntPtr param);
    [DllImport("user32.dll", CharSet = CharSet.Unicode,
        EntryPoint = "ChangeDisplaySettingsExW")]
    private static extern int Apply(
        string device, IntPtr mode, IntPtr hwnd, uint flags, IntPtr param);

    public static Display[] Inventory() {
        if (Marshal.SizeOf(typeof(Mode)) != 220)
            throw new InvalidOperationException("Unexpected Windows DEVMODE layout.");
        var result = new List<Display>();
        for (uint i = 0; ; i++) {
            var device = new Device { cb = (uint)Marshal.SizeOf(typeof(Device)) };
            if (!EnumDisplayDevicesW(null, i, ref device, 0)) break;
            if ((device.Flags & 1) == 0 || (device.Flags & 8) != 0) continue;
            var mode = new Mode { Size = (ushort)Marshal.SizeOf(typeof(Mode)) };
            if (!EnumDisplaySettingsExW(device.Name, -1, ref mode, 0))
                throw new InvalidOperationException("Cannot read display " + device.Name);
            result.Add(new Display {
                Name = device.Name, Description = device.Description,
                Primary = (device.Flags & 4) != 0, X = mode.X, Y = mode.Y,
                Width = mode.Width, Height = mode.Height, Configuration = mode
            });
        }
        return result.ToArray();
    }
    public static void SelectNvidia() {
        var displays = Inventory();
        var targets = Array.FindAll(displays,
            d => d.Description.IndexOf("NVIDIA", StringComparison.OrdinalIgnoreCase) >= 0);
        if (targets.Length != 1)
            throw new InvalidOperationException("Expected one active NVIDIA display.");
        var target = targets[0];
        if (target.Primary) return;
        foreach (var display in displays) {
            var mode = display.Configuration;
            mode.X -= target.X;
            mode.Y -= target.Y;
            mode.Fields |= 0x20; // DM_POSITION
            var primary = display.Name == target.Name ? 0x10u : 0u; // CDS_SET_PRIMARY
            int test = ChangeDisplaySettingsExW(
                display.Name, ref mode, IntPtr.Zero, 2u | primary, IntPtr.Zero);
            if (test != 0)
                throw new InvalidOperationException("Display mode test returned " + test);
        }
        foreach (var display in displays) {
            var mode = display.Configuration;
            mode.X -= target.X;
            mode.Y -= target.Y;
            mode.Fields |= 0x20;
            var primary = display.Name == target.Name ? 0x10u : 0u;
            // CDS_UPDATEREGISTRY | CDS_NORESET. No CDS_GLOBAL.
            int stage = ChangeDisplaySettingsExW(
                display.Name, ref mode, IntPtr.Zero, 0x10000001u | primary, IntPtr.Zero);
            if (stage != 0)
                throw new InvalidOperationException("Display staging returned " + stage);
        }
        int apply = Apply(null, IntPtr.Zero, IntPtr.Zero, 0, IntPtr.Zero);
        if (apply != 0)
            throw new InvalidOperationException("Display activation returned " + apply);
    }
}
'@
[HoudiniGpuDisplay]::Inventory() |
    ConvertTo-Json -Depth 4 | Set-Content "$Artifacts/displays-before.json"
[HoudiniGpuDisplay]::SelectNvidia()
$after = [HoudiniGpuDisplay]::Inventory()
$after | ConvertTo-Json -Depth 4 | Tee-Object -FilePath "$Artifacts/displays-after.json"
if (@($after | Where-Object { $_.Primary -and $_.Description -match 'NVIDIA' }).Count -ne 1) {
    throw 'The NVIDIA display did not become primary.'
}
