# Codex Custom Subagents

A collection of reusable Codex custom-agent packages. Each agent lives in its own folder under `agents/` with a package README, a guided installation contract, and a public-safe agent artifact.

## Agents

### Medical Record Retriever

A read-only agent that answers questions from medical-record PDFs in an authorized Google Drive folder, including discharge dates, documented discharge medications, allergies, encounters, and record summaries.

- [Open the package and installation guide](agents/medical-record-retriever/)

### Medical Record Retriever (Generic)

The same public-safe Medical Record Retriever package, retained as a separately addressable Generic variant for sharing and installation from its own folder URL.

- [Open the Generic package and installation guide](agents/medical-record-retriever-generic/)

Both folders install the same `medical_record_retriever` agent identity. Install one variant, not both, in the same Codex scope.

## Package convention

Each `agents/<package-name>/` directory should be independently understandable when shared as a GitHub folder URL. Keep its entry-point `README.md`, installation instructions, and public template together. Installation instructions must distinguish artifact creation, static validation, installation, discovery, spawning, connector validation, and behavioral validation.

## Privacy and security

- Never commit configured data-source IDs or URLs, patient or customer identifiers, source inventories, extracted private content, credentials, connector tokens, cookies, or authorization headers.
- Keep public templates non-operational with explicit configuration markers.
- Require explicit authorization before accessing private data sources.
- Prefer read-only sandboxing and least-privilege connector accounts.
- Treat connector installation and connector authentication as separate gates.
- Validate actual custom-agent activity and tool calls before calling an agent operational.

Local configured TOML variants and evaluation/report artifacts are ignored by the repository patterns in [`.gitignore`](.gitignore). Project installers should additionally protect exact configured target paths in the repository-local `.git/info/exclude`.

## License

No license has been granted yet. Add an explicit license before redistributing this repository or accepting external contributions.
