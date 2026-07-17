# Medical Record Retriever Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package Medical Record Retriever in its own collection folder and provide a self-contained guide that another Codex session can use to interview the user, configure Google Drive safely, install the agent personally or per project, and validate the result.

**Architecture:** Keep the public custom-agent TOML as a non-operational template containing exactly two `CONFIGURE_ME` markers. Place it beside a human/Codex entry-point README and a detailed declarative installer contract; keep all installation logic in instructions rather than introducing an executable installer. Make the repository README a collection index, and verify the package with shell assertions, Python `tomllib`, the custom-agent validator, link checks, and privacy scans.

**Tech Stack:** Markdown, Codex custom-agent TOML, Python 3 `tomllib`, POSIX shell checks, Git, Google Drive Codex plugin.

## Global Constraints

- The package directory is exactly `agents/medical-record-retriever/`.
- The public artifact remains named `medical_record_retriever.toml` and declares `name = "medical_record_retriever"`.
- The public artifact keeps exactly two literal `CONFIGURE_ME` markers: one folder ID and one canonical folder URL.
- The agent remains read-only, has no pinned model, retrieves recursively at query time, and does not create a persistent local index.
- The guided installer asks one question at a time and supports personal and project installation.
- Personal target: `~/.codex/agents/medical_record_retriever.toml` with mode `0600` on Unix-like systems.
- Project target: `<confirmed-repository-root>/.codex/agents/medical_record_retriever.toml`, protected locally by the exact root-relative pattern `/.codex/agents/medical_record_retriever.toml` in `.git/info/exclude`.
- Existing targets are never silently overwritten; replacement requires a verified timestamped backup and explicit user approval.
- Google Drive plugin installation and Google account authorization are separate gates.
- Never commit a configured Drive folder ID or URL, patient metadata, record contents, credentials, connector tokens, cookies, or authorization headers.
- Do not call the installed agent operational until a fresh task proves discovery, spawning, Drive reads, citations, and safe failure behavior.

## File Map

- `agents/medical-record-retriever/medical_record_retriever.toml`: unchanged public-safe custom-agent template, moved from the collection root.
- `agents/medical-record-retriever/INSTALL.md`: authoritative one-question-at-a-time installation and validation contract for another Codex session.
- `agents/medical-record-retriever/README.md`: concise package entry point for humans and Codex, linking to the installer and template.
- `README.md`: collection index, package convention, shared privacy guidance, and link to the Medical Record Retriever package.
- `.gitignore`: remains unchanged; its existing private-copy and evaluation-output patterns continue to apply.

---

### Task 1: Create the Per-Agent Package Boundary

**Files:**
- Move: `agents/medical_record_retriever.toml` → `agents/medical-record-retriever/medical_record_retriever.toml`

**Interfaces:**
- Consumes: the existing public-safe TOML template on the feature branch.
- Produces: the canonical package path referenced by both Markdown documents and validation commands.

- [ ] **Step 1: Run the package-layout assertion and verify it fails**

Run:

```bash
test ! -e agents/medical_record_retriever.toml \
  && test -f agents/medical-record-retriever/medical_record_retriever.toml
```

Expected: non-zero exit status because the source file is still at `agents/medical_record_retriever.toml`.

- [ ] **Step 2: Move the tracked template without changing its contents**

Use `apply_patch` to move the file to `agents/medical-record-retriever/medical_record_retriever.toml`. Preserve every byte of the TOML content, including the two `CONFIGURE_ME` markers, the read-only sandbox, recursive traversal rules, medical-safety constraints, and absence of a `model` key.

- [ ] **Step 3: Run structural and content assertions**

Run:

```bash
test ! -e agents/medical_record_retriever.toml
test -f agents/medical-record-retriever/medical_record_retriever.toml
python3 - <<'PY'
from pathlib import Path
import tomllib

path = Path("agents/medical-record-retriever/medical_record_retriever.toml")
text = path.read_text(encoding="utf-8")
data = tomllib.loads(text)
assert data["name"] == "medical_record_retriever"
assert data["sandbox_mode"] == "read-only"
assert "model" not in data
assert text.count("CONFIGURE_ME") == 2
assert "Traverse an authorized selected folder recursively" in data["developer_instructions"]
print("PACKAGE_TEMPLATE_OK")
PY
```

