# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Uses the Windows input-desktop check from the Nuke GUI CI investigation.
# Run this in the same session that will launch Houdini and drive its controls.
$ErrorActionPreference = 'Stop'
$sessionId = (Get-Process -Id $PID).SessionId
Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber | Format-List
Get-CimInstance Win32_VideoController |
    Select-Object Name, DriverVersion, CurrentHorizontalResolution, CurrentVerticalResolution |
    Format-List
Write-Output "Process session: $sessionId"
Write-Output "UserInteractive: $([Environment]::UserInteractive)"

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HoudiniGuiDesktopProbe {
    [DllImport("user32.dll", SetLastError = true)]
    static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll", SetLastError = true)]
    static extern bool SwitchDesktop(IntPtr desktop);
    [DllImport("user32.dll")]
    static extern bool CloseDesktop(IntPtr desktop);
    public static bool Available() {
        IntPtr desktop = OpenInputDesktop(0, false, 0x0100);
        if (desktop == IntPtr.Zero) return false;
        try { return SwitchDesktop(desktop); }
        finally { CloseDesktop(desktop); }
    }
}
'@
$available = [HoudiniGuiDesktopProbe]::Available()
Write-Output "Unlocked input desktop available: $available"
if ($sessionId -eq 0 -or -not [Environment]::UserInteractive -or -not $available) {
    throw 'Houdini GUI tests require an unlocked interactive desktop outside Session 0.'
}
Write-Output 'Desktop preflight passed; actual Houdini graphics and GUI licensing still require verification.'
