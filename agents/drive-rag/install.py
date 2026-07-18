#!/usr/bin/env python3
"""Install the managed Drive RAG agent bundle into a persistent Codex home."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import stat
import sys
import tempfile
import tomllib


START_MARKER = "<!-- codex-drive-rag:start -->"
END_MARKER = "<!-- codex-drive-rag:end -->"
GUIDANCE_BLOCK = """<!-- codex-drive-rag:start -->
For substantive questions that may involve configured Google Drive knowledge,
spawn the `drive_rag` subagent alongside your own reasoning and incorporate its
cited evidence. Skip it for casual chat, purely local coding commands, and
questions clearly unrelated to the Drive corpus. If relevance is uncertain,
delegate retrieval and accept `NO_RELEVANT_EVIDENCE` as a valid result.
For an ordinary interactive Drive-relevant query, if the subagent returns
`CONFIGURATION_REQUIRED`, ask the user for one or more Google Drive folder URLs
and optional aliases, then resume the `drive_rag` subagent in onboarding mode.
A scheduled or otherwise unattended sync must not prompt; preserve its typed
`CONFIGURATION_REQUIRED` result for operator follow-up.
<!-- codex-drive-rag:end -->"""


class InstallError(Exception):
    """An expected, user-actionable installation failure."""


def reject_symlink_components(path: Path, label: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part == "..":
            raise InstallError(f"{label} must not contain parent traversal")
        current /= part
        if current.is_symlink():
            raise InstallError(f"{label} must not contain symlinks")


def absolute_non_root(value: str, label: str) -> Path:
    candidate = Path(value).expanduser()
    if any(character in value for character in ('\n', '\r', '\x00', '`', '"')):
        raise InstallError(f"{label} contains unsupported path characters")
    if not candidate.is_absolute() or candidate == Path(candidate.anchor):
        raise InstallError(f"{label} must be an absolute non-root path")
    reject_symlink_components(candidate, label)
    resolved = candidate.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise InstallError(f"{label} must be an absolute non-root path")
    return resolved


def validate_source(source: Path) -> tuple[Path, Path]:
    agents = source / "agents"
    skills = source / "skills"
    agent = source / "agents" / "drive_rag.toml"
    skill = source / "skills" / "drive-rag"
    if not source.is_dir() or source.is_symlink():
        raise InstallError("source must be a real directory")
    if any(not path.is_dir() or path.is_symlink() for path in (agents, skills)):
        raise InstallError("source managed-asset directories are missing or unsafe")
    if not agent.is_file() or agent.is_symlink():
        raise InstallError("source agent is missing or unsafe")
    if not skill.is_dir() or skill.is_symlink() or not (skill / "SKILL.md").is_file():
        raise InstallError("source Skill is missing or unsafe")
    for path in skill.rglob("*"):
        if path.is_symlink():
            raise InstallError(f"source Skill contains a symlink: {path.relative_to(skill)}")
        if not path.is_dir() and not path.is_file():
            raise InstallError(
                f"source Skill contains an unsupported entry: {path.relative_to(skill)}"
            )
    return agent, skill


def managed_guidance(existing: str) -> str:
    starts = existing.count(START_MARKER)
    ends = existing.count(END_MARKER)
    if starts != ends or starts > 1:
        raise InstallError("managed guidance markers are ambiguous")
    if starts == 1:
        start = existing.index(START_MARKER)
        end_start = existing.index(END_MARKER)
        if end_start < start:
            raise InstallError("managed guidance markers are ambiguous")
        end = end_start + len(END_MARKER)
        return existing[:start] + GUIDANCE_BLOCK + existing[end:]
    if not existing:
        return GUIDANCE_BLOCK + "\n"
    separator = "\n" if existing.endswith("\n") else "\n\n"
    return existing + separator + GUIDANCE_BLOCK + "\n"


def ensure_real_directory(path: Path, label: str) -> None:
    if os.path.lexists(path):
        if path.is_symlink():
            raise InstallError(f"{label} must not be a symlink")
        if not path.is_dir():
            raise InstallError(f"{label} must be a directory")
        return
    path.mkdir()


def _stage_file(target: Path, payload: bytes, mode: int) -> Path:
    ensure_real_directory(target.parent, f"managed parent {target.parent.name}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.new.", dir=target.parent
    )
    staged = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(mode)
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _stage_directory(source: Path, target: Path, skill_text: str) -> Path:
    ensure_real_directory(target.parent, f"managed parent {target.parent.name}")
    staged = Path(tempfile.mkdtemp(prefix=f".{target.name}.new.", dir=target.parent))
    try:
        shutil.copytree(source, staged, dirs_exist_ok=True, copy_function=shutil.copy2)
        (staged / "SKILL.md").write_text(skill_text, encoding="utf-8")
        for directory in [staged, *(path for path in staged.rglob("*") if path.is_dir())]:
            directory.chmod(0o755)
        for file_path in (path for path in staged.rglob("*") if path.is_file()):
            source_mode = stat.S_IMODE(file_path.stat().st_mode)
            file_path.chmod(0o755 if source_mode & 0o111 else 0o644)
    except Exception:
        shutil.rmtree(staged)
        raise
    return staged


def _remove_path(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _commit_staged(staged: Path, target: Path) -> Path | None:
    backup: Path | None = None
    if os.path.lexists(target):
        backup = Path(tempfile.mkdtemp(prefix=f".{target.name}.old.", dir=target.parent))
        backup.rmdir()
        os.replace(target, backup)
    try:
        os.replace(staged, target)
    except Exception:
        if backup is not None:
            os.replace(backup, target)
        raise
    return backup


def _rollback_commit(target: Path, backup: Path | None) -> None:
    _remove_path(target)
    if backup is not None:
        os.replace(backup, target)


def _render_agent(source_agent: Path, codex_home: Path, state_root: Path) -> bytes:
    text = source_agent.read_text(encoding="utf-8")
    configured_skill = codex_home / "skills" / "drive-rag"
    source_setting = 'path = "docker/drive-rag/skills/drive-rag"'
    if text.count(source_setting) != 1:
        raise InstallError("source agent Skill path contract is invalid")
    text = text.replace(source_setting, f"path = {json.dumps(str(configured_skill))}")
    text = text.replace("/root/codexcode/.drive-rag", str(state_root))
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"rendered agent TOML is invalid: {exc}") from exc
    configs = parsed.get("skills", {}).get("config", [])
    if configs != [{"path": str(configured_skill), "enabled": True}]:
        raise InstallError("rendered agent Skill identity is invalid")
    return text.encode("utf-8")


def _render_skill(source_skill: Path, codex_home: Path, state_root: Path) -> str:
    text = (source_skill / "SKILL.md").read_text(encoding="utf-8")
    installed_skill = shlex.quote(str(codex_home / "skills" / "drive-rag"))
    installed_state = shlex.quote(str(state_root))
    if "/root/.codex/skills/drive-rag" not in text:
        raise InstallError("source Skill installation path contract is invalid")
    if "/root/codexcode/.drive-rag" not in text:
        raise InstallError("source Skill state path contract is invalid")
    return text.replace("/root/.codex/skills/drive-rag", installed_skill).replace(
        "/root/codexcode/.drive-rag", installed_state
    )


def _cleanup_backup(path: Path | None) -> None:
    if path is not None:
        _remove_path(path)


def install(source: Path, codex_home: Path, state_root: Path) -> None:
    source_agent, source_skill = validate_source(source)
    for target, label in ((codex_home, "codex home"), (state_root, "state root")):
        if target == source or source in target.parents:
            raise InstallError(f"{label} must not be inside the managed source")
    if codex_home == state_root or codex_home in state_root.parents or state_root in codex_home.parents:
        raise InstallError("codex home and state root must not overlap")

    guidance_path = codex_home / "AGENTS.md"
    if os.path.lexists(guidance_path) and (
        guidance_path.is_symlink() or not guidance_path.is_file()
    ):
        raise InstallError("Codex AGENTS.md must be a regular file")
    existing_guidance = (
        guidance_path.read_text(encoding="utf-8") if guidance_path.exists() else ""
    )
    next_guidance = managed_guidance(existing_guidance)
    guidance_mode = (
        stat.S_IMODE(guidance_path.stat().st_mode) if guidance_path.exists() else 0o600
    )

    codex_home.mkdir(parents=True, exist_ok=True)
    for managed_parent in (codex_home / "agents", codex_home / "skills"):
        if os.path.lexists(managed_parent) and (
            managed_parent.is_symlink() or not managed_parent.is_dir()
        ):
            raise InstallError(f"managed parent {managed_parent.name} must not be a symlink")
    state_existed = state_root.exists()
    prior_state_mode = stat.S_IMODE(state_root.stat().st_mode) if state_existed else None
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_root.chmod(0o700)

    agent_target = codex_home / "agents" / "drive_rag.toml"
    skill_target = codex_home / "skills" / "drive-rag"
    staged: list[tuple[Path, Path]] = []
    committed: list[tuple[Path, Path | None]] = []
    try:
        ensure_real_directory(agent_target.parent, "managed parent agents")
        ensure_real_directory(skill_target.parent, "managed parent skills")
        staged.append(
            (agent_target, _stage_file(agent_target, _render_agent(source_agent, codex_home, state_root), 0o600))
        )
        staged.append(
            (skill_target, _stage_directory(source_skill, skill_target, _render_skill(source_skill, codex_home, state_root)))
        )
        if next_guidance != existing_guidance:
            staged.append(
                (guidance_path, _stage_file(guidance_path, next_guidance.encode("utf-8"), guidance_mode))
            )

        for target, staged_path in staged:
            backup = _commit_staged(staged_path, target)
            committed.append((target, backup))
        for _, backup in committed:
            try:
                _cleanup_backup(backup)
            except OSError:
                pass
    except Exception:
        for target, backup in reversed(committed):
            _rollback_commit(target, backup)
        if prior_state_mode is not None:
            state_root.chmod(prior_state_mode)
        elif state_root.exists():
            try:
                state_root.rmdir()
            except OSError:
                pass
        raise
    finally:
        for _, staged_path in staged:
            _remove_path(staged_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--state-root", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        install(
            absolute_non_root(args.source, "source"),
            absolute_non_root(args.codex_home, "codex home"),
            absolute_non_root(args.state_root, "state root"),
        )
    except (InstallError, OSError, UnicodeError) as exc:
        print(f"Drive RAG installation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