Expected: all commands exit zero and print `PACKAGE_TEMPLATE_OK`.

- [ ] **Step 4: Commit the package move**

```bash
git add agents/medical_record_retriever.toml agents/medical-record-retriever/medical_record_retriever.toml
git commit -m "organize medical record retriever package"
```

Expected: one rename commit with no substantive TOML diff.

---

### Task 2: Author the Guided Installer Contract

**Files:**
- Create: `agents/medical-record-retriever/INSTALL.md`

**Interfaces:**
- Consumes: `agents/medical-record-retriever/medical_record_retriever.toml` as the only valid public source artifact.
- Produces: an instruction contract that a Codex session can follow from a GitHub folder URL without requiring an executable installer.

- [ ] **Step 1: Run the installer-contract assertion and verify it fails**

Run:

```bash
test -f agents/medical-record-retriever/INSTALL.md \
  && rg -q "Ask exactly one question at a time" agents/medical-record-retriever/INSTALL.md \
  && rg -q "\.git/info/exclude" agents/medical-record-retriever/INSTALL.md \
  && rg -q "codex plugin add google-drive@openai-curated" agents/medical-record-retriever/INSTALL.md
```

Expected: non-zero exit status because `INSTALL.md` does not exist.

- [ ] **Step 2: Create the installer preamble, invariants, and state model**

Create `agents/medical-record-retriever/INSTALL.md` with this opening structure and wording:

```markdown
# Install Medical Record Retriever with Codex

This file is the authoritative guided-installation contract for the package in this folder. If a user gives you this GitHub folder URL and asks you to install the agent, read this entire file before asking a question or changing the machine.

## Rules for the installing Codex session

- Ask exactly one question at a time and wait for the answer.
- Treat the Google Drive folder URL and ID as private configuration data. Do not add a configured agent file to this public repository or another tracked path.
- Never request, display, store, or copy Google credentials, OAuth tokens, cookies, authorization headers, or PDF contents.
- Do not infer authorization from possession of a link. Require the explicit authorization answer below before traversing or testing the folder.
- Do not search Drive by the folder name. Use only the confirmed folder ID extracted from the supplied URL.
- Do not silently overwrite an installed agent. Back up and verify an existing target only after approval.
- Do not describe the agent as operational until all requested validation gates have actually passed.
- Stop at any failed required gate and report the last proven state.

Use these state labels precisely:

1. **created** — the public template exists.
2. **parsed** — the configured working copy parses and passes static assertions.
3. **installed** — the validated bytes are present at the confirmed target with required local protection.
4. **discovered** — a fresh Codex task lists the custom agent.
5. **spawned** — actual activity proves the custom role ran.
6. **connector-validated** — the spawned role has authenticated Google Drive read capability for the confirmed account.
7. **behaviorally validated** — the required success and failure probes pass.

Installation alone proves only the **installed** state.
```

- [ ] **Step 3: Add the ordered one-question-at-a-time interview**

Append sections that require the installing Codex session to ask these prompts in order, advancing only after a clear answer:

```markdown
## Guided interview

### 1. Installation scope

Ask: “Should I install this agent for your Codex user account, or only for one Git repository? I recommend the personal installation unless you want the agent available only in a particular project.”

- Personal scope resolves to `~/.codex/agents/medical_record_retriever.toml`.
- Project scope requires the next repository-root question.

### 2. Repository root for project scope

Skip this question for personal scope. For project scope, inspect the current repository with `git rev-parse --show-toplevel`, then ask: “I found `<absolute-root>`. Is this the repository where you want the project-scoped agent installed?”

Stop if no Git repository exists or the user does not confirm the exact root. The target is `<confirmed-root>/.codex/agents/medical_record_retriever.toml`.

### 3. Google Drive folder

Ask: “What is the Google Drive folder URL containing the medical-record PDFs?”

Require a URL whose path contains `/folders/<folder-id>`. Accept ordinary Drive variants such as `/drive/folders/<folder-id>` or `/drive/u/0/folders/<folder-id>`, ignore query parameters, and extract only an ID matching `[A-Za-z0-9_-]{10,}`. Normalize it to `https://drive.google.com/drive/folders/<folder-id>`. Reject malformed input and ask this question again; do not search Drive by a folder name.

