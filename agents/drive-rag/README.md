# Google Drive RAG

`drive_rag` is a reusable Codex custom subagent that maintains a verified local
mirror and ChromaDB vector index for one or more Google Drive folders. Codex can
delegate Drive-backed questions to it while continuing its own reasoning.

The subagent:

- asks for one or more Google Drive folder URLs when none are configured;
- recursively traverses every enabled folder and verifies remote identities and revisions;
- mirrors ordinary files locally;
- exports Google Docs, Sheets, and Slides to PDF and also indexes their structured content;
- chunks content and creates multilingual embeddings in persistent ChromaDB;
- removes local files and ChromaDB records only after a complete, validated remote inventory proves deletion;
- returns evidence with Drive URL, Drive path, local path, locator, revision/hash, and retrieval distance; and
- maintains one hourly project-scoped scheduled sync when Codex task automation is available.

Drive access is read-only. Authentication is provided by Codex's connected
Google Drive connector; this package never asks for or stores Google tokens.

## Install with Codex

Give Codex this folder URL and say:

> Read `INSTALL.md` in this folder completely and install the Google Drive RAG subagent exactly as instructed.

[`INSTALL.md`](INSTALL.md) is the authoritative installation contract.

## Manage Google Drive folders

The recommended interface is a natural-language request to Codex. Codex
delegates the operation to `drive_rag`, which validates the exact folder through
the connected Google Drive connector before changing its local registry.

### Add a folder

Provide the Google Drive folder URL and, optionally, a unique local alias:

> Add this folder to the Drive RAG and call it `company-docs`:
> `https://drive.google.com/drive/folders/FOLDER_ID`
> Run the initial synchronization when finished.

To add multiple folders in one request:

> Add these folders to the Drive RAG:
>
> - `URL_1`, alias `engineering`
> - `URL_2`, alias `policies`
>
> Validate them and synchronize the index.

When no folders are configured, the first ordinary interactive question that
could use Drive knowledge triggers the same onboarding flow. The agent asks for
one or more folder URLs and optional aliases. It never guesses a folder from
recent files or searches by folder name.

An added folder is available for retrieval only after a complete recursive sync
successfully commits its mirror and ChromaDB records. Google Docs, Sheets, and
Slides are exported to PDF and their structured content is indexed as part of
that synchronization.

### List configured folders

Ask:

> List the folders configured in the Drive RAG.

The result identifies each folder by its local alias and durable Google Drive
folder ID and shows whether it is enabled.

### Remove a folder

Use either the exact alias or exact Google Drive folder ID:

> Remove the Drive RAG folder with alias `company-docs` and immediately
> reconcile the local mirror and ChromaDB.

Removal changes only the local RAG system. It does not delete, move, edit, or
share anything in Google Drive. A complete removal:

- removes the folder from the local registry;
- removes its files from the managed local mirror;
- removes its chunks and embeddings from ChromaDB; and
- commits a manifest containing only the remaining enabled folders.

The agent must run a new complete recursive inventory and successful fail-closed
sync before reporting removal complete. It must not manually delete mirror files
or ChromaDB records as a shortcut. If reconciliation fails, the registry change
is reported, the index remains `INDEX_STALE`, and the previous committed data is
not presented as current.

### Temporarily disable or re-enable a folder

To preserve the configuration while excluding a folder from retrieval:

> Disable the Drive RAG folder `company-docs` and reconcile the index.

To restore it later:

> Enable the Drive RAG folder `company-docs` and synchronize it.

Disabling retains the registry entry but, after successful reconciliation,
removes that folder from active retrieval and reconciles its local/indexed data.
Re-enabling performs a new validated synchronization before the folder is used.

### Scheduled synchronization

Folder additions, removals, enablement changes, and alias changes do not require
a new scheduled task. When the verified hourly task exists, it reads the current
enabled-folder registry on every run. If task automation is unavailable, the
agent reports `NOT_SCHEDULED` instead of claiming that synchronization is
scheduled.

## Package contents

- `agents/drive_rag.toml` — custom-agent definition.
- `skills/drive-rag/` — mandatory workflow Skill and deterministic Python CLI.
- `requirements.txt` — pinned Python dependencies.
- `install.py` — idempotent installer for the agent, Skill, parent delegation guidance, and private state directory.

## Runtime boundaries

- Linux, Python 3.11 or newer, and Tesseract OCR are required.
- The subagent is pinned to `gpt-5.6-terra` with medium reasoning effort.
- The runtime virtual environment is `/opt/drive-rag/venv`.
- Personal Codex assets default to `~/.codex`.
- Private mirror/index state defaults to `<workspace>/.drive-rag`.
- Google Drive content is never mutated.
- A cached schedule record is never treated as proof that a scheduled task exists.

The index uses `intfloat/multilingual-e5-small` through FastEmbed and persists
locally. The first embedding operation may download model files.
