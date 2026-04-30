# Design Workflow

Step-by-step workflow for designing features in deadline-cloud-for-houdini.

## Step 1: Understand the Problem

Before writing any design, answer these questions:
- What user problem are we solving?
- Which component is affected? (submitter, adaptor, HDA, or multiple)
- What does the user's workflow look like today?
- What should it look like after the change?

Read the relevant source code:
- Submitter: `src/deadline/houdini_submitter/python/deadline_cloud_for_houdini/`
- Adaptor: `src/deadline/houdini_adaptor/`
- HDA: `src/deadline/houdini_submitter/otls/deadline_cloud.hda/Driver_1deadline__cloud/`

## Step 2: Research

### Houdini APIs
- Check SideFX documentation: https://www.sidefx.com/docs/houdini/hom/hou/
- Test API calls in Houdini's Python Shell (Windows → Python Shell)
- Use hython for headless testing: `hython -c "import hou; ..."`

### Deadline Cloud APIs
- Check the `deadline` package source for available client methods
- Review OpenJD job template specification
- Check `openjd-adaptor-runtime` for adaptor lifecycle contracts

### Existing Patterns
- Look at how similar features work in other DCC integrations:
  - [deadline-cloud-for-maya](https://github.com/aws-deadline/deadline-cloud-for-maya)
  - [deadline-cloud-for-blender](https://github.com/aws-deadline/deadline-cloud-for-blender)
  - [deadline-cloud-for-3ds-max](https://github.com/aws-deadline/deadline-cloud-for-3ds-max)

## Step 3: Write the Design Document

Follow the 4-section template in `design-doc-structure.md`:
1. Problem Statement
2. Proposed Solution
3. Implementation Plan
4. Alternatives Considered

Save to `docs/design/your-feature-name.md`.

## Step 4: Identify Affected Files

For a typical submitter feature, you'll likely touch:

| Change Type | Files |
|-------------|-------|
| New parameter | HDA `DialogScript`, `submitter.py` |
| Asset detection | `_assets.py` |
| Queue parameters | `queue_parameters.py` |
| Job template | `submitter.py` |
| UI change | `houdini_submitter_widget.py`, HDA `DialogScript` |
| Adaptor behavior | `adaptor.py`, `houdini_handler.py` |
| Adaptor schema | `schemas/init_data.schema.json` or `schemas/run_data.schema.json` |

## Step 5: Plan Tests

Every feature needs:
- **Unit tests** — Test the logic in isolation using `mock_hou.py`
- **Integration tests** — Test end-to-end with a real Houdini scene (if the feature affects submission or rendering)

For submitter changes:
- Add tests in `test/unit/deadline_submitter_for_houdini/`
- Add a test scene in `test/integ/test_scripts/` if needed

For adaptor changes:
- Add tests in `test/unit/deadline_adaptor_for_houdini/`
- Update adaptor integration tests if the lifecycle changes

## Step 6: Review and Iterate

1. Share the design document with the team
2. Get feedback on the approach
3. Revise based on feedback
4. Get approval before implementing

## Step 7: Implement

Follow the implementation plan from the design document. Commit in logical steps:
1. Add/modify the HDA parameter interface
2. Implement the core logic
3. Add unit tests
4. Add integration tests
5. Update documentation

## Common Design Patterns

### Adding a New Submitter Parameter
1. Add parameter to HDA DialogScript (via Houdini UI)
2. Read parameter value in `submitter.py`
3. Map to OpenJD job template field
4. Add unit test with mock parameter value
5. Add integration test scene that uses the parameter

### Modifying Asset Detection
1. Identify the new file type or reference pattern
2. Add detection logic to `_assets.py`
3. Handle Houdini path variables (`$HIP`, `$JOB`)
4. Add unit tests with mock scene data
5. Add integration test with a scene containing the new reference type

### Changing Adaptor Behavior
1. Update JSON schema if init_data or run_data changes
2. Modify `adaptor.py` lifecycle methods
3. Update `houdini_handler.py` if new commands are needed
4. Add unit tests for the new behavior
5. Test with a real job submission