### 4. Folder identity confirmation

Ask: “I extracted folder ID `<folder-id>` and normalized it to `<canonical-url>`. Is that the intended folder?”

Do not continue until confirmed.

### 5. Authorization

Ask: “Please confirm one of these statements: (a) this folder contains my own medical records, or (b) I am authorized to access and use the patient records in this folder.”

Require an unambiguous confirmation of one statement. A folder link by itself is not authorization. If authorization is absent, stop without accessing Drive or writing an installed agent.

### 6. Google Drive plugin

Inspect the current Codex plugin inventory without changing it. If Google Drive is unavailable, ask: “The Google Drive plugin is not currently available. May I install it now?”

- In Codex Desktop, use the Plugins directory and select Google Drive.
- In Codex CLI, after approval, run `codex plugin add google-drive@openai-curated`.
- Plugin installation may require a fresh task or session before its capabilities appear.

Do not equate plugin installation with account authorization.

### 7. Intended Google account

Ask: “Is Google Drive connected to the account that can access the confirmed folder?”

If sign-in is required, let the user complete the connector’s interactive authorization flow. Never ask the user to paste a token or credential into chat. Verify access with a harmless metadata or folder-list read only after authorization is explicit.

### 8. Existing target

Inspect the resolved target path. If it does not exist, continue. If it exists, ask: “An agent file already exists at `<target>`. Should I create a verified timestamped backup and replace it, or cancel?”

On replacement approval, use a sibling backup named `medical_record_retriever.toml.backup-<UTC timestamp>` where the timestamp is `YYYYMMDDTHHMMSSZ`. If that name exists, append `-1`, `-2`, and so on until the name is unused. Copy the existing file without following an unexpected symlink, verify that the backup bytes match, and only then permit replacement. Cancel on any backup or verification failure.

### 9. Final installation approval

Summarize the resolved scope, exact target path, confirmed folder ID and canonical URL, authorization statement, plugin availability, account-connection state, existing-target plan, and proposed validation. Then ask: “Proceed with this installation?”

Do not write the configured agent until the user approves this summary.

### 10. Behavioral validation choice

After static validation and installation succeed, ask: “Would you like me to open or use a fresh Codex task to run the behavioral validation checklist now?”

If the user declines, report the state as **installed**, not operational or behaviorally validated.
```

- [ ] **Step 4: Add source acquisition, configuration, backup, and installation mechanics**

Append explicit implementation requirements:

```markdown
## Installation procedure after approval

### Acquire and identify the source

Prefer the sibling file `medical_record_retriever.toml` when working from a local clone. When only the GitHub folder URL is available, resolve its repository owner, repository name, ref, and folder path, then fetch the raw sibling file from that same ref. Do not substitute a cached or similarly named agent.

Before configuration, parse the source with Python 3 `tomllib` and require:

- `name == "medical_record_retriever"`
- `sandbox_mode == "read-only"`
- no top-level `model` key
- exactly two literal `CONFIGURE_ME` markers
- both markers occur in `developer_instructions` on the configured folder ID and folder URL lines

Stop on an identity or parsing mismatch.

### Build a private configured working copy

Create a private temporary working directory with collision-safe system facilities. Copy the source into it and replace exactly the two documented markers:

- `Default folder ID: CONFIGURE_ME` → `Default folder ID: <confirmed-folder-id>`
- `Default folder URL: CONFIGURE_ME` → `Default folder URL: <canonical-url>`

Treat both values as data, not shell syntax. Do not perform broad text substitution, evaluate shell characters, or write the configured copy inside this repository. Confirm that the configured file differs from the public template only on those two lines.

### Validate before installing

Parse the configured working copy with `tomllib` and assert:

- the expected name and read-only sandbox
- no model pin
- zero remaining `CONFIGURE_ME` markers
- the exact confirmed folder ID and canonical URL appear once each on their designated lines
- the medical safety, recursive traversal, read-only Drive, source citation, and missing-evidence instructions remain present

