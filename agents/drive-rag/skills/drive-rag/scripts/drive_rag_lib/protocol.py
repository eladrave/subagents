"""Versioned JSON protocol primitives shared by Drive RAG commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Final, Mapping


SCHEMA_VERSION: Final = "1"

CONFIGURATION_REQUIRED: Final = "CONFIGURATION_REQUIRED"
CONNECTOR_UNAVAILABLE: Final = "CONNECTOR_UNAVAILABLE"
CONNECTOR_AUTH_REQUIRED: Final = "CONNECTOR_AUTH_REQUIRED"
INVENTORY_INCOMPLETE: Final = "INVENTORY_INCOMPLETE"
PARTIAL_INDEX: Final = "PARTIAL_INDEX"
SYNC_FAILED_PREVIOUS_VERSION_ACTIVE: Final = "SYNC_FAILED_PREVIOUS_VERSION_ACTIVE"
SYNC_OK_NO_CHANGES: Final = "SYNC_OK_NO_CHANGES"
SYNC_OK_CHANGED: Final = "SYNC_OK_CHANGED"
INDEX_STALE: Final = "INDEX_STALE"
NO_RELEVANT_EVIDENCE: Final = "NO_RELEVANT_EVIDENCE"
NOT_SCHEDULED: Final = "NOT_SCHEDULED"

SPECIFICATION_STATUSES: Final = frozenset(
    {
        CONFIGURATION_REQUIRED,
        CONNECTOR_UNAVAILABLE,
        CONNECTOR_AUTH_REQUIRED,
        INVENTORY_INCOMPLETE,
        PARTIAL_INDEX,
        SYNC_FAILED_PREVIOUS_VERSION_ACTIVE,
        SYNC_OK_NO_CHANGES,
        SYNC_OK_CHANGED,
        INDEX_STALE,
        NO_RELEVANT_EVIDENCE,
        NOT_SCHEDULED,
    }
)


class DriveRagError(Exception):
    """An expected, user-correctable Drive RAG error."""

    def __init__(self, message: str, *, code: str = "INVALID_REQUEST") -> None:
        super().__init__(message)
        self.code = code


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically replace *path* with a private, deterministic JSON document."""

    destination = Path(path)
    temporary_path: Path | None = None
    try:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
        os.chmod(destination, 0o600)
    except (OSError, TypeError, ValueError) as exc:
        raise DriveRagError(
            f"could not write JSON file {destination}: {exc}",
            code="STATE_WRITE_FAILED",
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def read_json(path: Path) -> dict[str, object]:
    """Read a JSON object, translating malformed or unreadable state to a typed error."""

    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise DriveRagError(
            f"could not read JSON file {source}: {exc}",
            code="STATE_READ_FAILED",
        ) from exc
    if not isinstance(payload, dict):
        raise DriveRagError(
            f"JSON file {source} must contain an object",
            code="INVALID_STATE",
        )
    return payload


def emit_result(operation: str, status: str, **fields: object) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "status": status,
        **fields,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
