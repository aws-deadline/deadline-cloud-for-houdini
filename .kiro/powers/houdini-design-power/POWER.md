---
name: houdini-design-power
version: 1.0.0
displayName: Houdini Design Power
description: Structured design assistant for deadline-cloud-for-houdini - create design documents, research Houdini APIs, and plan features
keywords:
  - houdini
  - deadline
  - design
  - architecture
  - documentation
  - research
  - submitter
  - adaptor
  - hda
author: AWS Deadline Cloud Team
---

# Houdini Design Power

Structured design assistant for the deadline-cloud-for-houdini project. Helps create design documents, research Houdini APIs, and plan features for the submitter plugin and OpenJD adaptor.

## What This Power Does

- Creates structured design documents following a consistent 4-section format
- Provides architecture context for the submitter, adaptor, and HDA
- Guides research into Houdini Python APIs and SideFX documentation
- Links to external references (GitHub repos, SideFX docs, AWS docs)

## When to Use This Power

Use this power when you need to:
- Design a new feature for the submitter or adaptor
- Write a design document for a code change
- Research Houdini APIs before implementation
- Understand the existing architecture before modifying it
- Plan changes to the HDA parameter interface

## Quick Start

1. **Start a design:** "Create a design document for [feature description]"
2. **Research an API:** "How does Houdini's [API] work? Show me code examples."
3. **Understand architecture:** "Explain how the submitter generates job templates"
4. **Plan a change:** "I need to add [parameter] to the submitter. What files need to change?"

## Design Document Sections

Every design document follows this structure:

1. **Problem Statement** — What problem are we solving? Who is affected?
2. **Proposed Solution** — How do we solve it? What changes are needed?
3. **Implementation Plan** — Step-by-step plan with file changes and test strategy
4. **Alternatives Considered** — What other approaches were evaluated and why were they rejected?

## Architecture Context

### Submitter
- Houdini plugin loaded via package system (JSON)
- ROP node defined as an HDA with DialogScript and PythonModule
- Core logic in `submitter.py`, `_assets.py`, `queue_parameters.py`
- Generates OpenJD job templates from ROP node parameters

### Adaptor
- pip-installable package with `houdini-openjd` CLI
- Implements OpenJD lifecycle: `on_start`, `on_run`, `on_stop`, `on_cleanup`
- Launches hython and communicates via socket (HoudiniClient)
- JSON schemas define `init_data` and `run_data` contracts

### HDA
- Located at `otls/deadline_cloud.hda/Driver_1deadline__cloud/`
- DialogScript defines the parameter interface
- PythonModule contains callback functions
- Edited via Houdini's Asset Manager UI
