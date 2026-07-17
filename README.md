# Codex Custom Subagents

Reusable custom-agent configurations for Codex.

## Medical Record Retriever

`medical_record_retriever` is a read-only agent template for answering questions from medical-record PDFs in an authorized Google Drive folder. It performs retrieval at query time rather than building a persistent local vector index.

Example questions:

- “When was I discharged from Lee Health?”
- “Which medications were documented in my discharge instructions?”
- “What allergies are recorded in these documents?”

The agent is a record retriever and summarizer, not a clinician. It must not diagnose, prescribe, or recommend treatment or medication changes.

### Requirements

- A current Codex client with custom-agent support.
- The Google Drive plugin installed and authorized.
- An authorized Google Drive folder containing PDF medical records.

### Configure and install

1. Open [`agents/medical_record_retriever.toml`](agents/medical_record_retriever.toml).
2. Replace both `CONFIGURE_ME` values with the authorized folder ID and canonical folder URL.
3. Copy the configured file into your personal agents directory:

   ```bash
   mkdir -p ~/.codex/agents
   cp agents/medical_record_retriever.toml ~/.codex/agents/medical_record_retriever.toml
   chmod 600 ~/.codex/agents/medical_record_retriever.toml
   ```

4. Restart Codex or open a new task so the agent list refreshes.
5. Explicitly test the installed role before relying on it:

   ```text
   Spawn medical_record_retriever and ask: “When was I discharged from the named facility?”
   ```

Confirm that the custom role actually spawned, used only Google Drive read operations, and cited the source PDF. A response that merely labels itself with the agent name is not proof that the configuration loaded.

If the template still contains `CONFIGURE_ME`, it will refuse to use a default folder. You may provide a task-level folder only with an explicit statement that it contains your records or that you are authorized to access those records.

### Security notes

- The filesystem sandbox is read-only.
- Drive mutation is prohibited by the agent instructions, but connector-level write-tool restriction may not be available. For stronger isolation, use a dedicated Google account whose Drive access is limited to the intended records folder.
- Do not commit folder IDs, patient identifiers, record inventories, extracted text, credentials, or connector tokens.
- The agent does not download PDFs, persist extracted content, or create embeddings/indexes.
- Treat the template as created and statically validated only. It becomes operational only after a fresh session demonstrates successful selection, tool discovery, safe read calls, citations, and failure behavior.

### Static validation

Parse the TOML with Python:

```bash
python3 -c 'import pathlib,tomllib; p=pathlib.Path("agents/medical_record_retriever.toml"); d=tomllib.loads(p.read_text()); assert d["name"] == "medical_record_retriever"; assert d["sandbox_mode"] == "read-only"; print("TOML_PARSE_OK")'
```

If the `creating-codex-custom-subagents` Skill is installed, also run its strict validator:

```bash
python3 ~/.codex/skills/creating-codex-custom-subagents/scripts/validate_agent.py agents/medical_record_retriever.toml --strict
```

## License

No license has been granted yet. Add an explicit license before redistributing this repository or accepting external contributions.
