# Houdini submitter GUI baseline

The initial Windows baseline targets Houdini FX 22.0.368 with its embedded
Python 3.13 and PySide6. It launches the actual application on an unlocked
interactive desktop, captures a native viewport flipbook, and drives the
production ROP submitter through Windows accessibility.

Two cases export the existing minimal integration scene: defaults and edited
job/scene settings. They assert bundle parameters, assets and mock API traffic,
execute every step of the exact exported bundle with local OpenJD, and compare
its images with the repository's Git LFS reference images. They create no
Deadline jobs. `test/integ` remains the separate nine-case non-GUI baseline.

Dependencies:

```powershell
python -m pip install -e . -r requirements-testing.txt -r requirements-integ-testing.txt -r requirements-gui-testing.txt
git lfs pull --include="test/integ/**"
python pipeline/prepare-houdini-gui-dependencies.py --directory C:/temporary/houdini-dependencies
```

Use fresh dependency storage. Set `HOUDINI_EXECUTABLE`, `HYTHON_EXECUTABLE`,
`HOUDINI_VERSION`, `HFS`, `SHFS` and `HOUDINI_GUI_DEPENDENCIES` to the isolated
runtime. Houdini 22 needs its verified shared SHFS resources as well as HFS.
Preserve Houdini's Qt runtime; do not install another PySide into it.

After preparing the licensed desktop, use the complete mock-only launcher:

```powershell
./pipeline/run-houdini-gui-windows.ps1 -Python C:/temporary/test-env/Scripts/python.exe -Artifacts C:/temporary/test-results
```

`RUNNER_TEMP` must point to private temporary storage. The launcher changes
the hserver route to `http://127.0.0.1:1715` and restores the observed original
route. This needs explicit authorization on a shared workstation. It is designed
for disposable CI runners. `-GuiOnly` selects the two GUI cases for diagnosis;
the CI workflow always runs both suites.

The hook supplies temporary `HOUDINI_PATH` and preferences containing
`__HVER__`. It does not belong in a user's normal Houdini profile. Native logs,
viewport image, desktop screenshot, accessibility trees, mock traffic,
local-render logs and JUnit are retained under the artifact directory.

The complete baseline passed on GitHub's
`aws-deadline_windows-2025_gpu-t4-4-core` runner with Houdini 22.0.368,
Windows Server 2025 build 26100 and image `20261116.315.1`:
[run 45299821789](https://github.com/aws-deadline/deadline-cloud-for-houdini/actions/runs/45299821789)
at source `bef4a9854c3ed31dcf994e8025e8f03704dfed33`.
Both GUI cases and all nine non-GUI cases passed, with no failures, errors or
skips. Actual viewport capture, bundle export, local render comparisons,
Commercial license checkout and owned-process/tunnel cleanup passed.

That fresh Azure `Standard_NC4as_T4_v3` VM initially had no NVIDIA driver.
The workflow installs the hash-pinned, NVIDIA-signed Microsoft Azure GRID
596.86 package only when no NVIDIA driver is installed. It reuses an existing
driver only after verifying the signed Tesla T4 driver version 32.0.15.9686
and NVIDIA's reported version 596.86 with current and pending WDDM mode.
Unexpected or unhealthy drivers fail validation without being replaced.
`prepare-houdini-azure-graphics.ps1 -ValidateOnly` performs this preflight
without permitting installation; image owners can use it once the driver
is included in the runner image.

The workflow then selects the NVIDIA display as primary using Windows'
display configuration API. Driver installation alone left Hyper-V primary
and Houdini still reported an OpenGL 3.3 error. Both preparation steps are
needed for this tested image. They are restricted to the disposable GPU
trial runner; do not run them on a shared workstation. No reboot or persistent
runner-image change was needed. Standard Windows runner trials and the
isolated software graphics alternatives did not pass the actual GUI baseline.

SideFX's [Houdini 22 system requirements](https://www.sidefx.com/Support/system-requirements/)
exclude all Windows Server versions. This successful Server 2025 run establishes
compatibility for this baseline, not vendor OS support. An older Server 2019
image would not resolve that support gap.

This initial baseline does not claim complete GUI coverage. Python-panel
frame/take controls, the adaptor-wheel picker, attachment/host controls,
mocked submission and error recovery still need cases. Other repository-pinned
Houdini versions need their own embedded-Python/Qt bootstrap. Mainline/release
gating remains outside this GUI trial.
