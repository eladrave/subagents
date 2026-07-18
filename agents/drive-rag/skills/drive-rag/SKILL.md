---
name: drive-rag
description: Use when onboarding or managing Google Drive RAG folders, synchronizing their verified local mirror and ChromaDB index, or retrieving cited evidence from the configured corpus. Do not use for Drive editing or unrelated questions.
---

# Drive RAG

This Skill is mandatory for the `drive_rag` custom agent. The Google Drive
connector supplies authenticated read access; the bundled Python program owns
all deterministic local state. Never substitute a local Google client, another
authentication flow, or a guessed connector result.

## Fixed runtime contract

Use these paths exactly:

```text
Python:     /opt/drive-rag/venv/bin/python
CLI:        /root/.codex/skills/drive-rag/scripts/drive_rag.py
State root: /root/codexcode/.drive-rag
```

Invoke commands as argument arrays equivalent to the examples below. Quote
every path and value; never use `eval`, command-string construction, or source
text as a shell argument. Every CLI result is one JSON object. Continue only
when:

- the exit status is zero;
- stdout parses as exactly one JSON object;
- `schema_version` is `"1"`;
- `operation` is the requested operation;
- `status` is expected for that operation; and
- the input identity and returned run, file, revision, hash, path, and output
  identities match the request and the validated preceding result.

A nonzero exit or missing, malformed, stale, or identity-mismatched result is a
hard stop. Do not infer success from files appearing on disk.

Do not write document bodies to logs. Do not print raw connector responses,
downloaded content, structured native content, extracted text, embeddings, or
retrieved excerpts as diagnostic output. Operational reporting may contain
only bounded status codes, counts, file/root IDs, names, paths, revisions,
sizes, hashes, and warnings. Excerpts belong only in the final cited query
handoff.

## Required inputs and modes

Accept exactly one mode and an invocation context, `interactive` or
`unattended`:

- `onboard`: optional one or more Drive folder URLs and optional aliases;
- `status`: no additional input;
- `sync`: all enabled configured folders;
- `query`: the exact question and optional repeated folder aliases; or
- an explicit registry action: list, add, enable, disable, rename, or remove.

The state root is fixed. Do not accept a user-selected alternate state path.
Folder aliases are local labels; Drive folder IDs are the durable identities.
A scheduled run is always unattended. If the parent did not explicitly provide
context, treat a live user question as interactive only when the parent says it
is live; otherwise use the unattended fail-closed branch.

## Capability and authentication gate

Before the first connector read, inspect the tools actually available to this
agent. Required Google Drive connector capabilities are:

- `list_folder(url=<canonical folder URL>, top_k=<bounded limit>)` for folder
  validation and recursive inventory;
- `get_file_metadata(fileId=<exact file ID>, fields="id,name,mimeType,modifiedTime,version,headRevisionId,md5Checksum,size,parents,webViewLink")`
  for authoritative file/folder identity, ancestry, and available metadata;
- `list_file_revisions(fileId=<exact file ID>)` for authoritative opaque
  `currentRevisionId` fallback and pre/post materialization revision fencing;
- `fetch(url=<canonical Drive file URL>, download_raw_file=True)` for
  non-native file bytes;
- `export_file(id=<file ID>, mime_type="application/pdf")` for native PDF
  exports;
- `get_document` for Google Docs structured content;
- `get_presentation` for Google Slides structured content; and
- `get_spreadsheet_metadata` plus either bounded
  `get_spreadsheet_range` or `get_spreadsheet_cells` calls for Google Sheets.

If a capability required by the requested operation or an encountered native
type is not visible, return `CONNECTOR_UNAVAILABLE` with the missing tool names
and stop without changing configuration, mirrors, manifests, or ChromaDB. If
the connector reports missing or expired authorization, return
`CONNECTOR_AUTH_REQUIRED` and the minimum reconnect action. Never request or
handle credentials yourself.

Connector tools are read-only in this workflow. Never call Drive create,
update, move, share, or delete operations.

## Status and recovery gate

Start every mode by running:

```bash
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag status
```

Then inspect the validated cached schedule claim without exposing the prompt:

