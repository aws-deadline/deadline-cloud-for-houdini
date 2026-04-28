# Design Document Structure

Every design document for deadline-cloud-for-houdini follows this 4-section format.

## Template

```markdown
# Design: [Feature Name]

**Author:** [Name]
**Date:** [YYYY-MM-DD]
**Status:** Draft | In Review | Approved | Implemented

## 1. Problem Statement

What problem are we solving? Who is affected? What is the current behavior
and what is the desired behavior?

Include:
- User story or use case
- Current limitations or pain points
- Impact if not addressed

## 2. Proposed Solution

How do we solve it? Describe the approach at a level of detail sufficient
for another engineer to implement it.

Include:
- High-level design
- File changes required (list specific files)
- API or interface changes
- Data flow changes
- Configuration changes (HDA parameters, JSON schemas, etc.)

## 3. Implementation Plan

Step-by-step plan for implementing the solution.

Include:
- Ordered list of changes
- Test strategy (unit tests, integration tests, manual testing)
- Migration or backward compatibility considerations
- Rollout plan (if applicable)

## 4. Alternatives Considered

What other approaches were evaluated? Why were they rejected?

For each alternative:
- Brief description
- Pros and cons
- Reason for rejection
```

## Guidelines

### Scope
- One design document per feature or significant change
- Small bug fixes do not need a design document
- Changes to the HDA parameter interface always need a design document

### File Location
Store design documents in `docs/design/`:
```
docs/design/
├── usd-scene-dependency-detection.md   # Existing example
└── your-feature-name.md
```

### Level of Detail
- **Problem Statement:** Concrete, with examples. "Users cannot submit USD scenes with external references" is better than "USD support is incomplete."
- **Proposed Solution:** Specific enough to implement. Name the files, functions, and parameters that change.
- **Implementation Plan:** Ordered steps. Each step should be a single commit or PR.
- **Alternatives:** At least one alternative, even if it's "do nothing."

### Review Process
1. Write the design document
2. Share with the team for review
3. Iterate based on feedback
4. Mark as "Approved" before starting implementation
5. Update status to "Implemented" when done

## Existing Design Documents

Reference existing designs in `docs/design/` for style and depth:
- `usd-scene-dependency-detection.md` — Example of a well-scoped design for asset detection
