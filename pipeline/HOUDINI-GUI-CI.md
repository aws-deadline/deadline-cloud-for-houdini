# Houdini Windows GUI CI

`gui_windows.yml` runs the actual Houdini FX 22.0.368 GUI and the existing
non-GUI baseline on `aws-deadline_windows-2025_gpu-t4-4-core`. It runs on
relevant mainline pushes, manual dispatch, or a reusable workflow call. Private
execution is limited to the canonical repository on mainline, never pull-request or
fork code. An optional full source SHA must be an ancestor of the triggering
trusted commit. The triggering commit supplies the test harness; artifacts
record production and harness SHAs separately.

## Verified baseline

[Run 45525692974](https://github.com/aws-deadline/deadline-cloud-for-houdini/actions/runs/45525692974),
at source/harness `81011443877f4ec0f65e33e0410bcc0bb97c714a`, passed 6 GUI and
9 non-GUI cases with zero failures, errors or skips. The runtime was Houdini FX
22.0.368 (Commercial), embedded Python 3.13.10, Qt 6.8.3, Windows Server 2025
Datacenter 10.0.26100 build 26100, and image `20261116.315.1`.

The GUI cases drive the legacy ROP dialog, rather than the current Python Panel
entry point. They verify actual viewport rendering, two exported bundles with local
OpenJD render/image comparisons, default and edited mock submissions, native
unsaved-scene cancellation and retry, and missing-input rejection. Three mock
jobs were created; no real AWS jobs were submitted. See the
[GUI test README](../test/houdini_submitter_ui/README.md) for assertions and setup.

## Graphics preparation

The Azure `Standard_NC4as_T4_v3` runner image initially had a Tesla T4 with
no NVIDIA driver. `prepare-houdini-azure-graphics.ps1` installs the hash-pinned,
NVIDIA-signed Microsoft Azure GRID 596.86 package only when no NVIDIA driver
exists. It validates existing drivers without replacing them: signed Tesla T4,
Windows driver 32.0.15.9686, NVIDIA version 596.86 and current/pending WDDM.
Unexpected or unhealthy drivers fail. `-ValidateOnly` checks an image without
installing software. No reboot or persistent image change is performed.

Driver installation alone left Hyper-V as the primary display and the actual
Houdini GUI still reported an OpenGL 3.3 error.
`select-houdini-gpu-display.ps1` makes the NVIDIA display primary for the
job's desktop. Both steps were required on this image. Do not run graphics
preparation on a shared workstation.

The standard Windows runner and isolated software graphics trials did not pass
the actual licensed GUI baseline. The T4 runner passed native viewport captures,
submitter interactions and local renders. A standalone graphics probe or hython
checkout is not evidence of GUI compatibility. SideFX's
[Houdini 22 requirements](https://www.sidefx.com/Support/system-requirements/)
exclude Windows Server; this result establishes empirical baseline compatibility,
not vendor OS support.

One attempt timed out waiting for signed-driver WMI discovery before tests ran.
A fresh runner retry with unchanged source and guards passed. Runner-image driver
readiness remains a reliability limitation; a prepared image can remove per-run
installation once the image owner supplies a validated image.

## Installer and developer licensing

The job acquires the repository-pinned scanned installer, verifies the expected
S3 bucket owner and SHA256, and installs into fresh runner storage. Verified
shared SHFS resources are copied from its scanned installer manifest. Dependencies
live outside embedded Python and user preferences. The vendor installer can still
create shared Windows components despite `/InstallLicense=No`; use disposable
runners only.

Required repository secrets are `AWS_OIDC_ROLE_ARN`, `AWS_REGION`,
`INSTALLER_BUCKET`, `INSTALLER_BUCKET_EXPECTED_OWNER`, `LICENSE_ROLE_ARN` and
`BASTION_INSTANCE_TAG`. The OIDC/private-resource opt-in must restrict trust to
the canonical repository's approved branches.

`windows-license-tunnel.ps1` chains short-lived OIDC credentials into the approved
license role and opens an owned SSM forward through
`DccInteg-PortForwardToLicenseServer`. Remote SideFX port 1715 is forwarded to a
fresh local loopback port, avoiding any existing local listener. Tunnel startup,
protocol verification and tests stay in the same workflow step: Windows terminates
step-owned children between steps, which previously broke the license transport.

`run-houdini-gui-windows.ps1` records the existing hserver route, temporarily
sets it to the owned loopback tunnel, verifies it, and restores the original
route in `finally` and an unconditional cleanup step. This requires explicit
authorization on a shared workstation. A listening socket alone is insufficient:
preflight checks the SideFX protocol and actual Houdini license checkout before
pytest. Setup failures execute no test cases and must not be reported as skips.

## Isolation and artifacts

Test processes receive fake credentials and loopback mock configuration; real
AWS/OIDC credentials are removed from their environment. The actual GUI uses
production submitter controls and rejects non-loopback AWS calls. Submitted jobs
stay on the validating mock backend with S3 job attachments disabled. Exported
bundles render locally, never on an AWS fleet.

Windows Job Objects own descendant processes; the GUI starts with a medium
integrity token. Preferences, history, dependency and OpenJD session storage are
isolated. Processes close before owned session storage is removed. Cleanup
restores the license route and terminates the owned SSM session/process tree.
GUI runs are serialized with the `houdini-integration-tests` concurrency group.
The existing CodeBuild integration workflow is not serialized by this group.

Artifacts retain desktop/display evidence, driver provenance, sanitized license
status, native logs, accessibility trees, screenshots, viewport captures, mock
records, bundles, local-render logs, JUnit and source/harness SHAs. The private
`houdini-windows-license` directory contains role/session details and must never
be uploaded; only its sanitized status/cleanup results enter artifacts.

## Scope and architecture gaps

This workflow covers GUI test automation. EC1/EC3 installer builds, publishing,
release scheduling, Conda readiness and shared failure ticketing remain separate
work. It does not add a release gate or replace the existing multi-platform
non-GUI integration workflow. Installer/resource opt-in and runner image/access
changes require infrastructure-owner authorization.

Only Houdini 22.0.368 was GUI-validated. The repository also pins 19.5.805,
20.0.896, 20.5.613 and 21.0.440; each needs its own Python/Qt/licensing validation.
Linux GUI validation is separate work. The modeless legacy ROP login/event-pump
behavior and node deletion while the dialog is open are not covered. Python-panel
entry points, login/pre-GUI hooks, frame/take controls, wheel
selection, attachments/host requirements and API error recovery remain coverage
gaps.

The temporary trial branch is excluded from this production workflow. Removing
its existing infrastructure OIDC opt-in remains an infrastructure-owner task;
this change does not modify IAM or repository access.
