"""Validated local record of an observed Codex scheduled task."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Mapping

from .paths import ensure_state_root, resolve_below
from .protocol import SCHEMA_VERSION, DriveRagError, atomic_write_json, read_json


SCHEDULE_RRULE = "FREQ=HOURLY;INTERVAL=1"
SCHEDULE_PROJECT_MODE = "local"
SCHEDULE_PROJECT_PATH = "/root/codexcode"
SCHEDULE_PROMPT = """Spawn the drive_rag subagent in sync mode. Invoke the $drive-rag Skill explicitly.
Synchronize every enabled configured Google Drive folder into the shared local
state. Prove recursive inventory completeness before any deletion, export native
Google Docs, Sheets, and Slides as PDF, update ChromaDB, and report only status,
counts, changed identities, and actionable failures. If no folder is configured,
return CONFIGURATION_REQUIRED. If connector output is incomplete or cannot be
materialized locally, preserve the previous committed mirror and index."""

_SCHEDULE_FIELDS = {
    "task_id",
    "rrule",
    "project_mode",
    "project_path",
    "enabled",
    "prompt",
}


def _invalid(message: str) -> DriveRagError:
    return DriveRagError(message, code="INVALID_SCHEDULE_RECORD")


def _validate_task_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _invalid("schedule task_id is invalid")
    return value


@dataclass(frozen=True)
class ScheduleRecord:
    task_id: str
    rrule: str = SCHEDULE_RRULE
    project_mode: str = SCHEDULE_PROJECT_MODE
    project_path: str = SCHEDULE_PROJECT_PATH
    enabled: bool = True
    prompt: str = SCHEDULE_PROMPT

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ScheduleRecord":
        if set(payload) != _SCHEDULE_FIELDS:
            raise _invalid("schedule record fields are invalid")
        task_id = _validate_task_id(payload["task_id"])
        expected = {
            "rrule": SCHEDULE_RRULE,
            "project_mode": SCHEDULE_PROJECT_MODE,
            "project_path": SCHEDULE_PROJECT_PATH,
            "enabled": True,
            "prompt": SCHEDULE_PROMPT,
        }
        for field, value in expected.items():
            if payload[field] != value:
                raise _invalid(f"schedule {field} does not match the managed task")
        return cls(task_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "rrule": self.rrule,
            "project_mode": self.project_mode,
            "project_path": self.project_path,
            "enabled": self.enabled,
            "prompt": self.prompt,
        }


def _record_path(state_root: Path) -> Path:
    state = ensure_state_root(state_root)
    return resolve_below(state, state / "config" / "schedule.json")


def load_schedule(state_root: Path) -> ScheduleRecord | None:
    path = _record_path(state_root)
    if not os.path.lexists(path):
        return None
    if not path.is_file():
        raise _invalid("schedule record must be a regular file")
    try:
        payload = read_json(path)
    except DriveRagError as exc:
        raise _invalid(f"schedule record is unreadable: {exc}") from exc
    if set(payload) != {"schema_version", "schedule"}:
        raise _invalid("schedule record envelope is invalid")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise _invalid("schedule record schema_version is invalid")
    schedule = payload["schedule"]
    if not isinstance(schedule, dict):
        raise _invalid("schedule record payload must be an object")
    return ScheduleRecord.from_dict(schedule)


def record_schedule(state_root: Path, task_id: str) -> ScheduleRecord:
    observed_task_id = _validate_task_id(task_id)
    existing = load_schedule(state_root)
    if existing is not None:
        if existing.task_id != observed_task_id:
            raise DriveRagError(
                "observed schedule task ID does not match the cached task ID",
                code="SCHEDULE_IDENTITY_MISMATCH",
            )
        return existing
    record = ScheduleRecord(observed_task_id)
    atomic_write_json(
        _record_path(state_root),
        {"schema_version": SCHEMA_VERSION, "schedule": record.to_dict()},
    )
    return record


def clear_schedule(state_root: Path, task_id: str) -> bool:
    observed_task_id = _validate_task_id(task_id)
    path = _record_path(state_root)
    if not os.path.lexists(path):
        return False
    record = load_schedule(state_root)
    assert record is not None
    if record.task_id != observed_task_id:
        raise DriveRagError(
            "observed schedule task ID does not match the cached task ID",
            code="SCHEDULE_IDENTITY_MISMATCH",
        )
    try:
        path.unlink()
    except OSError as exc:
        raise DriveRagError(
            f"could not clear schedule record: {exc}", code="STATE_WRITE_FAILED"
        ) from exc
    return True


def schedule_state(state_root: Path) -> str:
    return "CONFIGURED" if load_schedule(state_root) is not None else "NOT_CONFIGURED"


def content_free_schedule(record: ScheduleRecord) -> dict[str, object]:
    return {
        "task_id": record.task_id,
        "rrule": record.rrule,
        "project_mode": record.project_mode,
        "project_path": record.project_path,
        "enabled": record.enabled,
        "prompt_sha256": hashlib.sha256(record.prompt.encode("utf-8")).hexdigest(),
    }
