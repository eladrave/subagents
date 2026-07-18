from __future__ import annotations

import os
import importlib.util
from pathlib import Path
import stat
import subprocess
import shutil
import sys
import tomllib

import pytest


START_MARKER = "<!-- codex-drive-rag:start -->"
END_MARKER = "<!-- codex-drive-rag:end -->"
AGENT_VALIDATOR = Path(
    "/root/.codex/skills/creating-codex-custom-subagents/scripts/validate_agent.py"
)


def load_installer(source: Path):
    spec = importlib.util.spec_from_file_location("drive_rag_installer", source / "install.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_installer(
    source: Path,
    home: Path,
    state: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(source / "install.py"),
            "--source",
            str(source),
            "--codex-home",
            str(home),
            "--state-root",
            str(state),
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def installed_text(home: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in home.rglob("*")
        if path.is_file()
    )


def test_installer_preserves_guidance_and_is_idempotent(
    asset_source: Path, tmp_path: Path
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    guidance = home / "AGENTS.md"
    guidance.write_text("User guidance.\n", encoding="utf-8")
    state = tmp_path / "workspace" / ".drive-rag"

    run_installer(asset_source, home, state)
    first = guidance.read_bytes()
    run_installer(asset_source, home, state)

    text = guidance.read_text(encoding="utf-8")
    assert guidance.read_bytes() == first
    assert text.count(START_MARKER) == 1
    assert text.count(END_MARKER) == 1
    assert text.startswith("User guidance.\n")
    assert (home / "agents" / "drive_rag.toml").is_file()
    assert (home / "skills" / "drive-rag" / "SKILL.md").is_file()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_installer_replaces_only_its_existing_marked_block(
    asset_source: Path, tmp_path: Path
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    guidance = home / "AGENTS.md"
    guidance.write_text(
        "Before.\n\n"
        f"{START_MARKER}\nOld managed text.\n{END_MARKER}\n\n"
        "After.\n",
        encoding="utf-8",
    )

    run_installer(asset_source, home, tmp_path / "state")

    text = guidance.read_text(encoding="utf-8")
    assert text.count(START_MARKER) == 1
    assert "Old managed text." not in text
    assert text.startswith("Before.\n\n")
    assert text.endswith("\n\nAfter.\n")


@pytest.mark.parametrize("argument", ["--source", "--codex-home", "--state-root"])
def test_installer_rejects_relative_and_filesystem_root_paths(
    asset_source: Path, tmp_path: Path, argument: str
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    values = {
        "--source": str(asset_source),
        "--codex-home": str(home),
        "--state-root": str(state),
    }
    for invalid in ("relative/path", os.sep):
        values[argument] = invalid
        result = subprocess.run(
            [
                sys.executable,
                str(asset_source / "install.py"),
                "--source",
                values["--source"],
                "--codex-home",
                values["--codex-home"],
                "--state-root",
                values["--state-root"],
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "absolute non-root" in result.stderr
        values = {
            "--source": str(asset_source),
            "--codex-home": str(home),
            "--state-root": str(state),
        }


def test_path_validation_rejects_a_symlink_that_resolves_to_root(
    asset_source: Path, tmp_path: Path
) -> None:
    root_link = tmp_path / "root-link"
    root_link.symlink_to(Path(os.sep), target_is_directory=True)
    installer = load_installer(asset_source)

    with pytest.raises(installer.InstallError, match="symlink|absolute non-root"):
        installer.absolute_non_root(str(root_link), "state root")


def test_installer_rejects_symlinked_state_without_chmodding_target(
    asset_source: Path, tmp_path: Path
) -> None:
    external = tmp_path / "external-state"
    external.mkdir(mode=0o755)
    state_link = tmp_path / "workspace" / ".drive-rag"
    state_link.parent.mkdir()
    state_link.symlink_to(external, target_is_directory=True)

    result = run_installer(asset_source, tmp_path / "home", state_link, check=False)

    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert stat.S_IMODE(external.stat().st_mode) == 0o755
    assert not (tmp_path / "home" / "agents" / "drive_rag.toml").exists()


def test_installer_rejects_symlinked_agents_guidance_file(
    asset_source: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    external = tmp_path / "external-guidance.md"
    external.write_text("External private guidance.\n", encoding="utf-8")
    (home / "AGENTS.md").symlink_to(external)

    result = run_installer(asset_source, home, tmp_path / "state", check=False)

    assert result.returncode != 0
    assert "AGENTS.md" in result.stderr
    assert external.read_text(encoding="utf-8") == "External private guidance.\n"
    assert (home / "AGENTS.md").is_symlink()


@pytest.mark.parametrize("managed_parent", ["agents", "skills"])
def test_installer_rejects_symlinked_managed_parent_directories(
    asset_source: Path, tmp_path: Path, managed_parent: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    external = tmp_path / f"external-{managed_parent}"
    external.mkdir()
    (home / managed_parent).symlink_to(external, target_is_directory=True)

    result = run_installer(asset_source, home, tmp_path / "state", check=False)

    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert list(external.iterdir()) == []


def test_installer_refuses_ambiguous_guidance_markers(
    asset_source: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    guidance = home / "AGENTS.md"
    guidance.write_text(
        f"{START_MARKER}\none\n{START_MARKER}\ntwo\n{END_MARKER}\n",
        encoding="utf-8",
    )
    original = guidance.read_bytes()

    result = run_installer(asset_source, home, tmp_path / "state", check=False)

    assert result.returncode != 0
    assert "managed guidance markers" in result.stderr
    assert guidance.read_bytes() == original


def test_installer_refuses_reversed_guidance_markers_without_changes(
    asset_source: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    guidance = home / "AGENTS.md"
    guidance.write_text(
        f"User prefix.\n{END_MARKER}\nstale\n{START_MARKER}\n",
        encoding="utf-8",
    )
    original = guidance.read_bytes()

    result = run_installer(asset_source, home, tmp_path / "state", check=False)

    assert result.returncode != 0
    assert "managed guidance markers" in result.stderr
    assert guidance.read_bytes() == original


def test_installer_rolls_back_managed_bundle_when_skill_commit_fails(
    asset_source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    (home / "skills" / "drive-rag").mkdir(parents=True)
    agent = home / "agents" / "drive_rag.toml"
    skill = home / "skills" / "drive-rag" / "SKILL.md"
    guidance = home / "AGENTS.md"
    agent.write_text("old agent\n", encoding="utf-8")
    skill.write_text("old skill\n", encoding="utf-8")
    guidance.write_text(
        f"User.\n{START_MARKER}\nold block\n{END_MARKER}\n",
        encoding="utf-8",
    )
    installer = load_installer(asset_source)
    original_commit = getattr(installer, "_commit_staged", None)

    def fail_skill_commit(staged: Path, target: Path):
        if target.name == "drive-rag":
            raise OSError("injected Skill commit failure")
        assert original_commit is not None
        return original_commit(staged, target)

    monkeypatch.setattr(installer, "_commit_staged", fail_skill_commit, raising=False)

    with pytest.raises(OSError, match="injected"):
        installer.install(asset_source, home, tmp_path / "state")

    assert agent.read_text(encoding="utf-8") == "old agent\n"
    assert skill.read_text(encoding="utf-8") == "old skill\n"
    assert guidance.read_text(encoding="utf-8") == (
        f"User.\n{START_MARKER}\nold block\n{END_MARKER}\n"
    )


def test_installed_assets_contain_no_credentials(
    asset_source: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    run_installer(asset_source, home, tmp_path / "state")
    text = installed_text(home)
    assert "Authorization: Bearer" not in text
    assert "GOOGLE_CLIENT_SECRET" not in text
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in text
    assert "OPENAI_API_KEY" not in text


def test_agent_contract_pins_efficient_model_and_requires_drive_rag_skill(
    asset_source: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    run_installer(asset_source, home, tmp_path / "state")
    data = tomllib.loads(
        (home / "agents" / "drive_rag.toml").read_text(encoding="utf-8")
    )

    assert data["name"] == "drive_rag"
    assert data["sandbox_mode"] == "workspace-write"
    assert data["model"] == "gpt-5.6-terra"
    assert data["model_reasoning_effort"] == "medium"
    assert data["skills"]["config"] == [
        {"path": str(home / "skills" / "drive-rag"), "enabled": True}
    ]
    description = data["description"]
    assert "substantive questions" in description
    assert "casual chat" in description
    instructions = data["developer_instructions"]
    for heading in (
        "## Role",
        "## Primary objective",
        "## Scope",
        "## Non-goals",
        "## Required inputs",
        "## Sources of truth",
        "## Required Skill and tools",
        "## Workflow",
        "## Verification",
        "## Output contract",
        "## Failure and escalation",
    ):
        assert heading in instructions
    assert "$drive-rag" in instructions
    assert "NO_RELEVANT_EVIDENCE" in instructions
    assert "CONNECTOR_UNAVAILABLE" in instructions
    assert "schema_version" in instructions
    assert "exit status" in instructions
    assert str(tmp_path / "state") in instructions


@pytest.mark.skipif(not AGENT_VALIDATOR.is_file(), reason="strict agent validator unavailable")
def test_source_and_rendered_agent_validate_with_unrelated_installed_skill(
    asset_source: Path, tmp_path: Path
) -> None:
    clean_repo = tmp_path / "clean-repo"
    source = clean_repo / "docker" / "drive-rag"
    shutil.copytree(asset_source, source)
    source_agent = source / "agents" / "drive_rag.toml"
    source_data = tomllib.loads(source_agent.read_text(encoding="utf-8"))
    assert source_data["skills"]["config"] == [
        {"path": "docker/drive-rag/skills/drive-rag", "enabled": True}
    ]

    isolated_home = tmp_path / "isolated-home"
    unrelated_skill = isolated_home / ".codex" / "skills" / "drive-rag"
    unrelated_skill.mkdir(parents=True)
    (unrelated_skill / "SKILL.md").write_text(
        "---\nname: drive-rag\ndescription: Use when unrelated.\n---\n"
        "# Unrelated installed Skill\n",
        encoding="utf-8",
    )
    clean_environment = dict(os.environ)
    clean_environment["HOME"] = str(isolated_home)
    clean_environment["CODEX_HOME"] = str(isolated_home / ".codex")

    source_result = subprocess.run(
        [
            sys.executable,
            str(AGENT_VALIDATOR),
            "docker/drive-rag/agents/drive_rag.toml",
            "--strict",
        ],
        cwd=clean_repo,
        check=False,
        capture_output=True,
        text=True,
        env=clean_environment,
    )
    assert source_result.returncode == 0, source_result.stdout + source_result.stderr
    assert "0 error(s), 0 warning(s)" in source_result.stdout

    home = tmp_path / "installed-home"
    run_installer(source, home, tmp_path / "state")
    installed_result = subprocess.run(
        [
            sys.executable,
            str(AGENT_VALIDATOR),
            str(home / "agents" / "drive_rag.toml"),
            "--strict",
        ],
        cwd=clean_repo,
        check=False,
        capture_output=True,
        text=True,
        env=clean_environment,
    )
    assert installed_result.returncode == 0, installed_result.stdout + installed_result.stderr
    assert "0 error(s), 0 warning(s)" in installed_result.stdout


def test_skill_encodes_complete_connector_sync_and_query_workflow(
    asset_source: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    run_installer(asset_source, home, tmp_path / "state")
    skill = (home / "skills" / "drive-rag" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "status",
        "interactive onboarding",
        "list_folder",
        "fetch",
        "export_file",
        "get_document",
        "get_presentation",
        "get_spreadsheet_metadata",
        "get_spreadsheet_range",
        "get_spreadsheet_cells",
        "recursive",
        "complete",
        "sync plan",
        "sync apply",
        "PDF",
        "structured",
        "schema_version",
        "input identity",
        "CONNECTOR_UNAVAILABLE",
        "CONNECTOR_AUTH_REQUIRED",
        "CONFIGURATION_REQUIRED",
        "NO_RELEVANT_EVIDENCE",
        "--folder-alias",
    ):
        assert required in skill
    assert "requested by the validated sync plan" in skill
    assert "Do not write document bodies to logs" in skill
    assert "incomplete" in skill.lower()
    assert "must not authorize deletion" in skill
    assert "Only report removal or disablement complete after" in skill
    assert str(home / "skills" / "drive-rag" / "scripts" / "drive_rag.py") in skill
    assert str(tmp_path / "state") in skill
