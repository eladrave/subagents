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
