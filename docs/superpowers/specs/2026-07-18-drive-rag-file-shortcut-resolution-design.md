# Drive RAG File Shortcut Resolution Design

## Objective

Resolve Google Drive file shortcuts when the connector exposes validated
`shortcutDetails`, index the target content once, and preserve each shortcut's
monitored-folder path and URL in retrieval evidence. Folder shortcuts remain
out of scope. Existing mirrors, manifests, and ChromaDB data must migrate in
place without clearing the corpus.

## Scope

Supported shortcut targets are ordinary Drive files and native Google Docs,
Sheets, and Slides. The feature does not traverse folder shortcuts, follow
shortcut chains, edit Drive, or infer a target from a shortcut name or URL.

The connector remains the only Drive authority. Resolution is available only
when shortcut metadata contains a non-empty `shortcutDetails.targetId` and
`shortcutDetails.targetMimeType` (including normalized snake-case equivalents
if the connector returns them). The target is still independently validated by
an exact metadata lookup; the shortcut's target MIME value is discovery
evidence, not final content authority.

## Considered approaches

### Canonical target with path provenance (selected)

Use the target file ID and revision as the content identity. Merge direct and
shortcut-derived paths into that target, materialize and embed it once, and
create retrieval records for every distinct reachable path. This avoids
duplicate downloads and embeddings while retaining useful citations.

### Shortcut as the content identity

Use each shortcut ID as a separate manifest file and attach a target ID. This
preserves paths naturally but duplicates content, chunks, and embeddings when
several shortcuts reference one target. It also makes revision and deletion
semantics misleading because the content revision belongs to the target.

### Target resolution without provenance

Replace the shortcut with the target and discard shortcut identity. This is a
small change but loses the monitored-folder path and produces citations that do
not explain how the target entered the corpus.

## Inventory model

The inventory continues to store one `RemoteFile` per canonical target ID. Its
name, MIME type, revision, modified time, checksum, size, native kind, and Drive
URL all come from exact target metadata. Its reachable paths may be direct or
shortcut-derived.

Each `RemotePath` gains source provenance:

- `source_kind`: `direct` or `shortcut`;
- `source_file_id`: the target ID for a direct path or shortcut ID for a
  shortcut-derived path; and
- `source_drive_url`: the direct target URL or shortcut URL respectively.

For a shortcut-derived path, `parent_ids` and `parts` describe the validated
folder-to-shortcut edge and shortcut display name. The containing `RemoteFile`
describes the target. Legacy schema-1 paths without provenance are read as
direct paths whose source identity is the containing file, allowing in-place
migration. New serialized state includes explicit provenance.

Duplicate target IDs must have identical target metadata and revision. Their
distinct direct and shortcut paths are merged deterministically. Exact
duplicate occurrences of the same target, root, path, and source are collapsed.

## Connector workflow

1. Request `shortcutDetails` in the exact metadata selector and accept the
   connector's documented normalized field names.
2. Validate the shortcut ID, MIME type, parent edge, URL, modified time, and
   non-empty target ID and target MIME type.
3. Reject a folder target or another shortcut target as an unresolved
   file-shortcut occurrence; do not recurse.
4. Fetch exact metadata for the target ID. Require a supported file identity
   and readable URL. Target metadata is authoritative if the shortcut's cached
   target MIME type is stale.
5. Obtain the target revision using metadata or `list_file_revisions` exactly
   as for a directly discovered file.
6. Add a shortcut-derived path to the canonical target inventory entry.

For every planned target materialization, read both shortcut metadata and
target revision state before and after the fetch/export/structured read. Every
participating shortcut must still point to the same target with the same
validated parent edge, URL, and modified metadata, and the target revision must
remain unchanged. Any mismatch discards the staged artifact.

## Sync, mirror, and index behavior

Planning remains keyed by canonical target file ID and revision. A target
reachable directly and through any number of shortcuts therefore produces one
download/export, one extraction, one chunk set, and one embedding set. Mirror
promotion writes the verified artifact to every resolved local path, including
the shortcut names under their monitored folders.

Chroma records become path-occurrence aware. A record identity includes the
chunk ID plus a deterministic occurrence key derived from root, Drive path,
source kind, and source file ID. Every occurrence reuses the target chunk's
embedding vector. This replaces the current one-record-per-root representation,
which cannot preserve multiple shortcut paths in one configured root.

On the first successful apply after upgrade, unchanged records are
transactionally rewritten to occurrence-aware identities using their existing
embeddings. Legacy direct records infer direct provenance. No Drive download,
re-extraction, re-embedding, state reset, or corpus rebuild is required. Journal
recovery must restore either the complete old record set or the complete new
record set; mixed identities are invalid.

Complete inventories may remove a shortcut occurrence that disappeared or was
retargeted. Partial inventories never remove the old occurrence or its target;
they may add a newly observed target and must retain the partial-coverage
warning.

## Query evidence

Existing evidence fields retain target semantics:

- `file_id`, `revision`, `mime_type`, and `drive_url` identify the canonical
  content target;
- `drive_path` and `local_path` identify the monitored occurrence; and
- new `source_kind`, `source_file_id`, and `source_drive_url` fields identify
  whether that occurrence was direct or reached through a shortcut.

For shortcut evidence, the parent can cite the target URL for content and also
show the shortcut URL so the user can locate the item in the monitored folder.
Retrieval deduplication may collapse identical chunks from several occurrences
only after preserving all distinct provenance links in the bounded result.

## Failure behavior

The following conditions make coverage partial, preserve the previous committed
version of the affected occurrence, and never authorize deletion:

- shortcut details are absent, malformed, or identity-mismatched;
- the target is missing, inaccessible, a folder, or another shortcut;
- exact target metadata is unavailable or conflicts with the validated target
  ID (a stale cached `targetMimeType` alone is not a conflict);
- a shortcut is retargeted during materialization; or
- the target revision changes during materialization.

The result reports a bounded typed reason such as `SHORTCUT_UNRESOLVED`,
`SHORTCUT_TARGET_UNSUPPORTED`, or `SHORTCUT_CHANGED_DURING_SYNC`. Other
discovered, revision-fenced files may still commit through the deletion-free
partial-index path.

## Verification

Automated regression coverage must prove:

1. A PDF shortcut resolves to its target and preserves shortcut provenance.
2. Docs, Sheets, and Slides shortcuts use target PDF export plus structured
   indexing.
3. Direct and multiple shortcut paths to one target materialize and embed once
   but create evidence for every distinct path.
4. Missing shortcut details, broken targets, folder targets, and shortcut
   chains cannot authorize deletion or block safe upserts of other files.
5. Shortcut retargeting or target revision changes during materialization
   discard the staged artifact.
6. A partial inventory preserves previously committed shortcut occurrences.
7. A complete inventory removes a deleted or retargeted occurrence without
   deleting a target still reachable through another path.
8. Legacy manifests and one-record-per-root Chroma data migrate transactionally
   without downloading or re-embedding unchanged content.
9. Query evidence returns target identity, target URL, shortcut path, shortcut
   identity, shortcut URL, revision, locator, and coverage state.
10. The existing OpenClaw fixture resolves its file shortcut when the live
    connector exposes shortcut details and otherwise returns the typed partial
    reason without regressing the other indexed files.

Strict agent and Skill validators, the complete package suite, disposable
installation validation, and an isolated copy of the existing OpenClaw state
remain required before release.
