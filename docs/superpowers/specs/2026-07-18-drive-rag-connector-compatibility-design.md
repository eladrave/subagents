# Drive RAG Connector Compatibility Design

## Problem

The installed Google Drive connector returns normalized metadata, omits
`version` and `headRevisionId`, and exposes revision identity through
`list_file_revisions`. Its `list_folder` response has no pagination or explicit
completeness marker. The original Drive RAG contract therefore cannot prove a
complete inventory or revision-fence materialized files.

## Approved architecture

The connector-facing Skill will accept normalized metadata names and obtain the
authoritative revision from `list_file_revisions.currentRevisionId` whenever
metadata does not provide a revision. Every raw download, native PDF export, and
native structured read is fenced by equal non-empty pre/post revision IDs.
Revision IDs are opaque strings.

The deterministic CLI will accept incomplete inventories containing discovered,
fully identified files. A partial plan may download or update discovered files
but always has an empty deletion set. Partial apply merges those files into the
existing committed manifest and index, preserves unobserved committed files,
and records partial coverage plus the connector's bounded reason. A later
complete inventory may perform ordinary reconciliation and deletion.

## Coverage contract

The manifest records `coverage` as `complete` or `partial` and a nullable
`coverage_reason`. A complete inventory commits complete coverage with no reason.
An incomplete inventory commits partial coverage with a non-empty reason.
Status, sync output, and query output expose coverage. Query remains available
against a consistent partial index but returns `PARTIAL_INDEX` and a coverage
warning; evidence retains Drive URL, path, revision, locator, content hash, and
distance.

## Safety invariants

- A partial inventory never creates a deletion plan.
- Missing files in a partial inventory remain in the manifest, mirror, and ChromaDB.
- Fewer results than `top_k` is not proof of completeness.
- A changed pre/post revision discards the staged artifact and prevents apply.
- Google Drive operations remain read-only.
- Exact root scope, inventory timestamps, artifact identities, journal recovery,
  and index consistency checks remain mandatory.
- Explicit registry removal is not inferred from remote absence; any local purge
  remains governed by the existing registry/reconciliation workflow.

## Testing

Deterministic tests cover normalized connector fields, opaque revision fallback,
native pre/post revision fencing instructions, partial planning without deletion,
partial initial upsert, preservation of unobserved committed files, complete
inventory deletion, partial query coverage, and native PDF plus structured
indexing. The live read-only acceptance test uses the configured `OpenClaw docs`
folder and must produce indexed files/chunks plus cited evidence without Drive
mutation. Lack of task automation may remain `NOT_SCHEDULED`.
