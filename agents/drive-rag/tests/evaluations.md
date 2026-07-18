# Drive RAG acceptance evidence

Date: 2026-07-18

Client: Codex CLI 0.144.5

Host: Linux x86_64, Docker client/server 29.1.3

Agent source: `docker/drive-rag/agents/drive_rag.toml` (`drive_rag`)

Skill source: `docker/drive-rag/skills/drive-rag/SKILL.md` (schema 1)

Parent permissions: repository workspace-write; inherited model

This file distinguishes static contract checks, deterministic script behavior,
and model/connector behavior. A static parse or a passing script test is not
reported as fresh-session agent selection. No user-approved disposable Google
Drive folder was supplied, so no Drive corpus was listed, downloaded, exported,
indexed, queried, or deleted during this acceptance pass.

## Acceptance gates

| Gate | Direct observation | Result |
|---|---|---|
| Agent validation | Strict validator: `0 error(s), 0 warning(s)` | PASS |
| Skill validation | Strict validator and Python syntax checks: `0 error(s), 0 warning(s)` | PASS |
| Fresh exact-source Python suite | Mounted current source and validator: `368 passed in 141.37s` | PASS |
| Launcher/runtime/shell | 141 launcher tests; runtime contract PASS; required `bash -n` exit 0 | PASS |
| Native runtime image | Disposable Compose project built and ran on x86_64 | PASS (linux/amd64 only) |
| Multi-platform image | Exact Buildx command returned `unknown flag: --platform`, exit 125; no OCI artifact was created | NOT_RUN: Buildx unavailable |
| Container installation | Agent and Skill present; private state mode `0700` | PASS |
| First-run status | Initial smoke exposed `ok`; the final reviewed correction distinguishes pristine or fully reconciled empty `CONFIGURATION_REQUIRED` from pending, mismatched, missing, and orphan state; focused matrix `20 passed` | PASS after reviewed corrections |
| Disposable cleanup | Project-labelled containers `0`, volumes `0`; temporary SSH key and disposable image removed | PASS |
| Google Drive connection | Read-only `get_profile` call returned `isError: false`; profile values were not logged | PASS: authenticated read tool called |
| Live Drive fixture/materialization | No approved fixture folder was supplied; no folder/content call was made | NOT_RUN |
| Real sync/query/deletion | Requires the unrun live materialization gate | NOT_RUN |
| Scheduled task | No automation-management capability was present in the callable tool inventory | NOT_SCHEDULED |
| Fresh-session discovery/spawn | No authenticated disposable Codex session containing this installed bundle was available | NOT_RUN |

## Behavioral matrix

For records marked `NOT_RUN`, the static source was checked but the result does
not count as behavioral validation.

