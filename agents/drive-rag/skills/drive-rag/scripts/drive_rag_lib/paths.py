"""Private Drive RAG state-directory and path-containment helpers."""

from __future__ import annotations

import os
from pathlib import Path

from .protocol import DriveRagError


STATE_DIRECTORIES = (
    "config",
    "manifests",
    "mirrors",
    "objects",
    "chroma",
    "models",
    "journal",
    "logs",
    "staging",
)


def resolve_below(root: Path, candidate: Path) -> Path:
    """Resolve *candidate* and require it to remain at or below *root*."""

    resolved_root = Path(root).expanduser().resolve(strict=False)
    raw_candidate = Path(candidate).expanduser()
    if not raw_candidate.is_absolute():
        raw_candidate = resolved_root / raw_candidate
    if ".." in raw_candidate.parts:
        normalized_candidate = Path(os.path.normpath(raw_candidate))
        try:
            normalized_candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise DriveRagError(
                f"path resolves outside state root: {candidate}",
                code="UNSAFE_PATH",
            ) from exc
        raise DriveRagError(
            f"path traversal is not allowed below state root: {candidate}",
            code="UNSAFE_PATH",
        )
    try:
        relative_parts = raw_candidate.relative_to(resolved_root).parts
    except ValueError as exc:
        raise DriveRagError(
            f"path resolves outside state root: {candidate}",
            code="UNSAFE_PATH",
        ) from exc
    current = resolved_root
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            try:
                current.resolve(strict=False).relative_to(resolved_root)
            except ValueError as exc:
                raise DriveRagError(
                    f"path resolves outside state root: {candidate}",
                    code="UNSAFE_PATH",
                ) from exc
            raise DriveRagError(
                f"symlink is not allowed below state root: {current}",
                code="UNSAFE_PATH",
            )
    resolved_candidate = raw_candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise DriveRagError(
            f"path resolves outside state root: {candidate}",
            code="UNSAFE_PATH",
        ) from exc
    return resolved_candidate


def ensure_state_root(root: Path) -> Path:
    """Create the private state hierarchy and return its resolved root path."""

    requested_root = Path(root).expanduser()
    try:
        requested_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved_root = requested_root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise DriveRagError(
                f"state root is not a directory: {requested_root}",
                code="INVALID_STATE_ROOT",
            )
        os.chmod(resolved_root, 0o700)
        for name in STATE_DIRECTORIES:
            directory = resolve_below(resolved_root, resolved_root / name)
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
    except DriveRagError:
        raise
    except OSError as exc:
        raise DriveRagError(
            f"could not initialize state root {requested_root}: {exc}",
            code="INVALID_STATE_ROOT",
        ) from exc
    return resolved_root
