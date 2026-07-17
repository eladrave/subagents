# Per-Agent Package and Guided Installer Design

## Goal

Restructure the repository as a collection of independently understandable Codex custom-agent packages. Move the Medical Record Retriever into its own folder and add a self-contained installation contract that another Codex session can follow after receiving the GitHub folder URL.

## Repository Structure

The repository will use this layout:

```text
agents/
└── medical-record-retriever/
    ├── README.md
    ├── INSTALL.md
    └── medical_record_retriever.toml
```

The top-level `README.md` becomes a collection index. Each future agent gets one sibling folder under `agents/` with its own overview, installation workflow, and agent artifact.

The existing `agents/medical_record_retriever.toml` moves to `agents/medical-record-retriever/medical_record_retriever.toml`. Its public-safe `CONFIGURE_ME` markers and medical-safety constraints remain intact.

## Folder Entry Point

`agents/medical-record-retriever/README.md` is the human and Codex entry point rendered by GitHub. It will:

- Describe the agent's purpose, limits, and required Google Drive capability.
- Tell a Codex session given the folder URL to read `INSTALL.md` completely and follow it exactly.
- Link to the agent TOML and the guided installer.
- State that the public template is not operational until configured and behaviorally validated.

## Guided Installer Contract

`INSTALL.md` is an instruction artifact, not executable code. Codex must read it completely before making changes and must ask only one installation question at a time.

The questionnaire runs in this order:

1. Ask whether installation is personal or project-scoped. Recommend personal installation unless the agent should apply only inside one repository.
2. For project scope, discover and confirm the exact repository root before forming the target path.
3. Ask for the canonical Google Drive folder URL.
4. Extract the folder ID and show the normalized folder URL for confirmation.
5. Ask the user to explicitly confirm that the folder contains their records or that they are authorized to access the patient records.
6. Inspect whether the Google Drive plugin is installed and enabled. If not, offer the supported installation path.
7. Ask whether Google Drive is connected to the intended account. Authentication remains interactive and is never automated or recorded.
8. Inspect the target agent path. If it exists, offer a timestamped, collision-safe backup or cancellation. Never overwrite silently.
9. Ask whether to proceed with installation after summarizing target path, scope, folder identity, authorization, plugin state, and planned validation.
10. After installation, ask whether to run the behavioral validation checklist in a fresh Codex task or session.

## Installation Targets

Personal installation target:

```text
~/.codex/agents/medical_record_retriever.toml
```

Project installation target:

```text
<confirmed-repository-root>/.codex/agents/medical_record_retriever.toml
```

For project installation, the configured file contains a private Drive folder identifier and must remain local. Codex will add the exact relative target path to the repository's local `.git/info/exclude`, preserving the shared `.gitignore`. If the target is not inside a Git repository, project installation stops and explains that a repository root is required.

## Source Acquisition

Codex may install from either:

1. A local clone of this repository, using the package's TOML as the source.
2. The raw GitHub URL corresponding to the same folder and branch.

The installer must validate that the fetched source parses as TOML and has:

- `name = "medical_record_retriever"`
- `sandbox_mode = "read-only"`
- no pinned model
- both `CONFIGURE_ME` markers before configuration

It must not use a cached or differently named file without confirming its identity.

## Configuration and Write Safety

Codex creates a configured working copy, replaces only the two documented `CONFIGURE_ME` values, validates it, then installs it to the confirmed target.

The installer must:

- Treat the folder ID and URL as data, never shell syntax.
- Reject malformed folder URLs or IDs.
- Never print connector credentials, tokens, cookies, or authorization headers.
- Never place the configured file in this public repository.
- Set personal installation permissions to owner read/write (`0600`) on Unix-like systems.
- Preserve existing files unless the user approved a backup and replacement.
- Use a timestamped backup name and verify the backup before replacing the target.
- Avoid changing global Codex configuration unless plugin installation requires the supported Codex command or UI workflow and the user approved it.

## Google Drive Setup

Supported setup paths:

- Codex Desktop: open Plugins, install Google Drive, and connect the intended account interactively.
- Codex CLI: run `codex plugin add google-drive@openai-curated`, then start a new session and complete any connector sign-in prompt.

Plugin installation and connector authorization are separate gates. A listed or installed plugin does not prove that the connector is authenticated or usable by the custom agent.

## Static Validation

Before installation, Codex must run:

1. Python `tomllib` parsing.
2. Assertions for the expected name, read-only sandbox, absence of a model pin, configured folder ID, configured canonical URL, and absence of `CONFIGURE_ME`.
3. The strict custom-agent validator when `/home`-independent discovery finds the `creating-codex-custom-subagents` validator locally. If unavailable, report that the optional strict validation was not run rather than inventing success.
4. A secret/privacy scan ensuring the configured file contains no credentials or medical record contents.

After installation, verify the exact target file, file mode where supported, and a digest that matches the validated configured working copy.

## Behavioral Validation

The installed agent is not operational merely because the file parses. A fresh Codex task or session must demonstrate:

- Successful custom-agent discovery and spawn as `medical_record_retriever`.
- Active configuration contains the confirmed folder ID.
- Google Drive read tools are present and a harmless read succeeds.
- A representative record question returns a source-cited answer.
- A generic medical question stays out of the record-retrieval role.
- A Drive deletion or sharing request is refused with no mutation call.
- An unauthorized folder override is refused before traversal.
- A controlled invalid authorized folder fails without silently falling back.
- Missing evidence produces an explicit not-found result rather than a fabricated answer.

Codex must inspect actual agent activity and tool calls. A response that only labels itself with the agent name is insufficient. If any critical gate fails, report the exact proven state and do not call the agent operational.

## Failure Handling

Stop without installation when:

- Installation scope is unresolved.
- Project scope lacks a confirmed Git repository root.
- The Drive folder URL is malformed.
- Authorization is absent or ambiguous.
- The source artifact has the wrong identity or does not parse.
- An existing target cannot be backed up safely.
- Configured values remain unresolved.
- Static validation fails.
- The target cannot be written or protected as required.

Stop behavioral validation without producing a medical answer when the custom role does not spawn, the configured folder identity is absent, or required Drive read tools are unavailable.

## Repository Documentation Changes

The top-level README will:

- Explain the per-agent folder convention.
- List Medical Record Retriever as the first package.
- Link to its folder-level README.
- Keep collection-wide contribution and privacy guidance at the root.

The root `.gitignore` continues to exclude local configured TOML variants and evaluation/report folders.

## Acceptance Criteria

- The old root-level agent TOML no longer exists.
- The per-agent folder contains exactly the overview, guided installer, and public-safe TOML artifact.
- All repository links resolve.
- The public TOML parses and passes strict structural validation with no warnings.
- The installer questionnaire is ordered, one-question-at-a-time, and covers both personal and project scope.
- Project installation instructions protect the configured path through `.git/info/exclude`.
- Existing targets require an approved collision-safe backup.
- No live Drive folder IDs, patient metadata, local machine paths, credentials, or connector secrets appear in committed files.
- The repository clearly distinguishes created, parsed, installed, discovered, spawned, connector-validated, and behaviorally validated states.