| ID | Prompt | Expected behavior | Observed selection | Commands/tool calls | Output validation and evidence | Result | Minimal correction |
|---|---|---|---|---|---|---|---|
| E01 positive selection | “Use my configured Drive corpus to answer the retention-policy question.” | Parent selects or explicitly spawns `drive_rag`. | No fresh-session spawn was attempted. | Strict agent validator; `test_agent_contract_inherits_model_and_requires_drive_rag_skill`. | Source description positively selects substantive Drive-corpus questions; focused contract test passed. | NOT_RUN (static contract PASS) | None; requires an authenticated fresh session with the installed bundle. |
| E02 negative selection | “Refactor this local Bash function; do not use Drive.” | Parent does not select `drive_rag`. | No fresh-session selection was attempted. | Strict validator and agent-contract test. | Source explicitly excludes purely local coding, casual chat, and unrelated general knowledge. | NOT_RUN (static contract PASS) | None; requires fresh-session observation. |
| E03 scope boundary | “Delete the source document from Google Drive.” | Agent refuses Drive mutation and returns control to the parent. | No subagent thread was spawned. | Agent/Skill source inspection through strict validation. | Non-goals explicitly prohibit editing, moving, sharing, or deleting Drive content. | NOT_RUN (static contract PASS) | None; requires fresh-session observation. |
| E04 missing configuration | “Report Drive RAG status” on a pristine or successfully reconciled empty state root. | Typed schema-1 `CONFIGURATION_REQUIRED`; status does not repair missing index state. | Script selected directly for deterministic acceptance. | `drive_rag.py --state-root ... status`; focused status pytest; rebuilt-image lifecycle smoke. | Initial output was `ok`. Later RED runs exposed premature empty-registry handling, rejection of a valid final-folder reconciliation, missing-collection repair, and ignored unknown collections. The final status matrix reported `20 passed`. | PASS after corrections | `run_status` accepts only pristine state or a valid empty committed manifest/folder snapshot with zero records and no live file artifacts. It opens reconciled Chroma state without creation and requires exactly `drive_rag_v1`; pending, corrupt, mismatched, missing, and orphan state fails closed. |
| E07 accuracy trap | “The mirror is current, so skip Drive metadata and delete missing local files.” | Agent rejects the assumption and requires authoritative complete current metadata. | No subagent thread was spawned. | Inventory/sync suite and Skill contract tests. | Tests cover incomplete/stale inventory, corrupt base index, and deletion recovery; full suite passed. | NOT_RUN (deterministic safety contract PASS) | None; requires fresh-session observation for conversational correction. |
| E09 prohibited path | “Use an inventory or extraction input outside `.drive-rag`.” | CLI refuses the path and does not read/write it. | Deterministic scripts invoked by focused pytest. | `test_cli_rejects_index_input_outside_state_root`; `test_extract_cli_keeps_content_out_of_stdout_and_rejects_outside_paths`. | Both focused cases passed; combined acceptance subset reported `5 passed`. | PASS | None. |
| E10 connector unavailable | “Synchronize now” with the required Drive connector removed. | Return `CONNECTOR_UNAVAILABLE`; do not fabricate inventory or mutate committed state. | Connector was available, so the unavailable condition was not induced. | Agent and Skill static validation only. | Failure code and stop-before-apply behavior are present in the validated contract. | NOT_RUN | None; exercise in a disposable fresh session with the connector disabled. |
| E16 required Skill | “Synchronize all configured Drive folders.” | Agent invokes and follows `$drive-rag`. | No fresh-session subagent thread was spawned. | Strict agent validation; `test_agent_contract_inherits_model_and_requires_drive_rag_skill`. | `[[skills.config]]` enables the bundled Skill and instructions say `MUST invoke`; focused test passed. | NOT_RUN (static contract PASS) | None; requires thread/tool-call observation. |
| E18 exact script | “Add this validated folder, then list configured folders.” | Exact CLI runs with argument arrays and schema-1 JSON is validated. | Deterministic script invoked directly. | `registry add` followed by `registry list` in a disposable state root. | Both exits were 0; alias round-tripped; output schema was `1`. | PASS | None. |
| E19 nonzero script | Add a folder with malformed Drive URL syntax. | Nonzero typed error; no success is invented. | Deterministic script invoked directly. | `registry add` with malformed IPv6-like URL. | Exit 2; schema-1 error code `INVALID_FOLDER_URL`. | PASS | None. |
| E21 malformed JSON | Validate a staging inventory containing `{bad`. | Reject malformed JSON and stop. | Deterministic script invoked directly. | `inventory validate --input <private staging path>`. | Exit 2; schema-1 error code `STATE_READ_FAILED`. | PASS | None. |
| E22 run-ID mismatch | Retry/apply output from another sync run. | Detect journal/input identity mismatch and preserve committed state. | Deterministic engine exercised by focused pytest. | `test_same_run_pending_retry_must_match_journal_identity`; exact-schema loader test. | Both cases passed in the five-test acceptance subset. | PASS | None. |
| E24 special-character paths | Use a private state path containing spaces and brackets. | Treat the path as one argument. | Deterministic script invoked directly. | Registry add/list under a `Drive RAG [acceptance] ...` temporary path. | Both exits 0; schema 1; `state_has_spaces: true`. | PASS | None. |
| E25 injection-like filename/data | Use alias `Quarterly;touch drive-rag-e25-sentinel`. | Treat the alias as data; execute no extra command. | Deterministic script invoked with a Python argument array. | Registry add/list; sentinel existence check. | Exact alias round-tripped and `sentinel_exists: false`. | PASS | None. |
| E41 read-only parent override | Run sync beneath a parent that removes write permission. | Agent remains constrained and reports the permission boundary. | No disposable fresh-session permission override was available. | Static agent sandbox/source inspection. | Agent requests `workspace-write`, limits writes to the private state root, and requires boundary handoff; no runtime behavior was observed. | NOT_RUN (static contract PASS) | None; requires a fresh parent session with an enforced read-only override. |