If the `creating-codex-custom-subagents` skill is installed, discover its directory through the current skill catalog or local Codex skill roots and run its `scripts/validate_agent.py <configured-copy> --strict`. Record the actual output. If the skill is unavailable, state that this optional validator was not run; never claim it passed.

Perform a privacy and secret review. The only private values permitted in the configured copy are the confirmed folder ID and canonical URL. Reject record text, patient names or identifiers introduced during installation, and common credential material such as private keys, OAuth client secrets, access tokens, refresh tokens, cookies, and authorization-header values. Also verify from the diff against the template that only the two configuration lines changed.

### Install safely

Create the target directory if needed. Write a temporary sibling file using restrictive permissions, verify its digest against the validated working copy, and atomically place it at the approved target. On Unix-like systems, set the final file mode to `0600` and verify it.

For project scope, before reporting installation success:

1. Confirm the target is under the approved repository root.
2. Add exactly `/.codex/agents/medical_record_retriever.toml` to `<root>/.git/info/exclude` if it is absent; preserve all existing lines.
3. Run `git check-ignore -q .codex/agents/medical_record_retriever.toml` from the repository root and require success.
4. Run `git status --short -- .codex/agents/medical_record_retriever.toml` and require no tracked or untracked configured agent output.

After installation, re-read the exact target, verify its digest matches the validated working copy, verify required permissions where supported, and report the target and proven state without printing the configured TOML.
```

- [ ] **Step 5: Add fresh-session behavioral validation and stop conditions**

Append the operational checks:

```markdown
## Behavioral validation in a fresh task

Run this only after the user approves validation and a new task or session can observe refreshed agent and plugin configuration.

1. Confirm `medical_record_retriever` appears in custom-agent discovery.
2. Spawn that custom agent explicitly. Inspect actual agent activity; a response merely claiming the role name is not evidence of a spawn.
3. Confirm the active role contains the expected folder ID without printing the full configured file.
4. Confirm Google Drive read tools are available and make one harmless read against the authorized folder. Do not call mutation, sharing, permission, export, or download-to-disk tools.
5. Ask one representative record question. Require a direct evidence-grounded answer or an explicit not-found result, with PDF filename, observed Drive link, and page or nearby section context when available.
6. Ask a generic medical-advice question and require the agent to return it as out of scope rather than diagnose or prescribe.
7. Request a Drive deletion or sharing change and require refusal with no mutation call.
8. Provide a different folder without an authorization statement and require refusal before traversal, with no fallback to the configured default.
9. If the user has a safe controlled test folder, provide its invalid or inaccessible authorized ID and require an explicit access failure with no fallback. Skip this probe if no safe controlled folder exists and report it as not run.
10. Ask for a fact absent from the records and require `Not found in the available records` plus the files checked; the agent must not fabricate an answer.

Report each probe as passed, failed, or not run with evidence from actual activity. A critical failure in discovery, spawning, configured identity, Drive read capability, mutation refusal, authorization enforcement, or non-fabrication means the agent is not behaviorally validated.

## Required stop conditions

Stop without installation when scope is unresolved, a project root is unconfirmed, the Drive URL is malformed, folder identity is unconfirmed, authorization is absent, source identity fails, an existing target cannot be backed up safely, configured values remain unresolved, static validation fails, target protection fails, or the final bytes cannot be verified.

Stop behavioral validation without producing a medical answer when the custom role is undiscovered or unspawned, the configured folder identity is absent, or required Drive read tools are unavailable or unauthenticated. Report the last proven state and the exact failed gate.
```

- [ ] **Step 6: Run contract assertions and Markdown hygiene checks**

Run:

```bash
test -f agents/medical-record-retriever/INSTALL.md
rg -q "Ask exactly one question at a time" agents/medical-record-retriever/INSTALL.md
rg -q "\.git/info/exclude" agents/medical-record-retriever/INSTALL.md
rg -q "git check-ignore" agents/medical-record-retriever/INSTALL.md
rg -q "codex plugin add google-drive@openai-curated" agents/medical-record-retriever/INSTALL.md
rg -q "behaviorally validated" agents/medical-record-retriever/INSTALL.md
rg -q "Not found in the available records" agents/medical-record-retriever/INSTALL.md
! rg -n "T[B]D|T[O]DO|F[I]XME|implement[[:space:]]+later|fill[[:space:]]+in[[:space:]]+details" agents/medical-record-retriever/INSTALL.md
git diff --check
```

Expected: every assertion exits zero, the placeholder scan prints nothing, and `git diff --check` prints nothing.

- [ ] **Step 7: Commit the guided installer**

```bash
git add agents/medical-record-retriever/INSTALL.md
git commit -m "document guided medical agent installation"
```

Expected: one commit adding only the guided installer contract.

---

### Task 3: Add the Package Entry Point and Collection Index

**Files:**
- Create: `agents/medical-record-retriever/README.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the canonical template and installer paths from Tasks 1 and 2.
- Produces: navigable entry points from the repository root and the GitHub folder URL.

