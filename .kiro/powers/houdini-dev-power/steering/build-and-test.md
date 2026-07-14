# Build and Test Guide

## Build

### Build Wheel and Sdist
```bash
hatch build
```
Output: `dist/deadline_cloud_for_houdini-{VERSION}-py3-none-any.whl` and `.tar.gz`

### Build Adaptor Wheels
```bash
pip install build
./scripts/build_wheels.sh
```
Output: `wheels/` directory with wheels for `deadline_cloud_for_houdini`, `deadline`, and `openjd_adaptor_runtime`.

### Build Standalone Installer
```bash
hatch run installer:build-installer --local-dev --platform <PLATFORM>
# See full options: hatch run installer:build-installer -h
```
Requires InstallBuilder. Omit `--platform` to use the current platform.

## Unit Tests

### Run All Unit Tests
```bash
hatch run test
```

This runs pytest against `test/unit/` with:
- Coverage for `src/deadline/houdini_adaptor` and `src/deadline/houdini_submitter`
- Parallel execution via `pytest-xdist` (`--numprocesses=auto`)
- Coverage threshold: 65% (configured in `pyproject.toml`)
- HTML coverage report: `build/coverage/`

### Run Across All Python Versions
```bash
hatch run all:test
```
Tests against Python 3.9, 3.10, 3.11, 3.12, 3.13.

### Run Specific Test Files
```bash
hatch run test -- test/unit/deadline_submitter_for_houdini/test_assets.py
hatch run test -- test/unit/deadline_adaptor_for_houdini/unit/HoudiniAdaptor/test_adaptor.py -v
```

### Run Tests Matching a Pattern
```bash
hatch run test -- -k "test_submit"
```

## Test Structure

```
test/
├── unit/
│   ├── test_copyright_headers.py                    # Copyright header validation
│   ├── deadline_submitter_for_houdini/              # Submitter unit tests
│   │   ├── test_assets.py                           # Asset detection
│   │   ├── test_job_template.py                     # Job template generation
│   │   ├── test_submit_callback.py                  # Submit callback
│   │   ├── test_rops.py                             # ROP node tests
│   │   ├── mock_hou.py                              # Mock for hou module
│   │   └── conftest.py
│   └── deadline_adaptor_for_houdini/unit/           # Adaptor unit tests
│       ├── HoudiniAdaptor/
│       │   ├── test_adaptor.py                      # Adaptor lifecycle tests
│       │   └── test_adaptor_pathmapping.py          # Path mapping tests
│       └── HoudiniClient/
│           ├── test_client.py                       # Client tests
│           ├── test_handler.py                      # Handler tests
│           └── mock_hou.py                          # Mock for hou module
├── integ/                                           # Integration tests
│   ├── test_houdini_submitters.py                   # @pytest.mark.submitter
│   ├── test_houdini_adaptors.py                     # @pytest.mark.adaptor
│   └── test_scripts/                                # Test scene scripts
└── installer/
    └── test_installer.py                            # Installer tests
```

## Mocking the `hou` Module

The `hou` module (Houdini's Python API) is only available inside a running Houdini session. Unit tests use `mock_hou.py` to mock it:

- `test/unit/deadline_submitter_for_houdini/mock_hou.py` — Mocks for submitter tests
- `test/unit/deadline_adaptor_for_houdini/unit/HoudiniClient/mock_hou.py` — Mocks for adaptor client tests

When writing new tests that import code using `hou`, ensure the mock is set up in `conftest.py` before the import.

## Linting and Formatting

### Check Style
```bash
hatch run lint
```
Runs:
1. `ruff check .` — Linting (line-length 100, isort)
2. `black --check --diff .` — Formatting check (line-length 100)
3. `mypy src test` — Type checking

### Auto-Format
```bash
hatch run fmt
```
Runs `black .` then `ruff check .`.

## Coverage

- Minimum threshold: **65%** (fails build if below)
- Measured packages: `deadline/houdini_adaptor`, `deadline/houdini_submitter`
- Reports: terminal, HTML (`build/coverage/`), XML (`build/coverage/coverage.xml`)
- Omitted: `__main__.py`, `_version.py`

## Installer Tests

```bash
hatch run test-installer
```
Requires a built installer in the repository root. Tests are in `test/installer/test_installer.py`.

## Hatch Environments

| Environment | Purpose | Key Scripts |
|-------------|---------|-------------|
| `default` | Unit tests, lint, format | `test`, `lint`, `fmt`, `install` |
| `all` | Multi-Python matrix (3.9-3.13) | `test` |
| `integ` | Integration tests | `test`, `test_submitters`, `test_adaptors` |
| `integ-ci` | CI matrix (Houdini 19.5-21.0) | `setup`, `test` |
| `installer` | Build standalone installer | `build-installer` |
| `release` | Semantic release | `bump`, `version` |

## Troubleshooting Build/Test Issues

### Tests not picking up code changes
```bash
hatch env prune
hatch run test
```

### `_version.py` not found
```bash
hatch build  # Generates _version.py via hatch-vcs
```

### Coverage report shows wrong files
Coverage is configured in `pyproject.toml` under `[tool.coverage.run]` and `[tool.coverage.paths]`. Source packages: `deadline/houdini_adaptor`, `deadline/houdini_submitter`.
