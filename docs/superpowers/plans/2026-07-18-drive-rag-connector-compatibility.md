# Drive RAG Connector Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Drive RAG safely index connector-discovered files when folder completeness cannot be proven, while using connector revision history for stable artifact fencing.

**Architecture:** Extend the existing schema-1 manifest with explicit coverage state. Reuse the current complete sync engine, but make planning and manifest construction coverage-aware so partial runs can only upsert and can never delete. Keep connector normalization and revision calls in the mandatory Skill contract because connector calls are model-orchestrated.

**Tech Stack:** Python 3.11+, pytest, ChromaDB, FastEmbed, Codex custom-agent TOML, Google Drive connector.

## Global Constraints

- Google Drive remains read-only.
- Partial inventories never authorize deletion.
- `currentRevisionId` is opaque and must match before and after materialization.
- Query evidence always contains Drive URL, path, revision, locator, content hash, distance, and coverage.
- Existing schema-1 journals and manifests fail closed rather than being guessed.

---

### Task 1: Add regression tests

**Files:**
- Create: `agents/drive-rag/tests/conftest.py`
- Create: `agents/drive-rag/tests/support.py`
- Create: `agents/drive-rag/tests/test_partial_inventory.py`
- Create: `agents/drive-rag/tests/test_connector_contract.py`

**Interfaces:**
- Consumes: `RemoteInventory`, `Manifest`, `plan_sync`, and `SyncEngine.apply`.
- Produces: failing tests for partial plans, partial merge, no deletion, coverage output, normalized metadata instructions, revision fallback, and native fencing.

- [ ] Write tests that construct incomplete inventories with discovered files and assert the desired partial behavior.
- [ ] Run the focused tests and require failures caused by the missing partial-index implementation.
- [ ] Add contract tests asserting `list_file_revisions`, normalized field mappings, pre/post revision equality, and `PARTIAL_INDEX` query warnings.

### Task 2: Implement deterministic partial synchronization

**Files:**
- Modify: `agents/drive-rag/skills/drive-rag/scripts/drive_rag_lib/models.py`
- Modify: `agents/drive-rag/skills/drive-rag/scripts/drive_rag_lib/inventory.py`
- Modify: `agents/drive-rag/skills/drive-rag/scripts/drive_rag_lib/sync.py`
- Modify: `agents/drive-rag/skills/drive-rag/scripts/drive_rag.py`

**Interfaces:**
- Consumes: validated complete or incomplete `RemoteInventory` objects.
- Produces: coverage-aware `Manifest`, deletion-free partial `SyncPlan`, and `PARTIAL_INDEX` sync/status envelopes.

- [ ] Add manifest `coverage` and `coverage_reason` fields with strict validation.
- [ ] Change `plan_sync` to require exact roots and freshness for every inventory, but populate deletions only when `complete` is true.
- [ ] Merge unobserved committed files into partial target state and preserve their objects/chunks.
- [ ] Emit coverage and typed partial status through CLI status, plan, apply, and query output.
- [ ] Run focused tests until green, then run all packaged deterministic tests.

### Task 3: Update connector and agent contracts

**Files:**
- Modify: `agents/drive-rag/skills/drive-rag/SKILL.md`
- Modify: `agents/drive-rag/agents/drive_rag.toml`
- Modify: `agents/drive-rag/skills/drive-rag/scripts/drive_rag_lib/schedule.py`
- Modify: `agents/drive-rag/README.md`
- Modify: `agents/drive-rag/INSTALL.md`

**Interfaces:**
- Consumes: actual connector response shapes in the defect report.
- Produces: exact model instructions for normalized metadata, revision fallback/fencing, partial inventory construction, deletion prohibition, and coverage-aware answers.

- [ ] Add `list_file_revisions(fileId=<exact ID>)` and normalized metadata mapping.
- [ ] Require equal pre/post revision IDs for every materialized artifact.
- [ ] Define partial inventory/apply/query behavior and scheduled-sync reporting.
- [ ] Validate installed agent and Skill with strict validators.

### Task 4: Verification and live acceptance

**Files:**
- Test: `agents/drive-rag/tests/`
- Runtime state: `/root/codexcode/.drive-rag` only.

**Interfaces:**
- Consumes: folder ID `1mIC560V6uUpxOgea4v94plu4zNQ6xRrO`, alias `OpenClaw docs`.
- Produces: verified indexed counts, cited query evidence, coverage state, clean journal, and read-only Drive audit.

- [ ] Run the complete deterministic suite and validators.
- [ ] Install the updated package into the active Codex home without replacing folder registry state.
- [ ] Use connector metadata, revision, listing, fetch/export, Docs/Sheets/Slides reads as applicable to build a partial inventory and artifacts.
- [ ] Apply the partial sync and require more than zero indexed files and chunks.
- [ ] Run a representative query and verify required citation and coverage fields.
- [ ] Verify `pending_journal` is false and no Drive mutation tool was called.
- [ ] Commit and push only after all available gates pass; report any live-only limitation precisely.