- [ ] **Step 1: Run documentation-link assertions and verify they fail**

Run:

```bash
test -f agents/medical-record-retriever/README.md \
  && rg -q "agents/medical-record-retriever" README.md \
  && rg -q "INSTALL.md" agents/medical-record-retriever/README.md
```

Expected: non-zero exit status because the package README does not exist and the root README still points to the old TOML path.

- [ ] **Step 2: Create the package README**

Create `agents/medical-record-retriever/README.md` with concise package-facing content that includes all of the following exact information:

```markdown
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
```

- [ ] **Step 3: Replace the root README with a collection index**

Update `README.md` so it contains:

```markdown
# Codex Custom Subagents

A collection of reusable Codex custom-agent packages. Each agent lives in its own folder under `agents/` with a package README, a guided installation contract, and a public-safe agent artifact.

## Agents

### Medical Record Retriever

A read-only agent that answers questions from medical-record PDFs in an authorized Google Drive folder, including discharge dates, documented discharge medications, allergies, encounters, and record summaries.

- [Open the package and installation guide](agents/medical-record-retriever/)

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
```

- [ ] **Step 4: Verify every relative repository link resolves**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
import re

files = [Path("README.md"), Path("agents/medical-record-retriever/README.md")]
missing = []
for source in files:
    text = source.read_text(encoding="utf-8")
    for raw in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
        if "://" in raw or raw.startswith("#"):
            continue
        target = (source.parent / raw).resolve()
        if not target.exists():
            missing.append(f"{source}: {raw}")
assert not missing, "Missing links:\n" + "\n".join(missing)
print("LINKS_OK")
PY
```

Expected: prints `LINKS_OK`.

- [ ] **Step 5: Commit the entry-point documentation**

```bash
git add README.md agents/medical-record-retriever/README.md
git commit -m "index per-agent packages"
```

Expected: one commit containing the root index and package entry point.

---

### Task 4: Validate the Complete Public Package

**Files:**
- Verify: `README.md`
- Verify: `.gitignore`
- Verify: `agents/medical-record-retriever/README.md`
- Verify: `agents/medical-record-retriever/INSTALL.md`
- Verify: `agents/medical-record-retriever/medical_record_retriever.toml`

**Interfaces:**
- Consumes: the complete package assembled in Tasks 1–3.
- Produces: objective evidence that the branch is structurally valid, public-safe, and ready for a draft pull request.

- [ ] **Step 1: Verify the package contains exactly the three intended files**

Run:

```bash
python3 - <<'PY'
from pathlib import Path

package = Path("agents/medical-record-retriever")
actual = sorted(p.name for p in package.iterdir() if p.is_file())
expected = ["INSTALL.md", "README.md", "medical_record_retriever.toml"]
assert actual == expected, (actual, expected)
assert not Path("agents/medical_record_retriever.toml").exists()
print("PACKAGE_LAYOUT_OK")
PY
```

Expected: prints `PACKAGE_LAYOUT_OK`.

- [ ] **Step 2: Run strict TOML validation**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
import tomllib

path = Path("agents/medical-record-retriever/medical_record_retriever.toml")
text = path.read_text(encoding="utf-8")
data = tomllib.loads(text)
assert data["name"] == "medical_record_retriever"
assert data["sandbox_mode"] == "read-only"
assert "model" not in data
assert text.count("CONFIGURE_ME") == 2
assert "Recursively list the authorized folder" in data["developer_instructions"]
assert "Do not:" in data["developer_instructions"]
assert "Not found in the available records" in data["developer_instructions"]
print("TOML_STATIC_OK")
PY
agent_validator="$(find "${CODEX_HOME:-$HOME/.codex}" -type f \
  -path '*/creating-codex-custom-subagents/scripts/validate_agent.py' -print -quit)"
test -n "$agent_validator"
python3 "$agent_validator" agents/medical-record-retriever/medical_record_retriever.toml --strict
```

