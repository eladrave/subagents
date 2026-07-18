"""Completeness checks and deterministic Drive reconciliation planning."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path, PurePosixPath
import re
import unicodedata
from urllib.parse import unquote, urlparse

from .models import (
    INDEXED,
    UNINDEXED,
    UNSUPPORTED_FORMAT,
    Manifest,
    ManifestFile,
    RemoteFile,
    RemoteInventory,
    RemotePath,
    SyncPlan,
)
from .protocol import (
    SCHEMA_VERSION,
    DriveRagError,
    INVENTORY_INCOMPLETE,
    read_json,
)


_INVENTORY_KEYS = {
    "schema_version",
    "run_id",
    "complete",
    "root_ids",
    "files",
    "incomplete_reason",
    "generated_at",
}

_RFC3339_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)
_MIME_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$%&'*+.^_`|~-]*/"
    r"[A-Za-z0-9][A-Za-z0-9!#$%&'*+.^_`|~-]*$"
)


def load_inventory(path: Path) -> RemoteInventory:
    """Load and structurally validate a schema-1 connector inventory."""

    payload = read_json(path)
    if "generated_at" not in payload:
        raise DriveRagError(
            "inventory is missing mandatory generated_at",
            code="INVALID_INVENTORY",
        )
    if set(payload) != _INVENTORY_KEYS:
        raise DriveRagError(
            "inventory must contain exactly the schema-1 inventory fields",
            code="INVALID_INVENTORY",
        )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise DriveRagError(
            f"unsupported inventory schema: {payload['schema_version']!r}",
            code="UNSUPPORTED_SCHEMA",
        )
    inventory = RemoteInventory.from_dict(
        {key: value for key, value in payload.items() if key != "schema_version"}
    )
    _validate_inventory(inventory)
    return inventory


def load_manifest(path: Path) -> Manifest:
    """Load a committed manifest, or return an empty one when none exists."""

    if not path.exists():
        return Manifest.empty()
    payload = read_json(path)
    if set(payload) != {
        "schema_version",
        "files",
        "model_identity",
        "last_success",
        "last_failure",
        "root_ids",
        "last_inventory_generated_at",
    }:
        raise DriveRagError(
            "manifest must contain exactly the schema-1 manifest fields",
            code="INVALID_STATE",
        )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise DriveRagError(
            f"unsupported manifest schema: {payload['schema_version']!r}",
            code="UNSUPPORTED_SCHEMA",
        )
    manifest = Manifest.from_dict(
        {key: value for key, value in payload.items() if key != "schema_version"}
    )
    _validate_manifest(manifest)
    return manifest


def prove_complete(
    inventory: RemoteInventory, expected_root_ids: Collection[str]
) -> RemoteInventory:
    """Require an inventory that can safely authorize reconciliation."""

    _validate_inventory(inventory)
    if not inventory.complete:
        reason = f": {inventory.incomplete_reason}" if inventory.incomplete_reason else ""
        raise DriveRagError(
            f"inventory is not complete{reason}",
            code=INVENTORY_INCOMPLETE,
        )
    if set(inventory.root_ids) != set(expected_root_ids):
        raise DriveRagError(
            "inventory roots do not match the expected complete root set",
            code=INVENTORY_INCOMPLETE,
        )
    return inventory


def plan_sync(
    inventory: RemoteInventory,
    manifest: Manifest,
    expected_root_ids: Collection[str] | None = None,
) -> SyncPlan:
    """Plan downloads and deletions only from a proven-complete inventory."""

    _validate_manifest(manifest)
    expected_roots = (
        set(expected_root_ids)
        if expected_root_ids is not None
        else set(manifest.root_ids or inventory.root_ids)
    )
    prove_complete(inventory, expected_roots)
    generated_at = _parse_generated_at(inventory.generated_at, "inventory generated_at")
    if manifest.last_inventory_generated_at is not None:
        last_generated_at = _parse_generated_at(
            manifest.last_inventory_generated_at,
            "manifest last_inventory_generated_at",
        )
        if generated_at <= last_generated_at:
            raise DriveRagError(
                "inventory must be newer than the last committed inventory",
                code=INVENTORY_INCOMPLETE,
            )
    current = _canonical_files(inventory.files)
    downloads: list[RemoteFile] = []
    unchanged: list[str] = []
    for file_id in sorted(current):
        remote = current[file_id]
        committed = manifest.files.get(file_id)
        if committed is not None and (
            committed.revision,
            committed.checksum,
        ) == (remote.revision, remote.checksum):
            unchanged.append(file_id)
        else:
            downloads.append(remote)
    return SyncPlan(
        run_id=inventory.run_id,
        downloads=tuple(downloads),
        unchanged_file_ids=tuple(unchanged),
        deleted_file_ids=tuple(sorted(set(manifest.files) - set(current))),
        target_paths=_resolve_collisions(current),
    )


def _validate_inventory(inventory: RemoteInventory) -> None:
    if not isinstance(inventory.complete, bool):
        raise DriveRagError(
            "inventory complete must be a boolean", code="INVALID_INVENTORY"
        )
    if not inventory.run_id.strip():
        raise DriveRagError("inventory run_id must not be empty", code="INVALID_INVENTORY")
    _parse_generated_at(inventory.generated_at, "inventory generated_at")
    valid_root_ids = isinstance(inventory.root_ids, tuple) and all(
        isinstance(root_id, str)
        and bool(root_id.strip())
        and not _contains_control_character(root_id)
        for root_id in inventory.root_ids
    )
    if not valid_root_ids or len(set(inventory.root_ids)) != len(inventory.root_ids):
        raise DriveRagError(
            "inventory root IDs must be unique and non-empty",
            code="INVALID_INVENTORY",
        )
    if inventory.complete and inventory.incomplete_reason is not None:
        raise DriveRagError(
            "complete inventory must not have an incomplete reason",
            code="INVALID_INVENTORY",
        )
    if not inventory.complete and not (inventory.incomplete_reason or "").strip():
        raise DriveRagError(
            "incomplete inventory must include an incomplete reason",
            code="INVALID_INVENTORY",
        )
    known_roots = set(inventory.root_ids)
    for remote in inventory.files:
        _validate_remote(remote, known_roots)
    _canonical_files(inventory.files)


def _validate_manifest(manifest: Manifest) -> None:
    valid_root_ids = isinstance(manifest.root_ids, tuple) and all(
        isinstance(root_id, str)
        and bool(root_id.strip())
        and not _contains_control_character(root_id)
        for root_id in manifest.root_ids
    )
    if not valid_root_ids or len(set(manifest.root_ids)) != len(manifest.root_ids):
        raise DriveRagError(
            "manifest root IDs must be unique and non-empty",
            code="INVALID_STATE",
        )
    root_ids = set(manifest.root_ids)
    if manifest.files and not root_ids:
        raise DriveRagError(
            "manifest root scope is missing for committed files",
            code="INVALID_STATE",
        )
    if (
        manifest.files or manifest.last_success is not None
    ) and manifest.last_inventory_generated_at is None:
        raise DriveRagError(
            "manifest is missing the last inventory timestamp",
            code="INVALID_STATE",
        )
    for file_id, committed in manifest.files.items():
        if not isinstance(committed, ManifestFile):
            raise DriveRagError(
                f"manifest entry {file_id!r} must be a manifest file",
                code="INVALID_STATE",
            )
        if (
            not isinstance(file_id, str)
            or not file_id.strip()
            or not isinstance(committed.file_id, str)
            or not committed.file_id.strip()
            or file_id != committed.file_id
        ):
            raise DriveRagError(
                "manifest file key and file_id must be non-empty and match",
                code="INVALID_STATE",
            )
        if not isinstance(committed.revision, str) or not committed.revision.strip():
            raise DriveRagError(
                f"manifest file {file_id} has an empty revision",
                code="INVALID_STATE",
            )
        if any(path.root_id not in root_ids for path in committed.paths):
            raise DriveRagError(
                f"manifest root scope does not cover file {file_id}",
                code="INVALID_STATE",
            )
        if committed.checksum is not None and (
            not isinstance(committed.checksum, str)
            or not re.fullmatch(r"[0-9a-f]{32}", committed.checksum)
        ):
            raise DriveRagError(
                f"manifest file {file_id} has an invalid Drive MD5 checksum",
                code="INVALID_STATE",
            )
        if not committed.paths:
            raise DriveRagError(
                f"manifest file {file_id} has no committed mirror path",
                code="INVALID_STATE",
            )
        for path in committed.paths:
            _validate_remote_path(path, root_ids, file_id, "manifest file")
        if committed.object_sha256 is not None and (
            not isinstance(committed.object_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", committed.object_sha256)
        ):
            raise DriveRagError(
                f"manifest file {file_id} has an invalid object SHA-256",
                code="INVALID_STATE",
            )
        if committed.native_kind not in {
            None,
            "document",
            "spreadsheet",
            "presentation",
        }:
            raise DriveRagError(
                f"manifest file {file_id} has an invalid native kind",
                code="INVALID_STATE",
            )
        if committed.index_status == INDEXED:
            if committed.index_reason is not None:
                raise DriveRagError(
                    f"manifest file {file_id} has a reason for indexed content",
                    code="INVALID_STATE",
                )
        elif committed.index_status == UNINDEXED:
            if committed.index_reason != UNSUPPORTED_FORMAT:
                raise DriveRagError(
                    f"manifest file {file_id} has an invalid unindexed reason",
                    code="INVALID_STATE",
                )
            if committed.active_chunk_ids:
                raise DriveRagError(
                    f"manifest file {file_id} is unindexed but has active chunks",
                    code="INVALID_STATE",
                )
        else:
            raise DriveRagError(
                f"manifest file {file_id} has an invalid index status",
                code="INVALID_STATE",
            )
        chunks = committed.active_chunk_ids
        valid_chunks = isinstance(chunks, tuple) and all(
            isinstance(chunk, str)
            and re.fullmatch(rf"{re.escape(file_id)}:[0-9a-f]{{64}}", chunk)
            for chunk in chunks
        )
        if not valid_chunks or len(set(chunks)) != len(chunks):
            raise DriveRagError(
                f"manifest file {file_id} has invalid or duplicate active chunk IDs",
                code="INVALID_STATE",
            )
        if chunks and (
            not isinstance(manifest.model_identity, str)
            or not manifest.model_identity.strip()
        ):
            raise DriveRagError(
                "manifest model identity is required for active chunks",
                code="INVALID_STATE",
            )
    if manifest.last_inventory_generated_at is not None:
        _parse_generated_at(
            manifest.last_inventory_generated_at,
            "manifest last_inventory_generated_at",
        )


def _parse_generated_at(value: object, field: str) -> datetime:
    code = "INVALID_STATE" if field.startswith("manifest") else "INVALID_INVENTORY"
    return _parse_rfc3339_utc(value, field, code=code)


def _parse_rfc3339_utc(value: object, field: str, *, code: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_UTC.fullmatch(value):
        raise DriveRagError(
            f"{field} must be an RFC3339 UTC timestamp ending in Z",
            code=code,
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DriveRagError(
            f"{field} must be a valid RFC3339 UTC timestamp",
            code=code,
        ) from exc
    if parsed.tzinfo != timezone.utc:
        raise DriveRagError(
            f"{field} must use UTC",
            code=code,
        )
    return parsed


def _validate_remote(remote: RemoteFile, known_roots: set[str]) -> None:
    required_strings = {
        "file ID": remote.file_id,
        "name": remote.name,
        "MIME type": remote.mime_type,
        "revision": remote.revision,
        "Drive URL": remote.drive_url,
        "modified_time": remote.modified_time,
    }
    for field, value in required_strings.items():
        if not isinstance(value, str) or not value.strip():
            raise DriveRagError(
                f"remote file {field} must not be empty",
                code="INVALID_INVENTORY",
            )
    if not _MIME_TYPE.fullmatch(remote.mime_type):
        raise DriveRagError(
            f"file {remote.file_id} has an invalid MIME type",
            code="INVALID_INVENTORY",
        )
    _parse_rfc3339_utc(
        remote.modified_time,
        f"file {remote.file_id} modified_time",
        code="INVALID_INVENTORY",
    )
    if remote.checksum is not None and (
        not isinstance(remote.checksum, str)
        or not re.fullmatch(r"[0-9a-f]{32}", remote.checksum)
    ):
        raise DriveRagError(
            f"file {remote.file_id} has an invalid Drive MD5 checksum",
            code="INVALID_INVENTORY",
        )
    if remote.size is not None and (
        not isinstance(remote.size, int)
        or isinstance(remote.size, bool)
        or remote.size < 0
    ):
        raise DriveRagError(
            f"file {remote.file_id} has an invalid negative size",
            code="INVALID_INVENTORY",
        )
    if remote.native_kind not in {None, "document", "spreadsheet", "presentation"}:
        raise DriveRagError(
            f"file {remote.file_id} has an invalid native kind",
            code="INVALID_INVENTORY",
        )
    try:
        parsed = urlparse(remote.drive_url)
        valid_origin = (
            parsed.scheme == "https"
            and parsed.hostname in {"drive.google.com", "docs.google.com"}
            and parsed.username is None
            and parsed.password is None
        )
    except (TypeError, ValueError) as exc:
        raise DriveRagError(
            f"file {remote.file_id} has a malformed Drive URL",
            code="INVALID_INVENTORY",
        ) from exc
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    valid_resource = (
        parsed.hostname == "drive.google.com"
        and len(parts) >= 3
        and parts[:2] == ["file", "d"]
        and parts[2] == remote.file_id
    ) or (
        parsed.hostname == "docs.google.com"
        and len(parts) >= 3
        and parts[0] in {"document", "spreadsheets", "presentation"}
        and parts[1] == "d"
        and parts[2] == remote.file_id
    )
    if not valid_origin or not valid_resource:
        raise DriveRagError(
            f"file {remote.file_id} has a malformed Drive URL",
            code="INVALID_INVENTORY",
        )
    native_contracts = {
        "document": ("application/vnd.google-apps.document", "document"),
        "spreadsheet": ("application/vnd.google-apps.spreadsheet", "spreadsheets"),
        "presentation": ("application/vnd.google-apps.presentation", "presentation"),
    }
    if remote.native_kind is None:
        valid_identity = (
            not remote.mime_type.startswith("application/vnd.google-apps.")
            and parsed.hostname == "drive.google.com"
            and parts[:2] == ["file", "d"]
        )
    else:
        expected_mime, expected_resource = native_contracts[remote.native_kind]
        valid_identity = (
            remote.mime_type == expected_mime
            and parsed.hostname == "docs.google.com"
            and parts[0] == expected_resource
        )
    if not valid_identity:
        raise DriveRagError(
            f"file {remote.file_id} MIME type, native kind, and URL do not match",
            code="INVALID_INVENTORY",
        )
    if not remote.paths:
        raise DriveRagError(
            f"file {remote.file_id} has no reachable path",
            code="INVALID_INVENTORY",
        )
    for path in remote.paths:
        _validate_remote_path(path, known_roots, remote.file_id, "file")


def _validate_remote_path(
    path: RemotePath,
    known_roots: set[str],
    file_id: str,
    context: str,
) -> None:
    error_code = "INVALID_STATE" if context == "manifest file" else "INVALID_INVENTORY"
    if not isinstance(path, RemotePath):
        raise DriveRagError(
            f"{context} {file_id} has a malformed remote path",
            code=error_code,
        )
    if (
        not isinstance(path.root_id, str)
        or not isinstance(path.parent_ids, tuple)
        or any(not isinstance(parent_id, str) for parent_id in path.parent_ids)
        or not isinstance(path.parts, tuple)
        or any(not isinstance(part, str) for part in path.parts)
    ):
        raise DriveRagError(
            f"{context} {file_id} path fields must be tuples of strings",
            code=error_code,
        )
    if path.root_id not in known_roots:
        raise DriveRagError(
            f"{context} {file_id} references unknown root {path.root_id}",
            code=error_code,
        )
    if not path.parts:
        raise DriveRagError(
            f"{context} {file_id} has an empty remote path",
            code=error_code,
        )
    if (
        not path.parent_ids
        or path.parent_ids[0] != path.root_id
        or any(
            not parent_id or _contains_control_character(parent_id)
            for parent_id in path.parent_ids
        )
    ):
        raise DriveRagError(
            f"{context} {file_id} has an invalid remote parent path",
            code=error_code,
        )
    if any(
        part in {"", ".", ".."}
        or "/" in part
        or "\\" in part
        or _contains_control_character(part)
        for part in path.parts
    ):
        raise DriveRagError(
            f"{context} {file_id} has an unsafe remote path",
            code=error_code,
        )


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _canonical_files(files: tuple[RemoteFile, ...]) -> dict[str, RemoteFile]:
    canonical: dict[str, RemoteFile] = {}
    for remote in files:
        remote = replace(
            remote,
            paths=tuple(
                sorted(
                    set(remote.paths),
                    key=lambda path: (path.root_id, path.parent_ids, path.parts),
                )
            ),
        )
        existing = canonical.get(remote.file_id)
        if existing is None:
            canonical[remote.file_id] = remote
            continue
        if replace(existing, paths=()) != replace(remote, paths=()):
            raise DriveRagError(
                f"conflicting duplicate file ID: {remote.file_id}",
                code="INVALID_INVENTORY",
            )
        paths = tuple(
            sorted(
                set(existing.paths) | set(remote.paths),
                key=lambda path: (path.root_id, path.parent_ids, path.parts),
            )
        )
        canonical[remote.file_id] = replace(existing, paths=paths)
    return canonical


def _resolve_collisions(
    current: dict[str, RemoteFile]
) -> dict[str, tuple[RemotePath, ...]]:
    owners: dict[tuple[str, tuple[str, ...]], set[str]] = {}
    for file_id, remote in current.items():
        for path in remote.paths:
            key = (path.root_id, path.parts)
            owners.setdefault(key, set()).add(file_id)

    entries: list[tuple[str, RemotePath, RemotePath]] = []
    seen_local_targets: set[tuple[str, str, tuple[str, ...]]] = set()
    for file_id in sorted(current):
        for path in current[file_id].paths:
            local_key = (file_id, path.root_id, path.parts)
            if local_key in seen_local_targets:
                continue
            seen_local_targets.add(local_key)
            key = (path.root_id, path.parts)
            if len(owners[key]) > 1:
                candidate = replace(
                    path,
                    parts=(
                        *path.parts[:-1],
                        _suffix_name(path.parts[-1], file_id),
                    ),
                )
            else:
                candidate = path
            entries.append((file_id, path, candidate))

    for _ in range(len(entries) + 1):
        final_owners: dict[tuple[str, tuple[str, ...]], list[int]] = {}
        for index, (_, _, candidate) in enumerate(entries):
            final_owners.setdefault(
                (candidate.root_id, candidate.parts), []
            ).append(index)
        conflicts = [
            indexes
            for indexes in final_owners.values()
            if len(indexes) > 1
        ]
        if not conflicts:
            break
        if any(
            len({entries[index][0] for index in indexes}) == 1
            for indexes in conflicts
        ):
            raise DriveRagError(
                "deterministic suffixing created duplicate targets for one file",
                code="INVALID_INVENTORY",
            )
        changed = False
        for indexes in conflicts:
            for index in indexes:
                file_id, original, candidate = entries[index]
                suffixed = replace(
                    original,
                    parts=(
                        *original.parts[:-1],
                        _suffix_name(original.parts[-1], file_id),
                    ),
                )
                if suffixed != candidate:
                    entries[index] = (file_id, original, suffixed)
                    changed = True
        if not changed:
            raise DriveRagError(
                "file-name collision remains after deterministic ID suffixing",
                code="INVALID_INVENTORY",
            )
    else:  # pragma: no cover - bounded defensive guard
        raise DriveRagError(
            "could not resolve file-name collisions",
            code="INVALID_INVENTORY",
        )

    targets: dict[str, list[RemotePath]] = {file_id: [] for file_id in sorted(current)}
    final_targets: dict[tuple[str, tuple[str, ...]], str] = {}
    for file_id, _, candidate in entries:
        final_key = (candidate.root_id, candidate.parts)
        if final_key in final_targets:
            raise DriveRagError(
                "duplicate final target remains after collision resolution",
                code="INVALID_INVENTORY",
            )
        final_targets[final_key] = file_id
        targets[file_id].append(candidate)
    return {file_id: tuple(paths) for file_id, paths in targets.items()}


def _suffix_name(name: str, file_id: str) -> str:
    source = PurePosixPath(name)
    suffix = source.suffix
    stem = name[: -len(suffix)] if suffix else name
    digest = hashlib.sha256(file_id.encode("utf-8")).hexdigest()[:8]
    return f"{stem}__{digest}{suffix}"