```bash
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag schedule show
```

Validate operation `schedule.show`, status `ok`, and `configured`. When true,
require the exact cached `task_id`, RRULE, project mode/path, enabled state, and
prompt SHA-256. For onboarding, status, and sync, query task management by that exact task ID and validate the live RRULE, local project mode and path, enabled state, and exact prompt. The cached record is not proof that the task still exists. Query mode may continue against an otherwise fresh index when task management is unavailable, but must carry a `NOT_SCHEDULED` freshness warning rather than claim a live schedule.

For onboarding or sync, if status reports a pending journal, run and validate:

```bash
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag sync recover
```

Then run status again. Do not begin an unrelated sync while recovery remains
pending. Query mode does not recover implicitly; it returns `INDEX_STALE` when
a journal is pending or the committed state is inconsistent.

## Interactive onboarding

When status returns `CONFIGURATION_REQUIRED` for an ordinary interactive
Drive-relevant query, ask the parent or user for one or more Google Drive folder
URLs and optional aliases, then enter onboarding using the sequence below. Do
not stop at the status code for this interactive fallback. For scheduled sync
or any other unattended invocation, return `CONFIGURATION_REQUIRED` without
prompting and without mutation.

The interactive onboarding workflow follows this sequence:

1. Run `registry list` and validate the schema-1 result.
2. If folders already exist and no add request was made, show aliases, enabled
   state, and content-free status; do not reconfigure them implicitly.
3. If none exist, ask the user for one or more Google Drive folder URLs and
   optional aliases in one concise prompt. Do not guess a folder from recent
   Drive files or search results.
4. Parse the folder ID from each canonical folder URL. Reject non-Drive URLs,
   missing IDs, file URLs, duplicate IDs, and ambiguous inputs.
5. Call `get_file_metadata` with the exact parsed folder ID and exact fields
   selector from the capability gate. Require matching `id`, folder MIME type
   `application/vnd.google-apps.folder`, canonical `webViewLink`, readable
   identity fields, and an expected parent shape. Then call
   `list_folder(url=<canonical folder URL>, top_k=<bounded limit>)` using the
   supplied canonical folder URL only to establish child access and listing
   completeness. This validation is not yet a full sync and must not register
   inaccessible roots.
6. Derive a missing alias from the validated Drive folder name. Make derived
   aliases unique deterministically; never silently alter an explicit alias.
7. Add each validated root with an argument array equivalent to:

   ```bash
   /opt/drive-rag/venv/bin/python \
     /root/.codex/skills/drive-rag/scripts/drive_rag.py \
     --state-root /root/codexcode/.drive-rag registry add \
     --folder-id "$FOLDER_ID" --url "$FOLDER_URL" --alias "$ALIAS"
   ```

8. Re-run `registry list` and verify the exact folder IDs, URLs, unique aliases,
   and enabled states before reporting success.

An unattended invocation with no configured folders returns
`CONFIGURATION_REQUIRED` and performs no mutation. Folder validation alone is
not proof that the descendant inventory is complete.

Registry maintenance uses only the CLI's `registry list`, `add`, `enable`,
`disable`, `rename`, and `remove` actions. After removing or disabling a root,
immediately run the complete recursive inventory, fail-closed plan, requested
artifact materialization, and `sync apply` workflow below for the resulting set
of enabled roots. Only report removal or disablement complete after that exact
`sync apply` succeeds and its result is validated. If connector, inventory,
artifact, or apply work fails, report the typed failure and that configuration
changed but local/Chroma reconciliation remains pending; queries are
`INDEX_STALE` until reconciliation succeeds. Do not manually delete mirror or
ChromaDB data.

Use the exact registry command forms below. `IDENTIFIER` is an exact folder ID
or alias and `NEW_ALIAS` is a new unique alias:

```bash
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag registry enable "IDENTIFIER"
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag registry disable "IDENTIFIER"
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag registry rename "IDENTIFIER" "NEW_ALIAS"
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag registry remove "IDENTIFIER"
```

## Complete recursive Drive inventory

For sync, load the registry and traverse every enabled root from its exact
folder ID. For each root:

