# Houdini Windows GUI CI

The standard GitHub Windows trial has failed during licensing and GUI startup;
the GPU requirement has not yet been established.
The initial real-GUI export/render baseline has been exercised on an authorized
Windows Server 2022 workstation with Houdini FX 22.0.368 and an A10G.

The final workstation baseline passed on 2026-11-13: 2 GUI cases and 9
non-GUI cases, with zero failures, errors or skips, in 336.30 seconds.
It used Windows Server 2022 Datacenter 10.0.20348, embedded Python 3.13.10,
Qt 6.8.3 and test-runner Python 3.12.10 against production source
`0f808859b2156f272c984e6e7010458449ebdef6`. Actual Commercial GUI checkout,
native viewport capture, mock-only requests, exported-bundle local renders,
license restoration and test-owned process/storage cleanup were verified.
This workstation result does not validate `windows-2025`.

The first GitHub trial on 2026-11-14, [run 43531286558](https://github.com/aws-deadline/deadline-cloud-for-houdini/actions/runs/43531286558),
tested source/harness `e7116a46ef9de99f4c0ccbcab0eb8690940aa3a6`.
Its independent `ubuntu-latest` availability job passed (Ubuntu 26.04.1,
image 20261111.196.1). The `windows-2025` job used Windows Server 2025
Datacenter 10.0.26100, image 20261103.298.1, with Hyper-V Video driver
10.0.26100.1150. Session 2 was interactive and its input desktop unlocked.
Dependency preparation passed, but AWS credential setup failed because
`AWS_REGION` was unavailable. Houdini installation, licensing and all GUI
and non-GUI test cases were not executed; there are no pytest counts for
this run. This is a configuration prerequisite, not a graphics failure.

The second attempt on 2026-11-16 passed OIDC, desktop/dependency checks, installer
SHA256 verification and installation of Houdini 22.0.368. It stopped before
license role assumption because local port 1715 was occupied. The job did not
record the listener's owner. Neither suite ran, and no graphics conclusion can
be drawn. The tunnel now allocates its own local port while retaining remote
port 1715; the launcher consumes that port and restores the original route.

The next [run, 43979672955](https://github.com/aws-deadline/deadline-cloud-for-houdini/actions/runs/43979672955),
tested source/harness `51a1190b1a72c6ff96b6a83515331ade944fa7c2` on
Windows Server 2025 Datacenter 10.0.26100, build 26100, using `windows-2025`
and Houdini 22.0.368. Its Linux availability job passed. OIDC, installer
verification/installation and the local SSM listener passed; the listener used
port 50813. The complete baseline ran: GUI 0 passed, 0 failed, 2 errors, 0 skipped;
non-GUI 0 passed, 9 failed, 0 errors, 0 skipped. The non-GUI applications reported
that no licenses could be found. Both GUI fixtures timed out, and their desktop
screenshots show Houdini License Administrator 22.0.368 reporting that the
installed driver cannot run OpenGL 3.3. The desktop was interactive and unlocked,
with Hyper-V Video driver 10.0.26100.1150. License-route restoration and owned
process/tunnel cleanup passed.

This demonstrates a graphics startup failure in License Administrator, but does
not establish compatibility of a licensed Houdini submitter GUI. The launcher
now verifies the applied hserver route and performs an actual, bounded hython
license checkout before pytest. Preflight failures retain sanitized diagnostics
and restore the route; they must be reported as setup failures with no pytest
counts, not as passing or skipped tests.

Read-only inspection of the working workstation's existing SSH configuration
confirmed that its port 1715 forward targets the dedicated SideFX host. The CI document's
destination still needs verification: `ssm:GetDocument` through the existing
developer port-forwarding role was denied. Do not change the document, role,
server configuration or entitlements without authorization.

The diagnostic [run 43987277250](https://github.com/aws-deadline/deadline-cloud-for-houdini/actions/runs/43987277250)
on 2026-11-17 tested source/harness
`3092ae2b9809d6f420bd2253f91e739e7b5e2ebc` with the same OS, Houdini build
and runner label. OIDC, installation, unlocked desktop and SSM listener checks
passed. `license-route.json` confirms hserver retained the requested loopback
route on port 50263. `license-preflight.json` records actual hython exit code 3
and no successful license checkout. Setup failed before pytest: zero GUI or
non-GUI test cases executed, with no pytest pass/fail/error/skip results. Original
route restoration and owned tunnel cleanup passed. Further baseline execution
is blocked on a usable SideFX license through this CI path; verifying or fixing
the remote document target and server entitlements requires the licensing owner.

For a subsequent isolated software-graphics trial, Mesa documents
[LLVMpipe on Windows](https://docs.mesa3d.org/drivers/llvmpipe.html): an
application-local `opengl32.dll` and `libgallium_wgl.dll` can provide software
OpenGL without replacing the system graphics driver. That trial still requires
a pinned, verified build and an actual licensed Houdini GUI run, including
viewport capture and the complete baseline. It has not been executed.
The current [SideFX Houdini 22 requirements](https://www.sidefx.com/Support/system-requirements/)
require GL 4.0 and say lower than GL 3.3 will not run; they do not support Windows
Server. The current environment-variable reference does not document
`HOUDINI_OGL_SOFTWARE` as a supported fallback. Neither those requirements nor
an independent OpenGL probe should substitute for the requested GUI trial.

The first trial targets `windows-2025` and the real Houdini GUI. The existing
`hython` integration tests do not establish desktop or graphics compatibility.
Keep Deadline submissions on the local test-fixtures mock and run exported
bundles locally through the adaptor.

## Developer licensing

`windows-license-tunnel.ps1` follows the existing DCC CI pattern: short-lived
GitHub OIDC credentials, role chaining, and SSM forwarding through the approved
`DccInteg-PortForwardToLicenseServer` document. SideFX uses remote port 1715.
The hosted tunnel allocates a separate local loopback port and passes it to
the launcher through `HOUDINI_GUI_LICENSE_PORT`, preserving any existing
listener on local port 1715.
It does not configure Houdini, install a license, or change the workstation's
license-server settings.

The job supplies these values through secrets:

| Variable | Purpose |
| --- | --- |
| `AWS_REGION` | Region containing the licensing bastion |
| `LICENSE_ROLE_ARN` | Approved role for the developer-license tunnel |
| `BASTION_INSTANCE_TAG` | Name tag identifying the licensing bastion |
| `RUNNER_TEMP` | GitHub's per-job temporary directory |

The initial OIDC role needs the repository-specific private-resource opt-in.
Trust must be restricted to the canonical repository's approved CI branches.
Houdini branch creation is restricted; this trial reuses an existing branch. Infrastructure
onboarding must set `gitHubRunnerTestBranch: 'fix-publish-python-version'` when
authorized to enable private-resource access.
Do not expose these resources to forks or pull-request code.

Start the tunnel before launching Houdini:

```powershell
./pipeline/windows-license-tunnel.ps1
```

Always run cleanup, including after setup or test failures:

```powershell
./pipeline/windows-license-tunnel.ps1 -Cleanup
```

Only upload `houdini-windows-license-status.txt` from `RUNNER_TEMP`. The
`houdini-windows-license` directory contains private role and SSM details and
must not enter test artifacts. Cleanup terminates the owned SSM session and
process tree before deleting its temporary configuration.

A listening port proves tunnel readiness only. Verify an actual Houdini GUI
license checkout before attributing startup failure to graphics. Keep the
licensed DCC alive until GUI diagnostics and tests finish, then stop its owned
process tree before closing the tunnel.

## Workflow and evidence

`gui_windows.yml` provides manual and reusable execution on `windows-2025`.
Changing this workflow on `fix-publish-python-version` also starts the initial trial,
before GitHub registers the new dispatch workflow on mainline.
An independent `ubuntu-latest` job records Linux runner availability and its
OS/source metadata; it runs no Houdini test cases and uses no private resources.
Private execution is limited to the canonical repository on `mainline` or
`fix-publish-python-version`. Its optional exact source SHA must belong to that trusted
branch history. The reviewed test harness is retained when testing an earlier
production revision; source and harness SHAs are reported separately.

The job acquires the repository-pinned scanned installer, checks its expected
bucket owner and SHA256, installs into fresh storage, prepares external Python
dependencies, starts the SideFX tunnel and runs both suites. It serializes
its own licensed runs, strips real AWS/OIDC credentials from test processes, supplies
isolated mock configuration, owns descendant processes with Windows Job Objects,
restores the original license route and unconditionally closes the tunnel.
OpenJD session storage is confined to this run's private directory and removed
after the process job closes, including retries for Windows file-handle races.
Reference images require Git LFS, including when testing an earlier revision.

`install-houdini-gui-windows.py` does not alter the embedded Python environment
or install the submitter into a user's preferences. The vendor installer can
still create shared Windows components despite `/InstallLicense=No`; use it on
disposable runners. Houdini 22's required SHFS resources are copied into the
test root only after verification against its scanned installer manifest.

On the workstation, process-local `SESI_LMHOST` did not redirect the shared
hserver service. A separate non-service client was rejected while that service
was installed. An explicitly authorized temporary `hserver -S` route change
then enabled actual Commercial GUI checkout; the original online route was
restored. The CI launcher includes restoration both in `finally` and in a
separate unconditional cleanup step.

The two GUI cases use accessible production controls to export a real scene,
assert the bundle and mock traffic, then render that exact bundle locally and
compare its images. Native viewport capture is performed inside the actual
licensed GUI. A viewport or independent graphics probe alone cannot substitute
for these submitter interactions.

## Remaining validation and architecture gaps

- Private installer/licensing resource opt-in and approved CI branch access are
  external prerequisites. Do not change IAM, secrets or access while testing.
- The initial GitHub workflow reached desktop/dependency preflight, then stopped
  at missing AWS region configuration. Actual standard-runner Houdini execution
  is required before requesting GPU access. Do not treat the A10G workstation's
  success as evidence that a GPU is required.
- The tested baseline is intentionally limited to Houdini 22.0.368; the existing
  repository pins 19.5.805, 20.0.896, 20.5.613 and 21.0.440 as well. Their Python
  and Qt bootstrap and licensing need separate validation.
- Remaining GUI coverage includes Python-panel frame/take controls, wheel
  picker, attachments, host requirements, mocked submission and recovery.
- Mainline/exact-release gating and shared failure ticketing await live CI
  validation and the approved shared mechanism. Installer builds, publishing,
  weekly release scheduling and Conda readiness belong to separate release work.

Artifacts include OS/display/desktop evidence, native application logs,
accessibility dumps, actual screenshots/viewport images, mock request counts,
local render logs, JUnit, and both source SHAs. Private SSM configuration and
connection logs are excluded from uploads.