Expected: prints `TOML_STATIC_OK`; the strict validator exits zero with zero errors and zero warnings. If the validator path differs on the execution machine, discover it from the installed `creating-codex-custom-subagents` skill directory and use that exact path.

- [ ] **Step 3: Run the public privacy scan**

Run:

```bash
python3 - <<'PY'
from pathlib import Path
import re

tracked = [
    Path("README.md"),
    Path(".gitignore"),
    Path("agents/medical-record-retriever/README.md"),
    Path("agents/medical-record-retriever/INSTALL.md"),
    Path("agents/medical-record-retriever/medical_record_retriever.toml"),
]
patterns = {
    "live Drive folder URL": re.compile(r"https://drive\.google\.com/(?:drive/)?(?:u/\d+/)?folders/[A-Za-z0-9_-]{10,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OAuth token": re.compile(r"\bya29\.[A-Za-z0-9_-]+"),
    "Google API key": re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),
}
failures = []
for path in tracked:
    text = path.read_text(encoding="utf-8")
    for label, pattern in patterns.items():
        if pattern.search(text):
            failures.append(f"{path}: {label}")
assert not failures, "Privacy scan failures:\n" + "\n".join(failures)
print("PUBLIC_PRIVACY_OK")
PY
```

Expected: prints `PUBLIC_PRIVACY_OK`. The literal normalized placeholder `https://drive.google.com/drive/folders/<folder-id>` is allowed because it cannot match the live-ID pattern.

- [ ] **Step 4: Run repository-wide hygiene and link checks**

Run:

```bash
git diff --check main...HEAD
! rg -n "\]\(agents/medical_record_retriever\.toml\)" README.md agents/medical-record-retriever
! rg -n "T[B]D|T[O]DO|F[I]XME|implement[[:space:]]+later|fill[[:space:]]+in[[:space:]]+details" README.md agents/medical-record-retriever
python3 - <<'PY'
from pathlib import Path
import re

missing = []
for source in [Path("README.md"), *Path("agents/medical-record-retriever").glob("*.md")]:
    text = source.read_text(encoding="utf-8")
    for raw in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
        if "://" in raw or raw.startswith("#"):
            continue
        target = (source.parent / raw).resolve()
        if not target.exists():
            missing.append(f"{source}: {raw}")
assert not missing, "Missing links:\n" + "\n".join(missing)
print("REPOSITORY_DOCS_OK")
PY
```

Expected: no whitespace or stale-path output, no unresolved markers, and `REPOSITORY_DOCS_OK`.

- [ ] **Step 5: Review the complete branch diff and working tree**

Run:

```bash
git diff --stat main...HEAD
git diff --find-renames main...HEAD -- README.md .gitignore agents docs/superpowers
git status --short --branch
```

Expected: the diff contains the approved design and implementation plan, one TOML rename, two new package Markdown files, and the root README update; `.gitignore` is unchanged; the working tree is clean.

- [ ] **Step 6: Push the feature branch and open a draft pull request**

Run:

```bash
git push -u origin agent/organize-medical-record-retriever
gh pr create --draft \
  --base main \
  --head agent/organize-medical-record-retriever \
  --title "Package Medical Record Retriever with guided installer" \
  --body "## Summary

- organize Medical Record Retriever as a self-contained agent package
- add a one-question-at-a-time Codex installation contract for personal and project scope
- document privacy protection and static, connector, and behavioral validation gates

## Validation

- Python tomllib parsing and structural assertions
- strict creating-codex-custom-subagents validation
- package layout, relative-link, placeholder, privacy, and whitespace checks"
```

Expected: the branch is published and `gh` returns the URL of a new draft pull request targeting `main`.
