# Install Google Drive RAG with Codex

This is the authoritative installation contract for this folder. If a user
points you at this GitHub folder and asks you to install the subagent, read this
entire file before changing the machine.

## Installation rules

- Install only from this folder and preserve its `agents/` and `skills/` layout.
- Never request, print, copy, or store Google credentials or connector tokens.
- Do not configure a Drive folder during installation. On the first relevant
  interactive request, the installed agent asks for one or more folder URLs.
- Do not claim that Drive access, scheduling, or a live sync works until each
  has been observed in the installed Codex environment.
- Stop on a failed required command and report the last verified state.
- Do not overwrite an unrelated custom agent or Skill. If either target exists,
  inspect it and ask before replacement unless it is demonstrably this package.

## Supported environment

This release targets Linux Codex installations with:

- Python 3.11 or newer with `venv` support;
- permission to create `/opt/drive-rag/venv`;
- Tesseract OCR with English language data; and
- a current Codex client supporting custom agents, Skills, the Google Drive
  connector, and scheduled tasks for the scheduling feature.

The deterministic runtime path is intentionally fixed at
`/opt/drive-rag/venv/bin/python`. Do not install dependencies into the system
Python interpreter.

## 1. Acquire the package

When working from a local clone, set `PACKAGE_DIR` to the absolute path of this
folder. When given only the GitHub folder URL, clone the repository into a
temporary directory and use the same folder from the requested ref:

```bash
git clone --depth 1 https://github.com/eladrave/subagents.git /tmp/codex-subagents
PACKAGE_DIR=/tmp/codex-subagents/agents/drive-rag
```

Reject the source if any of these are absent:

```text
INSTALL.md
agents/drive_rag.toml
skills/drive-rag/SKILL.md
skills/drive-rag/scripts/drive_rag.py
install.py
requirements.txt
```

## 2. Resolve installation paths

Use the current Codex user's home unless the user explicitly supplies another
Codex home. Resolve all paths to absolute, non-root paths:

```bash
CODEX_INSTALL_HOME="${CODEX_HOME:-$HOME/.codex}"
DRIVE_RAG_STATE_ROOT="${CODEX_WORKSPACE_ROOT:-$PWD}/.drive-rag"
```

The state root must be inside the user's persistent workspace, must not be
inside `CODEX_INSTALL_HOME`, and must not be inside this package directory.
For the Codex CLI Container, use `/root/.codex` and
`/root/codexcode/.drive-rag`.

## 3. Install system and Python dependencies

On Debian or Ubuntu, run:

```bash
apt-get update
apt-get install -y python3 python3-venv tesseract-ocr tesseract-ocr-eng
python3 -m venv /opt/drive-rag/venv
/opt/drive-rag/venv/bin/python -m pip install --upgrade pip
/opt/drive-rag/venv/bin/python -m pip install --requirement "$PACKAGE_DIR/requirements.txt"
```

On another Linux distribution, install equivalent Python and Tesseract
packages with its package manager, then run the final three Python commands.
Do not use `curl | sh` or another unchecked remote installer pipeline.

Verify:

```bash
/opt/drive-rag/venv/bin/python --version
/opt/drive-rag/venv/bin/python -c 'import chromadb, fastembed, fitz, pytesseract'
tesseract --version
```

## 4. Install the Codex agent and Skill

Run the bundled installer with absolute paths:

```bash
/opt/drive-rag/venv/bin/python "$PACKAGE_DIR/install.py" \
  --source "$PACKAGE_DIR" \
  --codex-home "$CODEX_INSTALL_HOME" \
  --state-root "$DRIVE_RAG_STATE_ROOT"
```

The installer atomically installs:

- `$CODEX_INSTALL_HOME/agents/drive_rag.toml` with mode `0600`;
- `$CODEX_INSTALL_HOME/skills/drive-rag/`;
- a bounded delegation block in `$CODEX_INSTALL_HOME/AGENTS.md`; and
- the private state root with mode `0700`.

It renders the agent and Skill with the resolved Codex home and state root. It
does not copy credentials or configure Google Drive folders.

## 5. Verify the installed bytes and configuration

Run:

```bash
test -f "$CODEX_INSTALL_HOME/agents/drive_rag.toml"
test -f "$CODEX_INSTALL_HOME/skills/drive-rag/SKILL.md"
test -x /opt/drive-rag/venv/bin/python
test "$(stat -c '%a' "$CODEX_INSTALL_HOME/agents/drive_rag.toml")" = 600
test "$(stat -c '%a' "$DRIVE_RAG_STATE_ROOT")" = 700
/opt/drive-rag/venv/bin/python \
  "$CODEX_INSTALL_HOME/skills/drive-rag/scripts/drive_rag.py" \
  --state-root "$DRIVE_RAG_STATE_ROOT" status
```

The last command must return one JSON object with `schema_version` equal to
`"1"`, operation `status`, and either `CONFIGURATION_REQUIRED` for a new
installation or a valid existing status. A new installation is expected to be
unconfigured.

If the `creating-codex-custom-subagents` skill is available, run its strict
agent validator against the installed TOML. Also run the standard Skill
validator against the installed Skill. Record actual output; never claim these
checks passed if they were unavailable.

## 6. Refresh Codex and validate discovery

Start a fresh Codex task so custom-agent and Skill discovery is refreshed.
Confirm that `drive_rag` is listed, then explicitly spawn it for a `status`
request. Actual subagent activity is required; text merely claiming the role
is not proof of a spawn.

## 7. Connect Google Drive

Confirm that the official Google Drive connector is installed and connected to
an account that can read the intended folders. If authorization is required,
let the user complete the connector's interactive flow. Never ask the user to
paste OAuth material into chat.

The agent requires read capabilities for folder listing, metadata, raw-file
fetch, native PDF export, Google Docs, Google Sheets, and Google Slides. It must
also have revision-history reads through `list_file_revisions`. It must return
`CONNECTOR_UNAVAILABLE` or `CONNECTOR_AUTH_REQUIRED` if those gates fail.

Some connector versions return normalized metadata names and omit explicit
folder-listing completeness markers. The installed agent accepts the normalized
fields, uses `currentRevisionId` as an opaque pre/post artifact fence, and may
commit discovered files as `PARTIAL_INDEX`. Partial runs carry a coverage
warning and never delete unobserved mirror files or ChromaDB records.

## 8. First-run onboarding

Do not ask for a folder during package installation. On the first ordinary
interactive Drive-relevant question, `CONFIGURATION_REQUIRED` causes the
parent or `drive_rag` agent to ask for one or more Google Drive folder URLs and
optional aliases. Multiple folders are supported and enabled folders are
queried together by default.

For each folder, the agent must validate the exact folder through the connector
before writing the registry. Then run a full initial sync. Native Google Docs,
Sheets, and Slides must be exported to PDF and also materialized from their
structured connector content before indexing.

## 9. Scheduling

After the first folder is successfully committed, the agent attempts to create
or adopt exactly one hourly, local-project scheduled task for all enabled
folders using `FREQ=HOURLY;INTERVAL=1`. It records a task ID only after the live
task's prompt, recurrence, scope, and enabled state are observed and match.

If task automation is unavailable, report `NOT_SCHEDULED` and leave no false
local schedule claim. Never create a duplicate while a cached or live task is
unresolved.

## 10. Behavioral validation

With a safe user-approved folder, verify all of the following:

1. Recursive discovery and initial sync complete.
2. A normal file is mirrored and indexed.
3. A Google Doc, Sheet, or Slide produces a PDF plus structured indexed content.
4. A representative question returns cited evidence from ChromaDB.
5. A changed remote revision updates both the mirror and its indexed chunks.
6. In a controlled fixture, a remotely deleted file is removed locally and its
   ChromaDB records disappear only after a complete validated inventory.
7. A missing connector capability fails closed without changing committed state.
8. An empty unattended installation returns `CONFIGURATION_REQUIRED` without prompting.

Do not perform destructive tests against user data. Use a controlled fixture or
report a deletion test as not run.

## Completion states

- **installed**: dependencies, agent, Skill, and private state root verified.
- **discovered**: a fresh Codex task lists the custom agent.
- **spawned**: actual task activity proves the role ran.
- **connector-validated**: authenticated required Drive reads were observed.
- **synced**: a complete validated inventory, or a deletion-free partial
  inventory with explicit coverage state, was committed to mirror and ChromaDB.
- **scheduled**: the exact live hourly task was observed and recorded.

Report only states that were actually proven.