1. Call the exact `get_file_metadata` shape from the capability gate for the
   root, then call `list_folder(url=<canonical folder URL>, top_k=<bounded limit>)` for the
   root and every discovered subfolder. Use only a canonical URL returned and
   identity-validated from the registry or connector, never a display name.
2. The current `list_folder` contract exposes no page-token input. Require its
   result to explicitly prove that the direct-child listing is complete. A
   result marked partial/truncated/capped, a result whose returned count reaches
   the requested `top_k`, or any response without a reliable completeness proof
   is `INVENTORY_INCOMPLETE`. If a future compatible connector returns a
   continuation token and accepts it, follow every page with the same URL and
   stable parameters until it explicitly reports no next page.
3. `list_folder` only establishes child discovery and completeness; never use
   its summary fields as authority for parent identity, MIME type, revision,
   checksum, size, or Drive URL. Call the exact `get_file_metadata` shape for
   every listed item. Require its `parents` to include the exact folder currently
   traversed and reject metadata/listing ID or name conflicts. For a folder MIME
   type, validate its folder identity, URL, and parent edge, then record
   subfolders only in the visited traversal/path graph and completeness proof;
   never emit a folder entry in `RemoteInventory.files`.
4. Track visited folder IDs, validated direct-child edges, and
   parent/root-relative paths; recurse into each folder ID exactly once and
   reuse its validated child-edge result for every reachable path. A repeated
   folder identity may add another reachable path but must not cause a second
   connector traversal or an infinite loop.
5. Retain all reachable paths for overlapping configured roots. Do not collapse
   entries by display name; file ID is the identity.
6. Only supported native Docs, Sheets, and Slides and non-folder files enter
   `RemoteInventory.files`, with paths derived through the visited folder graph.
   Validate every file's ID, parent identity, name, MIME type, Drive URL,
   revision or modified identity, modified time, size/checksum when available,
   and native kind. Folder metadata remains traversal evidence only.
7. Reject duplicate file IDs with conflicting revision, MIME, checksum, size,
   native kind, or URL. Preserve duplicate names as separate file identities;
   deterministic local collision suffixing belongs to `sync plan`.

Map authoritative metadata to inventory explicitly:

```text
id -> file_id
name -> name
mimeType -> mime_type
modifiedTime -> modified_time
version/headRevisionId -> revision
md5Checksum -> checksum
size -> size
parents -> parent_ids
webViewLink -> drive_url
title -> name
mime_type -> mime_type
modified_time -> modified_time
parent_ids -> parent_ids
url -> drive_url
```

Use `version` or `headRevisionId` when present. When both are absent, call
`list_file_revisions(fileId=<exact file ID>)` and use its non-empty
`currentRevisionId`. Treat every revision as an opaque revision string; do not
require decimal formatting. Call revision history immediately before and after
every raw download, native PDF export, and native structured-content read, and
accept the artifact only with a matching pre/post revision. A changed revision
discards the artifact and requires a fresh inventory/materialization attempt.
`md5Checksum` and `size` may be
null only when Drive omits them for that MIME type. The accumulated root-relative
path still supplies `parts`; metadata `parents` must agree with every direct
parent edge used to build each path. If metadata is unavailable, malformed, or
inconsistent with the listing or another path, return `INVENTORY_INCOMPLETE` and
do not authorize deletion. A missing `get_file_metadata` capability is
`CONNECTOR_UNAVAILABLE`.

Create one inventory JSON below
`/root/codexcode/.drive-rag/staging/<run-id>/`. It contains
`schema_version`, a unique safe `run_id`, current UTC RFC3339 `generated_at`,
`complete`, every enabled `root_id`, `files`, and `incomplete_reason`. Each file
contains its immutable `file_id`, original `name`, `mime_type`, non-empty
`revision`, UTC `modified_time`, canonical `drive_url`, optional `checksum` and
`size`, `native_kind`, and every path with `root_id`, `parent_ids`, and original
path `parts`. These names are the exact connector inventory fields; do not
rename or omit them when their value is unknown. Use JSON null only for an
unavailable optional `checksum` or `size`.

Nested-folder inventory example:

```json
{
  "schema_version": "1",
  "run_id": "run-20260718t120000z",
  "complete": true,
  "root_ids": ["folder-root"],
  "files": [
    {
      "file_id": "file-child",
      "name": "child.txt",
      "mime_type": "text/plain",
      "revision": "7",
      "modified_time": "2026-07-18T12:00:00Z",
      "drive_url": "https://drive.google.com/file/d/file-child/view",
      "checksum": "d3b07384d113edec49eaa6238ad5ff00",
      "size": 12,
      "paths": [
        {
          "root_id": "folder-root",
          "parent_ids": ["folder-root", "folder-nested"],
          "parts": ["Nested", "child.txt"]
        }
      ],
      "native_kind": null
    }
  ],
  "incomplete_reason": null,
  "generated_at": "2026-07-18T12:00:00Z"
}
```

The example's validated root and `folder-nested` exist only in the traversal
graph; `files` contains only `file-child`, and completeness still requires every
discovered folder, direct-child listing, metadata record, and page to be
validated even though folder nodes are not serialized as remote files.

Completeness requires every enabled root, every subfolder, and every page to
succeed. Set `complete: true` only after every continuation token has been
exhausted, the connector has explicitly ended pagination, and all identities
validate. Any partial response, timeout, tool error, unavailable page,
truncation, skipped subtree, malformed record, stale generated time, or identity
conflict makes the inventory incomplete. Preserve the reason and return
`PARTIAL_INDEX` when every discovered file still has validated identity and a
stable revision. An incomplete inventory must not authorize deletion and cannot
serve as deletion proof. It must never authorize deletion: the agent must not
call `sync apply` with
removals. It may run the validated partial
plan/apply path only to upsert discovered revision-fenced files, preserve
unobserved committed files, and commit `partial` coverage with the exact
incomplete reason. If any discovered identity, revision, path, or artifact is
invalid, return `INVENTORY_INCOMPLETE` without apply.

Validate the materialized inventory before planning:

```bash
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag inventory validate \
  --input "$INVENTORY_PATH"
```

## Fail-closed synchronization

After the complete inventory validation succeeds, create the fail-closed plan:

```bash
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag sync plan \
  --inventory "$INVENTORY_PATH" --output "$PLAN_PATH"
```

Require the plan file to remain below the same private staging tree. Verify its
reported SHA-256 before reading it, then validate `schema_version`, `run_id`,
`downloads`, `unchanged_file_ids`, `deleted_file_ids`, and `target_paths`
against the exact validated inventory. Stop on any unrequested, missing, or
conflicting identity.

Use bounded concurrent batches for independent connector metadata and revision
reads; do not serialize one call per file. Keep each batch at ten files or
fewer, validate every response identity, and stop the batch on any failed or
mismatched result. During an interactive or parent-observed foreground sync,
send a bounded content-free progress update after each connector batch and at
least once every 60 seconds. Report only the completed stage and counts; never
include document bodies or raw connector output. A scheduled unattended run
does not need conversational updates, but it must retain the same bounded
batching and fail-closed behavior.

Normal sync execution must not inspect implementation source. The validated
Skill, CLI help, and schema-1 command results are the runtime contract. Do not
pause a normal run to read `drive_rag.py` or `drive_rag_lib` merely to rediscover
partial-inventory behavior already defined here.

Use this no-change fast path after validating `sync plan`: when the plan reports
zero downloads, do not fetch, export, or read structured content. Write the
schema-1 empty artifact set for that exact run, verify it contains zero
artifacts, and immediately call `sync apply`. This applies both to a fully
unchanged plan and to a deletion-only complete plan; partial plans must still
contain zero deletions. Do not stop after writing the empty artifact set—the
fresh `SYNC_OK_NO_CHANGES`, `SYNC_OK_CHANGED`, or `PARTIAL_INDEX` apply result is
the required terminal sync result.

Fetch or export only file identities requested by the validated sync plan.
Never prefetch the entire corpus and never use a name search as a substitute
for exact file ID retrieval.

All raw and PDF connector artifacts must be absolute files beneath the private
run staging root for the exact validated run.