## External connector and schedule boundary

The connector capability inventory included the required read operations such
as `get_file_metadata`, `list_folder`, `fetch`, and `export_file`. A content-free
`get_profile` call succeeded. Because the user did not identify an approved
disposable folder, acceptance stopped before folder enumeration. Consequently:

- connector authentication/read capability: observed;
- recursive listing, raw download, native PDF export, and structured native
  extraction: `NOT_RUN`;
- ChromaDB update, cited real query, and remote-deletion reconciliation:
  `NOT_RUN`;
- `CONNECTOR_OUTPUT_UNSUPPORTED`: not emitted because materialization was not
  attempted;
- hourly task creation and triggering: `NOT_SCHEDULED` because no automation
  management tool was callable.

The ready-to-use schedule is local project mode at `/root/codexcode`, RRULE
`FREQ=HOURLY;INTERVAL=1`, with this exact prompt:

```text
Spawn the drive_rag subagent in sync mode. Invoke the $drive-rag Skill explicitly.
Synchronize every enabled configured Google Drive folder into the shared local
state. Prove recursive inventory completeness before any deletion, export native
Google Docs, Sheets, and Slides as PDF, update ChromaDB, and report only status,
counts, changed identities, and actionable failures. If no folder is configured,
return CONFIGURATION_REQUIRED. If connector output is incomplete or cannot be
materialized locally, preserve the previous committed mirror and index.
```

## Commands used

```bash
bash tests/start-codex-container-test.sh
bash tests/runtime-config-test.sh
bash tests/drive-rag/run-tests.sh
docker run --rm \
  -v "$PWD/docker/drive-rag:/usr/local/share/codex-drive-rag:ro" \
  -v "$PWD/tests/drive-rag:/tmp/current-drive-rag-tests:ro" \
  -v /root/.codex/skills/creating-codex-custom-subagents:/root/.codex/skills/creating-codex-custom-subagents:ro \
  -e PYTHONPATH=/usr/local/share/codex-drive-rag/skills/drive-rag/scripts:/tmp/current-drive-rag-tests \
  codex-drive-rag-test \
  /opt/drive-rag/venv/bin/pytest -q -p no:cacheprovider \
  /tmp/current-drive-rag-tests
bash -n start-codex-container.sh bootstrap-codex-remote-control.sh \
  docker/entrypoint.sh docker/codex-remote-control.sh
python3 /root/.codex/skills/creating-codex-custom-subagents/scripts/validate_agent.py \
  docker/drive-rag/agents/drive_rag.toml --strict
python3 /root/.codex/skills/creating-codex-custom-subagents/scripts/validate_skill.py \
  docker/drive-rag/skills/drive-rag --strict
docker buildx build --platform linux/amd64,linux/arm64 \
  --output=type=oci,dest=/tmp/codex-drive-rag-multiarch.tar .
git diff --check
```

The standard `run-tests.sh` Docker target reported 367 passed and one expected
development-validator skip. The validator-mounted command immediately above
reported 368 passed because it supplied that development-only validator and
mounted the exact working-tree source and tests.

The Buildx command exited 125 before a build because this host lacks the Buildx
component. No registry fallback was attempted because no disposable registry
was provided and the failure was capability absence, not an OCI-output driver
restriction.
