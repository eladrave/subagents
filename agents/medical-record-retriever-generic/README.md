# Medical Record Retriever

`medical_record_retriever` is a read-only Codex custom-agent template for answering questions from medical-record PDFs in one authorized Google Drive folder and its subfolders. It retrieves records at question time; it does not create a persistent local vector index.

Example questions include:

- “When was I discharged from Lee Health?”
- “Which medications were documented in my discharge instructions from Lynn Rehabilitation Center?”
- “What allergies are recorded in these documents?”

The agent retrieves and summarizes record evidence. It is not a clinician and must not diagnose, prescribe, select treatment, or recommend medication changes.

## Install with Codex

If you gave this GitHub folder URL to Codex, instruct it to read [`INSTALL.md`](INSTALL.md) completely and follow the guided interview exactly. The installer supports both personal and project-scoped installation, protects private folder configuration, verifies Google Drive setup, and offers behavioral validation.

Files in this package:

- [`INSTALL.md`](INSTALL.md) — authoritative guided installation and validation contract.
- [`medical_record_retriever.toml`](medical_record_retriever.toml) — public-safe agent template.

The public template is intentionally non-operational because both source fields contain `CONFIGURE_ME`. Do not commit a configured copy.

## Requirements and boundaries

- A current Codex client with reusable custom-agent support.
- The Google Drive plugin installed and connected to an account authorized for the records folder.
- An authorized Google Drive folder containing PDF records.
- Read-only filesystem sandboxing and read-only Drive behavior.
- Recursive retrieval limited to PDFs in the selected authorized folder tree.
- Evidence citations to the source PDF and explicit disclosure when evidence is missing, ambiguous, conflicting, unreadable, or truncated.

Drive connector configurations may expose mutation tools even though this agent forbids their use. For stronger isolation, use a dedicated Google account whose Drive access is limited to the intended records folder.

## Validation status

The committed template is created and statically validated only. A configured installation becomes operational only after a fresh Codex task proves agent discovery, actual spawning, authenticated Drive reads, source-cited answers, authorization enforcement, mutation refusal, and non-fabrication behavior. See the installer for the full checklist.