### Non-native artifacts

For each planned non-native download, call
`fetch(url=<canonical Drive file URL>, download_raw_file=True)` with the exact
validated `drive_url` from the sync plan identity. Do not pass a file ID to
`fetch` and do not use its default best-effort text mode. Materialize the
returned raw-file reference as an absolute file beneath the private run staging
root for this exact run; a preview, extracted text, plain URL, base64 string, or
other model-visible byte encoding is not an artifact. Validate connector file
ID/revision, expected size when available, readable file type, and SHA-256. Do
not execute Office macros, HTML scripts, or embedded content.

If the available connector surface returns an opaque reference or otherwise
cannot materialize required raw or PDF bytes as absolute files beneath the
private run staging root only after the correct supported invocation above was
attempted and its result validated, return `CONNECTOR_OUTPUT_UNSUPPORTED` and
stop before `sync apply`. In that state, keep the previous committed version.
Also, never transcribe binary bytes through prompts or shell arguments, decode
model-visible binary content, or replace a real download with
connector-extracted prose.

### Google Docs, Sheets, and Slides

For every planned native Google Doc, Google Sheet, or Google Slide:

1. Read pre-materialization metadata with the exact `get_file_metadata` call and
   fields selector above. Require an exact match to the validated inventory
   identity, MIME type, revision, modified time, parents, URL, checksum, and size.
2. Call `export_file(id=<file ID>, mime_type="application/pdf")` for the exact
   planned native file. Materialize the PDF under staging as an absolute file
   and verify the returned identity, nonempty size, `%PDF` signature, and
   SHA-256. The same `CONNECTOR_OUTPUT_UNSUPPORTED` materialization gate applies.
3. Obtain structured content for the same exact file ID and revision:
   - Docs: call `get_document` and normalize tabs, headings, paragraphs, lists,
     and tables into `{"kind":"document","sections":[{"locator":...,"text":...}]}`.
   - Sheets: call `get_spreadsheet_metadata`, then cover every nonempty used
     range with bounded `get_spreadsheet_range` or
     `get_spreadsheet_cells` calls. Follow continuation and split large ranges;
     never issue one unbounded whole-workbook read. Normalize into
     `{"kind":"spreadsheet","sheets":[{"name":...,"rows":[...]}]}` while
     preserving tab order, headers, and cell values.
   - Slides: call `get_presentation` and normalize visible text and speaker
     notes into
     `{"kind":"presentation","slides":[{"number":...,"text":...,"notes":...}]}`.
4. Validate that PDF and structured results refer to the planned file ID, and
   validate their revision identity whenever that connector result exposes one.
   The authoritative revision fence is the exact pre/post Drive metadata pair;
   this is required even when a structured Sheets result has no revision field.
   If either half is absent, truncated, malformed, or mismatched, do not replace
   the previously committed version.
5. Read post-materialization metadata with the identical exact fields selector.
   Require it to equal the pre-materialization metadata and planned inventory
   identity. If the file changed during materialization, discard both staged
   halves, return `INVENTORY_INCOMPLETE`, and restart from a new complete
   inventory before any apply or deletion. Never combine a PDF from one revision
   with structured content from another; a bounded retry must repeat the entire
   inventory and materialization fence, otherwise preserve the prior version.

Write one schema-1 artifact-set JSON for the plan run. Each planned download
has exactly one record containing `file_id`, `revision`, absolute staged
`payload_path`, `payload_sha256`, and either an absolute `structured_path` for
a native file or null for a non-native file. Paths must be unique, below the
exact run staging directory, and must not use symlinks. The artifact set must
contain no unchanged or deleted identity and no document content in metadata.

Apply only after every requested artifact and identity is verified:

```bash
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag sync apply \
  --inventory "$INVENTORY_PATH" --artifacts "$ARTIFACT_SET_PATH"
```

