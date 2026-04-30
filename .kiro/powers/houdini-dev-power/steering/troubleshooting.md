# Troubleshooting Guide

## Build Issues

### `hatch build` fails with version error
**Cause:** `hatch-vcs` cannot determine version from git tags.
**Fix:**
```bash
git fetch --tags
hatch build
```

### `_version.py` not found
**Cause:** The build hook hasn't run yet.
**Fix:**
```bash
hatch build  # Generates _version.py and copies to all locations
```

### Build artifacts stale
**Fix:**
```bash
rm -rf dist/ build/ *.egg-info
hatch env prune
hatch build
```

## Setup Issues

### `hatch run install` fails with pip errors
**Cause:** Network issues or invalid Houdini version string.
**Fix:**
- Verify version format: `X.Y` (e.g., `20.5`, not `20` or `20.5.487`)
- Check network connectivity for pip downloads
- Try: `hatch env prune` then retry

### Plugin not visible in Houdini after `hatch run install`
**Check:**
1. Verify the package JSON exists:
   ```bash
   # Linux
   cat ~/houdini21.0/packages/deadline_submitter_for_houdini.json
   # macOS
   cat ~/Library/Preferences/houdini/21.0/packages/deadline_submitter_for_houdini.json
   ```
2. Verify `plugin_env_21.0/` exists in the repo root and contains packages
3. Restart Houdini completely (not just reload)
4. In Houdini, check Windows → Shell and verify `PYTHONPATH` includes the plugin paths

### `plugin_env_{X.Y}/` is empty or missing packages
**Fix:**
```bash
rm -rf plugin_env_21.0/
hatch run install --houdini-version 21.0
```

## Unit Test Issues

### Tests fail with `ModuleNotFoundError: No module named 'hou'`
**Cause:** Test is trying to import `hou` without the mock.
**Fix:** Ensure `conftest.py` sets up the `mock_hou` module before any test imports. Check that `mock_hou.py` exists in the test directory.

### Tests fail with import errors for `deadline` or `openjd`
**Fix:**
```bash
hatch env prune
hatch run test
```

### Coverage below 65%
**Cause:** New code without tests, or test files not being collected.
**Check:**
- Coverage config in `pyproject.toml` under `[tool.coverage.run]`
- Source packages: `deadline/houdini_adaptor`, `deadline/houdini_submitter`
- Run with verbose coverage: `hatch run test -- --cov-report=term-missing`

### `test_copyright_headers.py` fails
**Cause:** Source file missing Apache-2.0 copyright header.
**Fix:** Add the header to the file, or run:
```bash
./scripts/add_copyright_headers.sh
```

## Integration Test Issues

### Tests fail with "queue parameters" in job template
**Cause:** Logged into Deadline Cloud Monitor during test run.
**Fix:**
```bash
deadline auth logout
hatch run integ:test
```

### `HYTHON_EXECUTABLE` not set
**Fix:** Set the environment variable to the full path of your hython binary:
```bash
# Linux
export HYTHON_EXECUTABLE="/opt/hfs<VERSION>/bin/hython"
# macOS
export HYTHON_EXECUTABLE="/Applications/Houdini/Houdini<VERSION>/Frameworks/Houdini.framework/Resources/bin/hython"
```
```powershell
# Windows
$Env:HYTHON_EXECUTABLE = "C:\Program Files\Side Effects Software\Houdini <VERSION>\bin\hython.exe"
```

### Image comparison failures
1. Run `git lfs pull` to download expected images
2. Check that the Houdini version matches what the expected images were rendered with
3. Platform differences in rendering are expected — check tolerance thresholds in `test/integ/helpers/image_comparison.py`

### Permission denied on Windows
Integration tests may need to write to Houdini's installation directory. Run PowerShell as Administrator.

### hython crashes or hangs
- Check Houdini license: `hython -c "import hou; print(hou.licenseCategory())"`
- Verify Houdini version matches `HOUDINI_VERSION` env var
- Check for conflicting Houdini environment variables

## Adaptor Issues

### `houdini-openjd` command not found
**Cause:** The adaptor package is not installed in the current environment.
**Fix:**
```bash
pip install -e .
# or
hatch shell
houdini-openjd --help
```

### Adaptor fails to launch hython
**Check:**
- `hython` is in PATH or `HYTHON_EXECUTABLE` is set
- Houdini license is valid
- The HIP file path in the job template is correct

### Adaptor socket connection errors
**Cause:** The HoudiniClient inside hython failed to connect to the adaptor.
**Check:**
- Firewall rules (the adaptor uses localhost sockets)
- hython process started successfully (check adaptor logs)

## Hatch Environment Issues

### Environments corrupted or stale
```bash
hatch env prune  # Delete all environments
hatch run test   # Recreates default environment
```

### Wrong Python version
```bash
hatch env show   # Shows which Python each environment uses
```

### `hatch shell` doesn't set expected variables
The `hatch shell` environment sets `MAYA_ENV_DIR` to `plugin_env/` — this is a legacy reference. For Houdini, use `hatch run install --houdini-version X.Y` instead.

## Platform-Specific Issues

### Linux: Houdini not found at `/opt/hfs{VERSION}/`
Houdini may be installed elsewhere. Find it:
```bash
find / -name "hython" -type f 2>/dev/null
```

### macOS: Framework path issues
The hython path on macOS is deeply nested:
```
/Applications/Houdini/Houdini{VERSION}/Frameworks/Houdini.framework/Resources/bin/hython
```

### Windows: Long path issues
Enable long paths in Windows if you encounter path length errors:
```powershell
# Run as Administrator
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

### Windows: pywin32 required for adaptor
The adaptor on Windows requires `pywin32`. After installing it into Houdini's Python site-packages, run the post-install script and grant permissions:
```powershell
# Run in elevated PowerShell after installing Houdini
& "<HOUDINI_PYTHON>/python.exe" "<HOUDINI_SITE_PACKAGES>/win32/scripts/pywin32_postinstall.py" -install
icacls "<HOUDINI_SITE_PACKAGES>/win32" /grant "<YOUR_USER>:(OI)(CI)R" /T
icacls "<HOUDINI_SITE_PACKAGES>/pywin32_system32" /grant "<YOUR_USER>:(OI)(CI)R" /T
icacls "<HOUDINI_SITE_PACKAGES>/pywin32.pth" /grant "<YOUR_USER>:R"
```
