# Houdini Windows GUI CI

No standard GitHub Windows Houdini GUI result or GPU requirement has been established.
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

The first trial targets `windows-2025` and the real Houdini GUI. The existing
`hython` integration tests do not establish desktop or graphics compatibility.
Keep Deadline submissions on the local test-fixtures mock and run exported
bundles locally through the adaptor.

## Developer licensing

`windows-license-tunnel.ps1` follows the existing DCC CI pattern: short-lived
GitHub OIDC credentials, role chaining, and SSM forwarding through the approved
`DccInteg-PortForwardToLicenseServer` document. SideFX uses local port 1715.
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