Validate the returned run ID and content-free counts. `sync apply` performs
structured normalization, supported-file extraction and OCR, chunking, local
embedding, ChromaDB mutation, mirror promotion, exact unreachable-file
deletion, manifest commit, and recovery journaling. Do not call `index
delete-file`, remove mirror paths, or modify the manifest as a shortcut.
For a partial inventory, validate status `PARTIAL_INDEX`, deletion count zero,
partial coverage, coverage value `partial`, and the exact coverage reason.
Partial apply may insert or
update discovered files but must preserve every unobserved committed manifest,
mirror, object, and ChromaDB identity. Only a later proven-complete inventory
may remove an identity because it was absent remotely.

Every committed file has an explicit index status. Supported content is
`indexed` with a null reason, including a supported empty document that
legitimately produces zero chunks. A mirrored unsupported file is `unindexed`
with reason `UNSUPPORTED_FORMAT` and has no active chunks. An artifact that
exceeds a deterministic extraction safety limit is also
mirrored and recorded as `unindexed`, with reason
`EXTRACTION_LIMIT_EXCEEDED`; this includes PDFs larger than 32 MiB. Do not
repeatedly retry such an artifact in a way that can exhaust the runtime.
Preserve these fields through alias moves, recovery, and unchanged-file
commits; deletion removes the
entire manifest entry. Content-free sync and status output may report indexed
and unindexed counts plus reason counts, but never source content. A legacy
schema-1 manifest entry without both fields is invalid state and requires a
controlled resync/rebuild; never infer a status from an empty chunk list.

Return `SYNC_OK_NO_CHANGES` or `SYNC_OK_CHANGED` only from a complete validated
apply result, and `PARTIAL_INDEX` from a validated deletion-free partial apply.
On failure return `SYNC_FAILED_PREVIOUS_VERSION_ACTIVE` only after
status confirms the last committed version is still active; otherwise report
the exact unresolved failure without claiming recovery.

## Cited query workflow

Query all enabled folders by default. Add one `--folder-alias` argument for each
exact requested alias; do not approximate unknown aliases. Run an argument
array equivalent to:

```bash
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag query \
  --question "$QUESTION" [--folder-alias "$ALIAS" ...]
```

Validate query readiness, model identity, alias scope, and the result envelope.
Each evidence item must contain a bounded excerpt, file ID/name, folder alias,
original Drive path, Drive URL, local mirror path, page/sheet-range/slide/section
locator, revision, content hash, MIME type, and retrieval distance. Treat the
distance as retrieval diagnostics, not source truth. Deduplicate overlapping
evidence and cite the exact locator and Drive URL in the parent handoff.
When manifest coverage is partial, require query status `PARTIAL_INDEX`, include
the exact coverage reason and a coverage warning on every query result, and tell
the parent that evidence may omit files the connector did not enumerate.

If the evidence list is empty or no item supports the question, return
`NO_RELEVANT_EVIDENCE`. Do not fill gaps from general knowledge. If the CLI
returns `INDEX_STALE`, `CONFIGURATION_REQUIRED`, or another typed error, return
that exact state instead of querying files directly or citing stale evidence.

## Scheduled runs

After at least one validated folder is committed, create one scheduled task,
not one task per folder. Its schedule is RRULE `FREQ=HOURLY;INTERVAL=1`, it runs
in local project mode for `/root/codexcode`, and every invocation synchronizes
every enabled folder in the shared state. The scheduled invocation is
non-interactive. With no configured folders it returns
`CONFIGURATION_REQUIRED`; it never asks questions and keeps successful
no-change output concise.

Before any task creation, run `schedule show`. If it reports
`configured: false`, enumerate matching local-project scheduled tasks before
creation through task management. Inspect every task in `/root/codexcode` whose
prompt or identity references `drive_rag` or `$drive-rag`; validate exact RRULE,
local project mode/path, enabled state, and the full prompt below. If exactly one
live task matches, adopt it and call `schedule record --task-id` with its
observed ID instead of creating anything. If multiple candidates exist, or any
candidate is a near match with mismatched configuration, return
`SCHEDULE_CONFLICT` with content-free task IDs and mismatched fields; do not
record or create a task. Create exactly one new task only when enumeration
succeeds and proves no matching or near-matching task exists. If enumeration is
unavailable or incomplete, return `NOT_SCHEDULED` with the proposal and do not
create.

Use this exact scheduled-task prompt:

