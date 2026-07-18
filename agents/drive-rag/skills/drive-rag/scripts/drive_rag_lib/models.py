"""Strict, serializable domain models for Drive RAG state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .protocol import DriveRagError


INDEXED = "indexed"
UNINDEXED = "unindexed"
UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
EXTRACTION_LIMIT_EXCEEDED = "EXTRACTION_LIMIT_EXCEEDED"


def _require_keys(
    payload: Mapping[str, object], expected: set[str], model_name: str
) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected keys: {', '.join(unexpected)}")
        raise DriveRagError(
            f"invalid {model_name}: {'; '.join(details)}",
            code="INVALID_STATE",
        )


def _require_string(value: object, field: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise DriveRagError(f"{field} must be a string", code="INVALID_STATE")
    return value


def _require_string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DriveRagError(
            f"{field} must be a list of strings",
            code="INVALID_STATE",
        )
    return tuple(value)


@dataclass(frozen=True)
class FolderConfig:
    folder_id: str
    url: str
    alias: str
    enabled: bool = True

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "FolderConfig":
        _require_keys(payload, {"folder_id", "url", "alias", "enabled"}, "folder")
        folder_id = _require_string(payload["folder_id"], "folder_id")
        url = _require_string(payload["url"], "url")
        alias = _require_string(payload["alias"], "alias")
        enabled = payload["enabled"]
        if not isinstance(enabled, bool):
            raise DriveRagError("enabled must be a boolean", code="INVALID_STATE")
        assert folder_id is not None and url is not None and alias is not None
        return cls(folder_id, url, alias, enabled)

    def to_dict(self) -> dict[str, object]:
        return {
            "folder_id": self.folder_id,
            "url": self.url,
            "alias": self.alias,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class RemotePath:
    root_id: str
    parent_ids: tuple[str, ...]
    parts: tuple[str, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RemotePath":
        _require_keys(payload, {"root_id", "parent_ids", "parts"}, "remote path")
        root_id = _require_string(payload["root_id"], "root_id")
        assert root_id is not None
        return cls(
            root_id=root_id,
            parent_ids=_require_string_tuple(payload["parent_ids"], "parent_ids"),
            parts=_require_string_tuple(payload["parts"], "parts"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "root_id": self.root_id,
            "parent_ids": list(self.parent_ids),
            "parts": list(self.parts),
        }


@dataclass(frozen=True)
class RemoteFile:
    file_id: str
    name: str
    mime_type: str
    revision: str
    modified_time: str
    drive_url: str
    checksum: str | None
    size: int | None
    paths: tuple[RemotePath, ...]
    native_kind: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RemoteFile":
        expected = {
            "file_id",
            "name",
            "mime_type",
            "revision",
            "modified_time",
            "drive_url",
            "checksum",
            "size",
            "paths",
            "native_kind",
        }
        _require_keys(payload, expected, "remote file")
        size = payload["size"]
        if size is not None and (not isinstance(size, int) or isinstance(size, bool)):
            raise DriveRagError("size must be an integer or null", code="INVALID_STATE")
        raw_paths = payload["paths"]
        if not isinstance(raw_paths, list) or any(
            not isinstance(item, dict) for item in raw_paths
        ):
            raise DriveRagError("paths must be a list of objects", code="INVALID_STATE")

        string_fields = {
            field: _require_string(payload[field], field)
            for field in (
                "file_id",
                "name",
                "mime_type",
                "revision",
                "modified_time",
                "drive_url",
            )
        }
        assert all(value is not None for value in string_fields.values())
        return cls(
            file_id=string_fields["file_id"],  # type: ignore[arg-type]
            name=string_fields["name"],  # type: ignore[arg-type]
            mime_type=string_fields["mime_type"],  # type: ignore[arg-type]
            revision=string_fields["revision"],  # type: ignore[arg-type]
            modified_time=string_fields["modified_time"],  # type: ignore[arg-type]
            drive_url=string_fields["drive_url"],  # type: ignore[arg-type]
            checksum=_require_string(payload["checksum"], "checksum", nullable=True),
            size=size,
            paths=tuple(RemotePath.from_dict(item) for item in raw_paths),
            native_kind=_require_string(
                payload["native_kind"], "native_kind", nullable=True
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "revision": self.revision,
            "modified_time": self.modified_time,
            "drive_url": self.drive_url,
            "checksum": self.checksum,
            "size": self.size,
            "paths": [path.to_dict() for path in self.paths],
            "native_kind": self.native_kind,
        }


@dataclass(frozen=True)
class RemoteInventory:
    run_id: str
    complete: bool
    root_ids: tuple[str, ...]
    files: tuple[RemoteFile, ...]
    incomplete_reason: str | None
    generated_at: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RemoteInventory":
        _require_keys(
            payload,
            {
                "run_id",
                "complete",
                "root_ids",
                "files",
                "incomplete_reason",
                "generated_at",
            },
            "remote inventory",
        )
        complete = payload["complete"]
        if not isinstance(complete, bool):
            raise DriveRagError("complete must be a boolean", code="INVALID_STATE")
        raw_files = payload["files"]
        if not isinstance(raw_files, list) or any(
            not isinstance(item, dict) for item in raw_files
        ):
            raise DriveRagError("files must be a list of objects", code="INVALID_STATE")
        run_id = _require_string(payload["run_id"], "run_id")
        generated_at = _require_string(payload["generated_at"], "generated_at")
        assert run_id is not None and generated_at is not None
        return cls(
            run_id,
            complete,
            _require_string_tuple(payload["root_ids"], "root_ids"),
            tuple(RemoteFile.from_dict(item) for item in raw_files),
            _require_string(
                payload["incomplete_reason"], "incomplete_reason", nullable=True
            ),
            generated_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "complete": self.complete,
            "root_ids": list(self.root_ids),
            "files": [remote.to_dict() for remote in self.files],
            "incomplete_reason": self.incomplete_reason,
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True)
class ManifestFile:
    file_id: str
    revision: str
    checksum: str | None
    object_sha256: str | None
    paths: tuple[RemotePath, ...]
    active_chunk_ids: tuple[str, ...]
    native_kind: str | None = None
    index_status: str = INDEXED
    index_reason: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ManifestFile":
        _require_keys(
            payload,
            {
                "file_id",
                "revision",
                "checksum",
                "object_sha256",
                "paths",
                "active_chunk_ids",
                "native_kind",
                "index_status",
                "index_reason",
            },
            "manifest file",
        )
        raw_paths = payload["paths"]
        if not isinstance(raw_paths, list) or any(
            not isinstance(item, dict) for item in raw_paths
        ):
            raise DriveRagError("paths must be a list of objects", code="INVALID_STATE")
        file_id = _require_string(payload["file_id"], "file_id")
        revision = _require_string(payload["revision"], "revision")
        assert file_id is not None and revision is not None
        return cls(
            file_id,
            revision,
            _require_string(payload["checksum"], "checksum", nullable=True),
            _require_string(payload["object_sha256"], "object_sha256", nullable=True),
            tuple(RemotePath.from_dict(item) for item in raw_paths),
            _require_string_tuple(payload["active_chunk_ids"], "active_chunk_ids"),
            _require_string(payload["native_kind"], "native_kind", nullable=True),
            _require_string(payload["index_status"], "index_status"),
            _require_string(payload["index_reason"], "index_reason", nullable=True),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "revision": self.revision,
            "checksum": self.checksum,
            "object_sha256": self.object_sha256,
            "paths": [path.to_dict() for path in self.paths],
            "active_chunk_ids": list(self.active_chunk_ids),
            "native_kind": self.native_kind,
            "index_status": self.index_status,
            "index_reason": self.index_reason,
        }


@dataclass(frozen=True)
class Manifest:
    files: dict[str, ManifestFile]
    model_identity: str | None
    last_success: str | None
    last_failure: str | None
    root_ids: tuple[str, ...] = ()
    last_inventory_generated_at: str | None = None
    coverage: str = "complete"
    coverage_reason: str | None = None

    @classmethod
    def empty(cls) -> "Manifest":
        return cls({}, None, None, None)

    @classmethod
    def from_remote(cls, inventory: RemoteInventory) -> "Manifest":
        files: dict[str, ManifestFile] = {}
        for remote in inventory.files:
            committed = files.get(remote.file_id)
            if committed is None:
                files[remote.file_id] = ManifestFile(
                    remote.file_id,
                    remote.revision,
                    remote.checksum,
                    None,
                    tuple(
                        sorted(
                            set(remote.paths),
                            key=lambda path: (path.root_id, path.parent_ids, path.parts),
                        )
                    ),
                    (),
                    remote.native_kind,
                    INDEXED,
                    None,
                )
                continue
            if (committed.revision, committed.checksum) != (
                remote.revision,
                remote.checksum,
            ):
                raise DriveRagError(
                    f"conflicting duplicate file ID: {remote.file_id}",
                    code="INVALID_INVENTORY",
                )
            files[remote.file_id] = ManifestFile(
                committed.file_id,
                committed.revision,
                committed.checksum,
                committed.object_sha256,
                tuple(
                    sorted(
                        set(committed.paths) | set(remote.paths),
                        key=lambda path: (path.root_id, path.parent_ids, path.parts),
                    )
                ),
                committed.active_chunk_ids,
                committed.native_kind,
                committed.index_status,
                committed.index_reason,
            )
        return cls(
            files,
            None,
            inventory.run_id,
            None,
            inventory.root_ids,
            inventory.generated_at,
            "complete" if inventory.complete else "partial",
            inventory.incomplete_reason,
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Manifest":
        required = {
                "files",
                "model_identity",
                "last_success",
                "last_failure",
                "root_ids",
                "last_inventory_generated_at",
            }
        extended = required | {"coverage", "coverage_reason"}
        if set(payload) not in (required, extended):
            raise DriveRagError("manifest fields are invalid", code="INVALID_STATE")
        raw_files = payload["files"]
        if not isinstance(raw_files, dict) or any(
            not isinstance(file_id, str) or not isinstance(item, dict)
            for file_id, item in raw_files.items()
        ):
            raise DriveRagError("files must be an object", code="INVALID_STATE")
        files = {
            file_id: ManifestFile.from_dict(item) for file_id, item in raw_files.items()
        }
        if any(file_id != item.file_id for file_id, item in files.items()):
            raise DriveRagError(
                "manifest file key must match file_id", code="INVALID_STATE"
            )
        coverage = payload.get("coverage", "complete")
        coverage_reason = payload.get("coverage_reason")
        if coverage not in {"complete", "partial"}:
            raise DriveRagError("manifest coverage is invalid", code="INVALID_STATE")
        if coverage == "complete" and coverage_reason is not None:
            raise DriveRagError(
                "complete manifest must not have a coverage reason",
                code="INVALID_STATE",
            )
        if coverage == "partial" and not isinstance(coverage_reason, str):
            raise DriveRagError(
                "partial manifest requires a coverage reason",
                code="INVALID_STATE",
            )
        return cls(
            files,
            _require_string(payload["model_identity"], "model_identity", nullable=True),
            _require_string(payload["last_success"], "last_success", nullable=True),
            _require_string(payload["last_failure"], "last_failure", nullable=True),
            _require_string_tuple(payload["root_ids"], "root_ids"),
            _require_string(
                payload["last_inventory_generated_at"],
                "last_inventory_generated_at",
                nullable=True,
            ),
            coverage,
            coverage_reason,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "files": {
                file_id: self.files[file_id].to_dict() for file_id in sorted(self.files)
            },
            "model_identity": self.model_identity,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "root_ids": list(self.root_ids),
            "last_inventory_generated_at": self.last_inventory_generated_at,
            "coverage": self.coverage,
            "coverage_reason": self.coverage_reason,
        }


@dataclass(frozen=True)
class SyncPlan:
    run_id: str
    downloads: tuple[RemoteFile, ...]
    unchanged_file_ids: tuple[str, ...]
    deleted_file_ids: tuple[str, ...]
    target_paths: dict[str, tuple[RemotePath, ...]]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "SyncPlan":
        _require_keys(
            payload,
            {
                "run_id",
                "downloads",
                "unchanged_file_ids",
                "deleted_file_ids",
                "target_paths",
            },
            "sync plan",
        )
        run_id = _require_string(payload["run_id"], "run_id")
        raw_downloads = payload["downloads"]
        raw_targets = payload["target_paths"]
        if not isinstance(raw_downloads, list) or any(
            not isinstance(item, dict) for item in raw_downloads
        ):
            raise DriveRagError(
                "downloads must be a list of objects", code="INVALID_STATE"
            )
        if not isinstance(raw_targets, dict) or any(
            not isinstance(file_id, str)
            or not isinstance(paths, list)
            or any(not isinstance(path, dict) for path in paths)
            for file_id, paths in raw_targets.items()
        ):
            raise DriveRagError(
                "target_paths must map file IDs to path lists",
                code="INVALID_STATE",
            )
        assert run_id is not None
        return cls(
            run_id,
            tuple(RemoteFile.from_dict(item) for item in raw_downloads),
            _require_string_tuple(
                payload["unchanged_file_ids"], "unchanged_file_ids"
            ),
            _require_string_tuple(payload["deleted_file_ids"], "deleted_file_ids"),
            {
                file_id: tuple(RemotePath.from_dict(path) for path in paths)
                for file_id, paths in raw_targets.items()
            },
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "downloads": [remote.to_dict() for remote in self.downloads],
            "unchanged_file_ids": list(self.unchanged_file_ids),
            "deleted_file_ids": list(self.deleted_file_ids),
            "target_paths": {
                file_id: [path.to_dict() for path in self.target_paths[file_id]]
                for file_id in sorted(self.target_paths)
            },
        }


@dataclass(frozen=True)
class Artifact:
    file_id: str
    revision: str
    payload_path: str
    payload_sha256: str
    structured_path: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Artifact":
        _require_keys(
            payload,
            {
                "file_id",
                "revision",
                "payload_path",
                "payload_sha256",
                "structured_path",
            },
            "artifact",
        )
        file_id = _require_string(payload["file_id"], "file_id")
        revision = _require_string(payload["revision"], "revision")
        payload_path = _require_string(payload["payload_path"], "payload_path")
        payload_sha256 = _require_string(
            payload["payload_sha256"], "payload_sha256"
        )
        assert file_id is not None
        assert revision is not None
        assert payload_path is not None
        assert payload_sha256 is not None
        return cls(
            file_id,
            revision,
            payload_path,
            payload_sha256,
            _require_string(
                payload["structured_path"], "structured_path", nullable=True
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "revision": self.revision,
            "payload_path": self.payload_path,
            "payload_sha256": self.payload_sha256,
            "structured_path": self.structured_path,
        }


@dataclass(frozen=True)
class ArtifactSet:
    run_id: str
    artifacts: tuple[Artifact, ...]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ArtifactSet":
        _require_keys(payload, {"run_id", "artifacts"}, "artifact set")
        run_id = _require_string(payload["run_id"], "run_id")
        raw_artifacts = payload["artifacts"]
        if not isinstance(raw_artifacts, list) or any(
            not isinstance(item, dict) for item in raw_artifacts
        ):
            raise DriveRagError(
                "artifacts must be a list of objects", code="INVALID_STATE"
            )
        assert run_id is not None
        return cls(
            run_id,
            tuple(Artifact.from_dict(item) for item in raw_artifacts),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class PreparedReference:
    file_id: str
    path: str
    sha256: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PreparedReference":
        _require_keys(payload, {"file_id", "path", "sha256"}, "prepared reference")
        file_id = _require_string(payload["file_id"], "file_id")
        path = _require_string(payload["path"], "path")
        sha256 = _require_string(payload["sha256"], "sha256")
        assert file_id is not None and path is not None and sha256 is not None
        return cls(file_id, path, sha256)

    def to_dict(self) -> dict[str, object]:
        return {"file_id": self.file_id, "path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class ActivationReference:
    path: str
    sha256: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ActivationReference":
        _require_keys(payload, {"path", "sha256"}, "activation reference")
        path = _require_string(payload["path"], "path")
        sha256 = _require_string(payload["sha256"], "sha256")
        assert path is not None and sha256 is not None
        return cls(path, sha256)

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class Journal:
    run_id: str
    phase: str
    inventory: RemoteInventory
    artifacts: ArtifactSet
    plan: SyncPlan
    base_manifest: Manifest
    target_manifest: Manifest | None
    base_folders: tuple[FolderConfig, ...]
    target_folders: tuple[FolderConfig, ...]
    prepared: tuple[PreparedReference, ...]
    activation: tuple[ActivationReference, ...] | None
    changed: bool

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Journal":
        expected = {
            "run_id",
            "phase",
            "inventory",
            "artifacts",
            "plan",
            "base_manifest",
            "target_manifest",
            "base_folders",
            "target_folders",
            "prepared",
            "activation",
            "changed",
        }
        if set(payload) not in (expected, expected - {"activation"}):
            raise DriveRagError("journal fields are invalid", code="INVALID_STATE")
        run_id = _require_string(payload["run_id"], "run_id")
        phase = _require_string(payload["phase"], "phase")
        raw_inventory = payload["inventory"]
        raw_artifacts = payload["artifacts"]
        raw_plan = payload["plan"]
        raw_manifest = payload["base_manifest"]
        raw_target_manifest = payload["target_manifest"]
        raw_base_folders = payload["base_folders"]
        raw_target_folders = payload["target_folders"]
        raw_prepared = payload["prepared"]
        raw_activation = payload.get("activation")
        changed = payload["changed"]
        if (
            not isinstance(raw_inventory, dict)
            or not isinstance(raw_artifacts, dict)
            or not isinstance(raw_plan, dict)
            or not isinstance(raw_manifest, dict)
            or (
                raw_target_manifest is not None
                and not isinstance(raw_target_manifest, dict)
            )
            or (
                raw_activation is not None
                and not isinstance(raw_activation, (dict, list))
            )
        ):
            raise DriveRagError(
                "journal inventory, artifacts, plan, and manifest must be objects",
                code="INVALID_STATE",
            )
        if not isinstance(raw_prepared, list) or any(
            not isinstance(item, dict) for item in raw_prepared
        ):
            raise DriveRagError(
                "journal prepared references must be a list of objects",
                code="INVALID_STATE",
            )
        if isinstance(raw_activation, list) and any(
            not isinstance(item, dict) for item in raw_activation
        ):
            raise DriveRagError(
                "journal activation references must be a list of objects",
                code="INVALID_STATE",
            )
        for field, folders in (
            ("base_folders", raw_base_folders),
            ("target_folders", raw_target_folders),
        ):
            if not isinstance(folders, list) or any(
                not isinstance(item, dict) for item in folders
            ):
                raise DriveRagError(
                    f"journal {field} must be a list of folders",
                    code="INVALID_STATE",
                )
        if not isinstance(changed, bool):
            raise DriveRagError("journal changed must be a boolean", code="INVALID_STATE")
        assert run_id is not None and phase is not None
        return cls(
            run_id,
            phase,
            RemoteInventory.from_dict(raw_inventory),
            ArtifactSet.from_dict(raw_artifacts),
            SyncPlan.from_dict(raw_plan),
            Manifest.from_dict(raw_manifest),
            (
                Manifest.from_dict(raw_target_manifest)
                if isinstance(raw_target_manifest, dict)
                else None
            ),
            tuple(FolderConfig.from_dict(item) for item in raw_base_folders),
            tuple(FolderConfig.from_dict(item) for item in raw_target_folders),
            tuple(PreparedReference.from_dict(item) for item in raw_prepared),
            (
                (ActivationReference.from_dict(raw_activation),)
                if isinstance(raw_activation, dict)
                else tuple(
                    ActivationReference.from_dict(item)
                    for item in raw_activation
                    if isinstance(item, dict)
                )
                if isinstance(raw_activation, list)
                and all(isinstance(item, dict) for item in raw_activation)
                else None
            ),
            changed,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "phase": self.phase,
            "inventory": self.inventory.to_dict(),
            "artifacts": self.artifacts.to_dict(),
            "plan": self.plan.to_dict(),
            "base_manifest": self.base_manifest.to_dict(),
            "target_manifest": (
                self.target_manifest.to_dict()
                if self.target_manifest is not None
                else None
            ),
            "base_folders": [item.to_dict() for item in self.base_folders],
            "target_folders": [item.to_dict() for item in self.target_folders],
            "prepared": [item.to_dict() for item in self.prepared],
            "activation": (
                [item.to_dict() for item in self.activation]
                if self.activation is not None
                else None
            ),
            "changed": self.changed,
        }


@dataclass(frozen=True)
class ExtractedBlock:
    locator: str
    text: str
    metadata: dict[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ExtractedBlock":
        _require_keys(payload, {"locator", "text", "metadata"}, "extracted block")
        locator = _require_string(payload["locator"], "locator")
        text = _require_string(payload["text"], "text")
        metadata = payload["metadata"]
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) for key in metadata
        ):
            raise DriveRagError("metadata must be an object", code="INVALID_STATE")
        assert locator is not None and text is not None
        return cls(locator, text, dict(metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "locator": self.locator,
            "text": self.text,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class ExtractedDocument:
    file_id: str
    revision: str
    blocks: tuple[ExtractedBlock, ...]
    format: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ExtractedDocument":
        _require_keys(
            payload,
            {"file_id", "revision", "blocks", "format"},
            "extracted document",
        )
        raw_blocks = payload["blocks"]
        if not isinstance(raw_blocks, list) or any(
            not isinstance(item, dict) for item in raw_blocks
        ):
            raise DriveRagError("blocks must be a list of objects", code="INVALID_STATE")
        file_id = _require_string(payload["file_id"], "file_id")
        revision = _require_string(payload["revision"], "revision")
        document_format = _require_string(payload["format"], "format")
        assert file_id is not None and revision is not None and document_format is not None
        return cls(
            file_id,
            revision,
            tuple(ExtractedBlock.from_dict(item) for item in raw_blocks),
            document_format,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "revision": self.revision,
            "blocks": [block.to_dict() for block in self.blocks],
            "format": self.format,
        }


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    text: str
    locator: str
    metadata: dict[str, object]

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Chunk":
        _require_keys(payload, {"chunk_id", "text", "locator", "metadata"}, "chunk")
        chunk_id = _require_string(payload["chunk_id"], "chunk_id")
        text = _require_string(payload["text"], "text")
        locator = _require_string(payload["locator"], "locator")
        metadata = payload["metadata"]
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) for key in metadata
        ):
            raise DriveRagError("metadata must be an object", code="INVALID_STATE")
        assert chunk_id is not None and text is not None and locator is not None
        return cls(chunk_id, text, locator, dict(metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "locator": self.locator,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class Evidence:
    excerpt: str
    file_id: str
    file_name: str
    folder_alias: str
    drive_path: str
    drive_url: str
    local_path: str
    locator: str
    revision: str
    content_hash: str
    mime_type: str
    distance: float

    def to_dict(self) -> dict[str, object]:
        return {
            "excerpt": self.excerpt,
            "file_id": self.file_id,
            "file_name": self.file_name,
            "folder_alias": self.folder_alias,
            "drive_path": self.drive_path,
            "drive_url": self.drive_url,
            "local_path": self.local_path,
            "locator": self.locator,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "mime_type": self.mime_type,
            "distance": self.distance,
        }
