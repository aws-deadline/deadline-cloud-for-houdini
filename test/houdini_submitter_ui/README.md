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

This initial baseline does not claim complete GUI coverage. Python-panel
frame/take controls, the adaptor-wheel picker, attachment/host controls,
mocked submission and error recovery still need cases. Other repository-pinned
Houdini versions need their own embedded-Python/Qt bootstrap. Mainline/release
gating awaits the first standard Windows CI result.