```text
Spawn the drive_rag subagent in sync mode. Invoke the $drive-rag Skill explicitly.
Synchronize every enabled configured Google Drive folder into the shared local
state. Prove recursive inventory completeness before any deletion, export native
Google Docs, Sheets, and Slides as PDF, update ChromaDB, and report only status,
counts, changed identities, and actionable failures. If no folder is configured,
return CONFIGURATION_REQUIRED. If connector enumeration is incomplete but every
discovered file has a stable revision, perform deletion-free partial upserts and
return PARTIAL_INDEX with a coverage warning. Never authorize deletion from a
partial inventory; preserve unobserved committed mirror and index records.
```

Use scheduled-task management only when its tool is actually visible. After a
creation request, verify the returned task identity, local project mode,
project path, enabled state, RRULE, and exact prompt before reporting it as
scheduled. Only after that direct observation, persist the verified task ID with:

```bash
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag schedule record --task-id "$TASK_ID"
```

Validate operation `schedule.record`, status `ok`, the observed task ID, RRULE,
project mode/path, and enabled state. Then run `status` again and require
`schedule_state` to be `CONFIGURED`, and run `schedule show` to verify the same
content-free cached identity. The deterministic record stores schema version 1,
the exact task ID, RRULE, local project mode and path, enabled state, and exact
prompt. A malformed or nonconforming record returns
`INVALID_SCHEDULE_RECORD`; mere file existence never means configured.
`schedule record` is idempotent only for that same cached task ID. A different
ID returns `SCHEDULE_IDENTITY_MISMATCH` and preserves the original record; first
resolve the original live task through the exact-ID workflow instead of
overwriting or orphaning it.

If scheduled-task management is unavailable, creation fails, or creation cannot
be observed, return `NOT_SCHEDULED` plus the exact RRULE and prompt above. Do not create a schedule record and do not claim that a schedule exists. If task creation succeeds but recording fails, use task management to disable or delete that newly created task and verify rollback before returning the exact failure.

For status and onboarding, cross-check any recorded task ID through task
management; the local record is not live proof by itself. If the live task is
missing, disabled, or mismatched, do not create a second task. First disable or
delete a mismatched live task when it still exists, verify that exact ID is no
longer enabled, then clear the cached claim using the observed local task ID:

```bash
/opt/drive-rag/venv/bin/python \
  /root/.codex/skills/drive-rag/scripts/drive_rag.py \
  --state-root /root/codexcode/.drive-rag schedule clear --task-id "$TASK_ID"
```

Validate operation `schedule.clear`, status `ok`, and `removed: true`, then
re-run `schedule show` and require `configured: false`. Return `NOT_SCHEDULED`
with the proposed RRULE/prompt or recreate exactly one task when the requested
mode authorizes schedule maintenance. If task management is unavailable, return
`NOT_SCHEDULED` without using a cached `CONFIGURED` state as a live claim and
without deleting the record. A later folder add, enable, disable, rename, or
remove updates the registry only; this one hourly task continues to discover
every enabled configured folder at run time.

## Output contract

Return one primary status code and only the fields relevant to the mode:

- configuration/status: folder aliases, enabled state, bounded counts,
  last-success/failure identity, pending-journal state, and schedule state;
- sync: run identity, changed/unchanged/deleted counts, prior-version state,
  validation performed, and content-free warnings; or
- query: `QUERY_EVIDENCE` plus cited evidence, freshness state, alias scope,
  and clearly labeled inference, or `NO_RELEVANT_EVIDENCE`.

Use these failure states exactly when applicable:
`CONFIGURATION_REQUIRED`, `CONNECTOR_UNAVAILABLE`,
`CONNECTOR_AUTH_REQUIRED`, `CONNECTOR_OUTPUT_UNSUPPORTED`,
`INVENTORY_INCOMPLETE`, `PARTIAL_INDEX`,
`SYNC_FAILED_PREVIOUS_VERSION_ACTIVE`, `INDEX_STALE`, and `NOT_SCHEDULED`.
Never report connector authentication, scheduled-task creation, synchronization,
deletion, or retrieval as operational unless that exact stage was directly
observed and validated.
