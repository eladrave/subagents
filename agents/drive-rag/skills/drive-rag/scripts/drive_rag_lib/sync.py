"""Crash-safe reconciliation of verified Drive artifacts, mirrors, and Chroma."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Mapping, Sequence

import fitz

from .chunk import chunk_document
from .aliases import alias_key, canonical_alias
from .embed import Embedder
from .extract import (
    extract_file,
    extract_native_structured,
    require_bounded_file,
)
from .index import ChromaIndex
from .inventory import _validate_manifest, plan_sync, prove_complete
from .models import (
    INDEXED,
    UNINDEXED,
    UNSUPPORTED_FORMAT,
    ActivationReference,
    Artifact,
    ArtifactSet,
    Chunk,
    FolderConfig,
    Journal,
    Manifest,
    ManifestFile,
    PreparedReference,
    RemoteFile,
    RemoteInventory,
    RemotePath,
    SyncPlan,
)
from .paths import ensure_state_root, resolve_below
from .protocol import (
    INDEX_STALE,
    SCHEMA_VERSION,
    SYNC_FAILED_PREVIOUS_VERSION_ACTIVE,
    SYNC_OK_CHANGED,
    SYNC_OK_NO_CHANGES,
    DriveRagError,
    atomic_write_json,
    read_json,
)
from .registry import Registry
from .schedule import schedule_state


JOURNAL_PHASES = (
    "planned",
    "verified",
    "indexed",
    "promoted",
    "deleted",
    "activating",
    "committed",
)
_PHASE_INDEX = {phase: index for index, phase in enumerate(JOURNAL_PHASES)}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ACTIVATION_SHARD_BYTES = 16 * 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_artifact_set(path: Path) -> ArtifactSet:
    """Load an exact schema-1 connector artifact set."""

    require_bounded_file(path, "artifact set")
    payload = read_json(path)
    if set(payload) != {"schema_version", "run_id", "artifacts"}:
        raise DriveRagError(
            "artifact set must contain exactly the schema-1 fields",
            code="INVALID_ARTIFACT",
        )
    if payload.pop("schema_version") != SCHEMA_VERSION:
        raise DriveRagError(
            "artifact set schema is unsupported", code="UNSUPPORTED_SCHEMA"
        )
    try:
        return ArtifactSet.from_dict(payload)
    except DriveRagError as exc:
        raise DriveRagError(str(exc), code="INVALID_ARTIFACT") from exc


@dataclass(frozen=True)
class SyncResult:
    status: str
    engine: "SyncEngine"


@dataclass(frozen=True)
class SyncStatus:
    folder_count: int
    last_success: str | None
    last_failure: str | None
    indexed_files: int
    indexed_chunks: int
    unindexed_files: int
    unindexed_reasons: Mapping[str, int]
    model_identity: str
    pending_journal: bool
    schedule_state: str

    def to_dict(self) -> dict[str, object]:
        return {
            "folder_count": self.folder_count,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "indexed_files": self.indexed_files,
            "indexed_chunks": self.indexed_chunks,
            "unindexed_files": self.unindexed_files,
            "unindexed_reasons": dict(self.unindexed_reasons),
            "model_identity": self.model_identity,
            "pending_journal": self.pending_journal,
            "schedule_state": self.schedule_state,
        }


@dataclass(frozen=True)
class _PreparedFile:
    file_id: str
    revision: str
    mime_type: str
    object_sha256: str
    chunks: tuple[Chunk, ...]
    embeddings: tuple[tuple[float, ...], ...]
    index_status: str
    index_reason: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "_PreparedFile":
        expected = {
            "file_id",
            "revision",
            "mime_type",
            "object_sha256",
            "chunks",
            "embeddings",
            "index_status",
            "index_reason",
        }
        if set(payload) != expected:
            raise DriveRagError(
                "prepared artifact has invalid fields", code="INVALID_JOURNAL"
            )
        strings = [
            payload["file_id"],
            payload["revision"],
            payload["mime_type"],
            payload["object_sha256"],
        ]
        if any(not isinstance(value, str) or not value.strip() for value in strings):
            raise DriveRagError(
                "prepared artifact identity is invalid", code="INVALID_JOURNAL"
            )
        if _SHA256.fullmatch(payload["object_sha256"]) is None:
            raise DriveRagError(
                "prepared object hash is invalid", code="INVALID_JOURNAL"
            )
        raw_chunks = payload["chunks"]
        raw_embeddings = payload["embeddings"]
        if not isinstance(raw_chunks, list) or any(
            not isinstance(item, dict) for item in raw_chunks
        ):
            raise DriveRagError(
                "prepared chunks are invalid", code="INVALID_JOURNAL"
            )
        if not isinstance(raw_embeddings, list) or any(
            not isinstance(vector, list) for vector in raw_embeddings
        ):
            raise DriveRagError(
                "prepared embeddings are invalid", code="INVALID_JOURNAL"
            )
        embeddings: list[tuple[float, ...]] = []
        for vector in raw_embeddings:
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in vector
            ):
                raise DriveRagError(
                    "prepared embedding values are invalid", code="INVALID_JOURNAL"
                )
            embeddings.append(tuple(float(value) for value in vector))
        chunks = tuple(Chunk.from_dict(item) for item in raw_chunks)
        if len(chunks) != len(embeddings):
            raise DriveRagError(
                "prepared chunk and embedding counts differ", code="INVALID_JOURNAL"
            )
        index_status = payload["index_status"]
        index_reason = payload["index_reason"]
        if index_status == INDEXED:
            if index_reason is not None:
                raise DriveRagError(
                    "indexed prepared artifact has an index reason",
                    code="INVALID_JOURNAL",
                )
        elif index_status == UNINDEXED:
            if index_reason != UNSUPPORTED_FORMAT or chunks or embeddings:
                raise DriveRagError(
                    "unindexed prepared artifact is inconsistent",
                    code="INVALID_JOURNAL",
                )
        else:
            raise DriveRagError(
                "prepared artifact index status is invalid", code="INVALID_JOURNAL"
            )
        return cls(
            payload["file_id"],
            payload["revision"],
            payload["mime_type"],
            payload["object_sha256"],
            chunks,
            tuple(embeddings),
            index_status,
            index_reason,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "revision": self.revision,
            "mime_type": self.mime_type,
            "object_sha256": self.object_sha256,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "embeddings": [list(vector) for vector in self.embeddings],
            "index_status": self.index_status,
            "index_reason": self.index_reason,
        }


class SyncEngine:
    def __init__(
        self,
        state_root: Path,
        embedder: Embedder,
        index: ChromaIndex,
        phase_callback: Callable[[str], None] | None,
    ) -> None:
        self.state_root = state_root
        self.embedder = embedder
        self.index = index
        self.phase_callback = phase_callback
        self._journal_path = self.state_root / "journal" / "pending.json"
        self._manifest_path = self.state_root / "manifests" / "current.json"
        self._folders_path = self.state_root / "manifests" / "folders.json"

    @classmethod
    def open(
        cls,
        state_root: Path,
        embedder: Embedder,
        *,
        phase_callback: Callable[[str], None] | None = None,
        create_index_if_missing: bool = True,
    ) -> "SyncEngine":
        state = ensure_state_root(state_root)
        model_id = getattr(embedder, "model_id", None)
        dimension = getattr(embedder, "dimension", None)
        if not isinstance(model_id, str) or not model_id.strip():
            raise DriveRagError("embedder model ID is invalid", code="INVALID_INDEX_INPUT")
        if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
            raise DriveRagError(
                "embedder dimension is invalid", code="INVALID_INDEX_INPUT"
            )
        index = ChromaIndex(
            state / "chroma",
            model_id,
            dimension,
            create_if_missing=create_index_if_missing,
        )
        return cls(state, embedder, index, phase_callback)

    def manifest(self) -> Manifest:
        from .inventory import load_manifest

        return load_manifest(self._manifest_path)

    def object_path(self, file_id: str, object_sha256: str | None) -> Path:
        if not isinstance(file_id, str) or not file_id.strip():
            raise DriveRagError("file ID must not be empty", code="INVALID_STATE")
        if not isinstance(object_sha256, str) or _SHA256.fullmatch(object_sha256) is None:
            raise DriveRagError("object hash is invalid", code="INVALID_STATE")
        identity = hashlib.sha256(file_id.encode("utf-8")).hexdigest()
        return resolve_below(
            self.state_root,
            self.state_root / "objects" / identity / f"{object_sha256}.payload",
        )

    def has_pending_journal(self) -> bool:
        return self._journal_path.exists() or self._journal_path.is_symlink()

    def apply(
        self, inventory: RemoteInventory, artifacts: ArtifactSet
    ) -> SyncResult:
        pending = self._load_journal(required=False)
        if pending is not None:
            if pending.run_id == inventory.run_id:
                if pending.inventory != inventory or pending.artifacts != artifacts:
                    raise DriveRagError(
                        "pending run identity differs from retry input",
                        code="INVALID_JOURNAL",
                    )
                return self.recover()
            self.recover()

        base_manifest = self.manifest()
        target_folders = tuple(
            folder for folder in Registry.load(self.state_root).list() if folder.enabled
        )
        target_root_ids = {folder.folder_id for folder in target_folders}
        try:
            if self._is_committed_retry(
                inventory, artifacts, base_manifest, target_folders
            ):
                self.index.assert_manifest_consistent(base_manifest)
                return SyncResult(SYNC_OK_NO_CHANGES, self)
            self.index.assert_manifest_consistent(base_manifest)
            prove_complete(inventory, target_root_ids)
            plan = plan_sync(inventory, base_manifest, target_root_ids)
            base_folders = self._load_committed_folders(base_manifest)
            remotes = self._remote_files(inventory)
            plan = self._resolve_rendered_collisions(plan, remotes)
            self._validate_rendered_targets(
                plan.target_paths,
                remotes,
                self._folder_map(target_folders),
            )
            self._validate_artifact_metadata(inventory, artifacts, plan.downloads)
            changed = self._plan_changed(
                plan,
                base_manifest,
                base_folders,
                target_folders,
            )
            journal = Journal(
                inventory.run_id,
                "planned",
                inventory,
                artifacts,
                plan,
                base_manifest,
                None,
                base_folders,
                target_folders,
                (),
                None,
                changed,
            )
            self._validate_journal(journal)
        except DriveRagError:
            self._mark_failure(base_manifest)
            raise

        self._write_journal(journal)
        self._notify("planned")
        try:
            return self._resume(journal)
        except DriveRagError:
            current = self._load_journal(required=False)
            if current is not None and current.phase == "committed":
                raise
            if current is not None and current.phase == "planned":
                self._discard_pending(current)
            self._mark_failure(base_manifest)
            raise

    def recover(self) -> SyncResult:
        journal = self._load_journal(required=False)
        if journal is None:
            return SyncResult(SYNC_OK_NO_CHANGES, self)
        self._assert_observed_manifest(journal)
        if journal.phase == "committed":
            status = SYNC_OK_CHANGED if journal.changed else SYNC_OK_NO_CHANGES
            self._discard_pending(journal)
            return SyncResult(status, self)
        try:
            return self._resume(journal)
        except DriveRagError:
            self._mark_failure(journal.base_manifest)
            raise

    def status(self) -> SyncStatus:
        registry = Registry.load(self.state_root)
        manifest = self.manifest()
        committed_chunks = {
            chunk_id
            for committed in manifest.files.values()
            for chunk_id in committed.active_chunk_ids
        }
        configured_schedule = schedule_state(self.state_root)
        unindexed_reasons = Counter(
            committed.index_reason
            for committed in manifest.files.values()
            if committed.index_status == UNINDEXED
            and committed.index_reason is not None
        )
        return SyncStatus(
            len(registry.list()),
            manifest.last_success,
            manifest.last_failure,
            sum(
                1
                for committed in manifest.files.values()
                if committed.index_status == INDEXED
            ),
            len(committed_chunks),
            sum(
                1
                for committed in manifest.files.values()
                if committed.index_status == UNINDEXED
            ),
            dict(sorted(unindexed_reasons.items())),
            self._model_identity(),
            self.has_pending_journal(),
            configured_schedule,
        )

    def assert_query_ready(self) -> Manifest:
        if self.has_pending_journal():
            raise DriveRagError(
                "a pending synchronization journal makes the index stale",
                code=INDEX_STALE,
            )
        manifest = self.manifest()
        enabled = {
            folder.folder_id
            for folder in Registry.load(self.state_root).list()
            if folder.enabled
        }
        if (
            not manifest.last_success
            or manifest.last_failure is not None
            or manifest.model_identity != self._model_identity()
            or set(manifest.root_ids) != enabled
        ):
            raise DriveRagError(
                "the persistent index is not synchronized with current configuration",
                code=INDEX_STALE,
            )
        self.index.assert_manifest_consistent(manifest)
        return manifest

    def _resume(self, journal: Journal) -> SyncResult:
        self._assert_observed_manifest(journal)
        if _PHASE_INDEX[journal.phase] >= _PHASE_INDEX["verified"]:
            expected_target = self._build_target_manifest(journal)
            if journal.target_manifest != expected_target:
                raise DriveRagError(
                    "journal target manifest differs from prepared state",
                    code="INVALID_JOURNAL",
                )
        if journal.phase == "planned":
            prepared = self._prepare(journal)
            journal = replace(journal, prepared=prepared)
            target_manifest = self._build_target_manifest(journal)
            journal = replace(
                journal,
                phase="verified",
                target_manifest=target_manifest,
            )
            self._write_journal(journal)
            self._notify("verified")
        if journal.phase == "verified":
            self._index_files(journal)
            journal = self._advance(journal, "indexed")
        if journal.phase == "indexed":
            self._promote(journal)
            journal = self._advance(journal, "promoted")
        if journal.phase == "promoted":
            self._delete_stale(journal)
            journal = self._advance(journal, "deleted")
        if journal.phase == "deleted":
            journal = self._prepare_activation(journal)
        if journal.phase == "activating":
            self._commit(journal)
            journal = self._advance(journal, "committed")
        if journal.phase != "committed":
            raise DriveRagError("journal phase is invalid", code="INVALID_JOURNAL")
        status = SYNC_OK_CHANGED if journal.changed else SYNC_OK_NO_CHANGES
        self._discard_pending(journal)
        return SyncResult(status, self)

    def _advance(self, journal: Journal, phase: str) -> Journal:
        advanced = replace(journal, phase=phase)
        self._write_journal(advanced)
        self._notify(phase)
        return advanced

    def _notify(self, phase: str) -> None:
        if self.phase_callback is not None:
            self.phase_callback(phase)

    def _prepare(self, journal: Journal) -> tuple[PreparedReference, ...]:
        references: list[PreparedReference] = []
        artifacts = {artifact.file_id: artifact for artifact in journal.artifacts.artifacts}
        for remote in journal.plan.downloads:
            artifact = artifacts[remote.file_id]
            payload = require_bounded_file(Path(artifact.payload_path), "artifact payload")
            payload_hash = _sha256_file(payload)
            if payload_hash != artifact.payload_sha256:
                raise DriveRagError(
                    f"artifact hash differs for file {remote.file_id}",
                    code="INVALID_ARTIFACT",
                )
            if remote.size is not None and remote.native_kind is None:
                if payload.stat().st_size != remote.size:
                    raise DriveRagError(
                        f"artifact size differs for file {remote.file_id}",
                        code="INVALID_ARTIFACT",
                    )
            if remote.checksum is not None and remote.native_kind is None:
                if _md5_file(payload) != remote.checksum:
                    raise DriveRagError(
                        f"artifact checksum differs for file {remote.file_id}",
                        code="INVALID_ARTIFACT",
                    )
            index_status = INDEXED
            index_reason = None
            if remote.native_kind is not None:
                self._validate_pdf(payload, remote.file_id)
                structured_path = Path(artifact.structured_path or "")
                require_bounded_file(structured_path, "structured artifact")
                structured = read_json(structured_path)
                if structured.get("kind") != remote.native_kind:
                    raise DriveRagError(
                        f"structured artifact kind differs for file {remote.file_id}",
                        code="INVALID_ARTIFACT",
                    )
                document = extract_native_structured(
                    remote.file_id, remote.revision, structured
                )
            else:
                try:
                    document = extract_file(
                        remote.file_id,
                        remote.revision,
                        payload,
                        remote.mime_type,
                    )
                except DriveRagError as exc:
                    if exc.code != "UNSUPPORTED_FORMAT":
                        raise
                    document = None
                    index_status = UNINDEXED
                    index_reason = UNSUPPORTED_FORMAT
            chunks = (
                chunk_document(document, self.embedder) if document is not None else ()
            )
            embeddings = self.embedder.embed_passages([chunk.text for chunk in chunks])
            prepared = _PreparedFile(
                remote.file_id,
                remote.revision,
                remote.mime_type,
                payload_hash,
                chunks,
                tuple(tuple(float(value) for value in vector) for vector in embeddings),
                index_status,
                index_reason,
            )
            prepared_path = self._prepared_path(journal.run_id, remote.file_id)
            atomic_write_json(
                prepared_path,
                {"schema_version": SCHEMA_VERSION, **prepared.to_dict()},
            )
            references.append(
                PreparedReference(
                    remote.file_id,
                    str(prepared_path),
                    _sha256_file(prepared_path),
                )
            )
        return tuple(sorted(references, key=lambda item: item.file_id))

    def _index_files(self, journal: Journal) -> None:
        prepared = self._load_prepared(journal)
        remotes = self._remote_files(journal.inventory)
        target_aliases = self._folder_map(journal.target_folders)
        for remote in journal.plan.downloads:
            item = prepared[remote.file_id]
            roots = self._root_metadata(
                remote,
                journal.plan.target_paths[remote.file_id],
                target_aliases,
            )
            self.index.stage_upsert(
                remote.file_id,
                remote.revision,
                item.chunks,
                item.embeddings,
                roots,
                mime_type=remote.mime_type,
            )

    def _promote(self, journal: Journal) -> None:
        prepared = self._load_prepared(journal)
        artifacts = {artifact.file_id: artifact for artifact in journal.artifacts.artifacts}
        aliases = self._folder_map(journal.target_folders)
        remotes = self._remote_files(journal.inventory)
        for file_id in sorted(journal.plan.target_paths):
            item = prepared.get(file_id)
            if item is not None:
                object_hash = item.object_sha256
                source = self._staging_path(
                    self.state_root / "staging" / journal.run_id,
                    artifacts[file_id].payload_path,
                    "artifact payload",
                )
            else:
                committed = journal.base_manifest.files.get(file_id)
                if committed is None or committed.object_sha256 is None:
                    raise DriveRagError(
                        f"committed object identity is missing for file {file_id}",
                        code="INVALID_STATE",
                    )
                object_hash = committed.object_sha256
                source = None
            object_path = self.object_path(file_id, object_hash)
            self._promote_object(source, object_path, object_hash, file_id)
            remote = remotes[file_id]
            for path in journal.plan.target_paths[file_id]:
                mirror = self._mirror_path(remote, path, aliases)
                self._replace_from_object(object_path, mirror)

    def _delete_stale(self, journal: Journal) -> None:
        base_aliases = self._folder_map(journal.base_folders)
        target_aliases = self._folder_map(journal.target_folders)
        remotes = self._remote_files(journal.inventory)
        desired: set[Path] = set()
        for file_id, paths in journal.plan.target_paths.items():
            remote = remotes[file_id]
            desired.update(self._mirror_path(remote, path, target_aliases) for path in paths)

        for file_id, committed in journal.base_manifest.files.items():
            for path in committed.paths:
                old_path = self._mirror_path_from_kind(
                    path, committed.native_kind, base_aliases
                )
                if old_path not in desired:
                    self._unlink_mirror(old_path)

        prepared = self._load_prepared(journal)
        for file_id, committed in journal.base_manifest.files.items():
            old_hash = committed.object_sha256
            if old_hash is None:
                continue
            replacement = prepared.get(file_id)
            if file_id in journal.plan.deleted_file_ids or (
                replacement is not None and replacement.object_sha256 != old_hash
            ):
                object_path = self.object_path(file_id, old_hash)
                self._unlink_object(object_path)

    def _commit(self, journal: Journal) -> None:
        committed = self._build_target_manifest(journal)
        if journal.target_manifest != committed:
            raise DriveRagError(
                "journal target manifest differs from prepared state",
                code="INVALID_JOURNAL",
            )
        candidates, roots, mime_types = self._index_expectations(journal)
        snapshot = self._load_activation_snapshot(journal)
        affected = self._activation_file_ids(journal)
        self.index.assert_snapshot_embeddings_match_current(
            snapshot,
            self._activation_unchanged_file_ids(journal),
            self.embedder.embed_passages,
        )
        if self.manifest() == committed:
            try:
                self.index.assert_target_records(
                    committed, candidates, roots, mime_types
                )
                return
            except DriveRagError:
                pass
        try:
            self.index.restore_file_records(snapshot, affected)
            self.index.assert_transition_records(
                journal.base_manifest,
                committed,
                candidates,
                self._base_index_roots(journal),
            )
            self._activate_index(journal, committed)
            self.index.assert_target_records(
                committed, candidates, roots, mime_types
            )
            atomic_write_json(
                self._folders_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "folders": [
                        folder.to_dict() for folder in journal.target_folders
                    ],
                },
            )
            atomic_write_json(
                self._manifest_path,
                {"schema_version": SCHEMA_VERSION, **committed.to_dict()},
            )
        except Exception:
            self.index.restore_file_records(snapshot, affected)
            raise

    def _prepare_activation(self, journal: Journal) -> Journal:
        committed = self._build_target_manifest(journal)
        if journal.target_manifest != committed:
            raise DriveRagError(
                "journal target manifest differs from prepared state",
                code="INVALID_JOURNAL",
            )
        candidates, _, _ = self._index_expectations(journal)
        base_roots = self._base_index_roots(journal)
        self.index.assert_transition_records(
            journal.base_manifest,
            committed,
            candidates,
            base_roots,
        )
        affected = self._activation_file_ids(journal)
        batch_size = self.index.client.get_max_batch_size()
        fields = ("ids", "documents", "metadatas", "embeddings")
        empty_shard: dict[str, list[object]] = {key: [] for key in fields}
        base_size = len(
            (
                json.dumps(
                    {"schema_version": SCHEMA_VERSION, "activation": empty_shard},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        )
        current: dict[str, list[object]] = {key: [] for key in fields}
        current_size = base_size
        references: list[ActivationReference] = []

        def flush() -> None:
            nonlocal current, current_size
            if not current["ids"]:
                return
            path = self._activation_path(journal.run_id, len(references))
            atomic_write_json(
                path,
                {"schema_version": SCHEMA_VERSION, "activation": current},
            )
            require_bounded_file(path, "activation snapshot shard")
            if path.stat().st_size > _ACTIVATION_SHARD_BYTES:
                raise DriveRagError(
                    "activation snapshot shard exceeds its byte limit",
                    code="EXTRACTION_LIMIT_EXCEEDED",
                )
            references.append(ActivationReference(str(path), _sha256_file(path)))
            current = {key: [] for key in fields}
            current_size = base_size

        for page in self.index.iter_file_record_pages(affected):
            for index in range(len(page["ids"])):
                contribution = sum(
                    len(
                        json.dumps(
                            page[key][index],
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode("utf-8")
                    )
                    for key in fields
                )
                if current["ids"]:
                    contribution += 2 * len(fields)
                if base_size + contribution > _ACTIVATION_SHARD_BYTES:
                    raise DriveRagError(
                        "one activation record exceeds the shard byte limit",
                        code="EXTRACTION_LIMIT_EXCEEDED",
                    )
                if current["ids"] and (
                    len(current["ids"]) >= batch_size
                    or current_size + contribution > _ACTIVATION_SHARD_BYTES
                ):
                    flush()
                    contribution -= 2 * len(fields)
                for key in fields:
                    current[key].append(page[key][index])
                current_size += contribution
        flush()
        activating = replace(
            journal,
            phase="activating",
            activation=tuple(references),
        )
        self._write_journal(activating)
        self._notify("activating")
        return activating

    def _load_activation_snapshot(
        self, journal: Journal, *, allow_missing: bool = False
    ) -> dict[str, list[object]]:
        references = journal.activation
        if references is None:
            raise DriveRagError(
                "activating journal is missing its snapshot",
                code="INVALID_JOURNAL",
            )
        snapshot: dict[str, list[object]] = {
            "ids": [],
            "documents": [],
            "metadatas": [],
            "embeddings": [],
        }
        for ordinal, reference in enumerate(references):
            path = self._validate_activation_reference(
                journal, reference, ordinal, allow_missing=allow_missing
            )
            if path is None:
                return {}
            payload = read_json(path)
            if (
                set(payload) != {"schema_version", "activation"}
                or payload.get("schema_version") != SCHEMA_VERSION
                or not isinstance(payload.get("activation"), dict)
            ):
                raise DriveRagError(
                    "activation snapshot schema is invalid", code="INVALID_JOURNAL"
                )
            shard = payload["activation"]
            assert isinstance(shard, dict)
            if set(shard) != set(snapshot) or any(
                not isinstance(shard[key], list) for key in snapshot
            ):
                raise DriveRagError(
                    "activation snapshot shard is invalid", code="INVALID_JOURNAL"
                )
            for key in snapshot:
                snapshot[key].extend(shard[key])
        committed = self._build_target_manifest(journal)
        candidates, _, _ = self._index_expectations(journal)
        self.index.assert_transition_snapshot(
            snapshot,
            journal.base_manifest,
            committed,
            candidates,
            self._base_index_roots(journal),
            file_ids=self._activation_file_ids(journal),
        )
        return snapshot

    def _activation_file_ids(self, journal: Journal) -> set[str]:
        affected = {remote.file_id for remote in journal.plan.downloads}
        affected.update(journal.plan.deleted_file_ids)
        affected.update(self._activation_unchanged_file_ids(journal))
        return affected

    def _activation_unchanged_file_ids(self, journal: Journal) -> set[str]:
        base_aliases = self._folder_map(journal.base_folders)
        target_aliases = self._folder_map(journal.target_folders)
        result: set[str] = set()
        for file_id in journal.plan.unchanged_file_ids:
            committed = journal.base_manifest.files[file_id]
            if self._paths_or_aliases_changed(
                committed.paths,
                journal.plan.target_paths[file_id],
                base_aliases,
                target_aliases,
            ):
                result.add(file_id)
        return result

    def _base_index_roots(
        self, journal: Journal
    ) -> dict[str, dict[str, dict[str, str]]]:
        aliases = self._folder_map(journal.base_folders)
        remotes = self._remote_files(journal.inventory)
        downloads = {remote.file_id for remote in journal.plan.downloads}
        roots: dict[str, dict[str, dict[str, str]]] = {}
        for file_id, committed in journal.base_manifest.files.items():
            selected: dict[str, RemotePath] = {}
            for path in sorted(
                committed.paths, key=lambda item: (item.root_id, item.parts)
            ):
                selected.setdefault(path.root_id, path)
            file_roots: dict[str, dict[str, str]] = {}
            for root_id, path in selected.items():
                metadata = {
                    "folder_alias": aliases[root_id].alias,
                    "local_path": str(
                        self._mirror_path_from_kind(
                            path, committed.native_kind, aliases
                        )
                    ),
                }
                remote = remotes.get(file_id)
                if remote is not None and file_id not in downloads:
                    metadata["drive_url"] = remote.drive_url
                file_roots[root_id] = metadata
            roots[file_id] = file_roots
        return roots

    def _index_expectations(
        self, journal: Journal
    ) -> tuple[
        dict[str, tuple[str, dict[str, str], list[float]]],
        dict[str, dict[str, dict[str, str]]],
        dict[str, str],
    ]:
        prepared = self._load_prepared(journal)
        remotes = self._remote_files(journal.inventory)
        aliases = self._folder_map(journal.target_folders)
        candidates: dict[str, tuple[str, dict[str, str], list[float]]] = {}
        roots: dict[str, dict[str, dict[str, str]]] = {}
        mime_types: dict[str, str] = {}
        for file_id, paths in journal.plan.target_paths.items():
            remote = remotes[file_id]
            file_roots = self._root_metadata(remote, paths, aliases)
            roots[file_id] = file_roots
            mime_types[file_id] = remote.mime_type
            item = prepared.get(file_id)
            if item is not None:
                candidates.update(
                    self.index.expected_records(
                        file_id,
                        remote.revision,
                        item.chunks,
                        item.embeddings,
                        file_roots,
                        mime_type=remote.mime_type,
                    )
                )
        return candidates, roots, mime_types

    def _activate_index(self, journal: Journal, target: Manifest) -> None:
        base_aliases = self._folder_map(journal.base_folders)
        target_aliases = self._folder_map(journal.target_folders)
        remotes = self._remote_files(journal.inventory)
        for remote in journal.plan.downloads:
            committed = target.files[remote.file_id]
            self.index.retire_file_except(
                remote.file_id,
                committed.active_chunk_ids,
                tuple(sorted({path.root_id for path in committed.paths})),
            )
        for file_id in journal.plan.unchanged_file_ids:
            committed = journal.base_manifest.files[file_id]
            remote = remotes[file_id]
            targets = journal.plan.target_paths[file_id]
            if self._paths_or_aliases_changed(
                committed.paths,
                targets,
                base_aliases,
                target_aliases,
            ):
                self.index.repath_file(
                    file_id,
                    remote.revision,
                    committed.active_chunk_ids,
                    self._root_metadata(remote, targets, target_aliases),
                    mime_type=remote.mime_type,
                )
        for file_id in journal.plan.deleted_file_ids:
            self.index.delete_file(file_id)

    def _build_target_manifest(self, journal: Journal) -> Manifest:
        prepared = self._load_prepared(journal)
        remotes = self._remote_files(journal.inventory)
        files: dict[str, ManifestFile] = {}
        for file_id in sorted(journal.plan.target_paths):
            remote = remotes[file_id]
            item = prepared.get(file_id)
            if item is not None:
                object_sha256 = item.object_sha256
                chunk_ids = tuple(chunk.chunk_id for chunk in item.chunks)
                index_status = item.index_status
                index_reason = item.index_reason
            else:
                committed = journal.base_manifest.files.get(file_id)
                if committed is None:
                    raise DriveRagError(
                        f"committed identity is missing for file {file_id}",
                        code="INVALID_STATE",
                    )
                object_sha256 = committed.object_sha256
                chunk_ids = committed.active_chunk_ids
                index_status = committed.index_status
                index_reason = committed.index_reason
            files[file_id] = ManifestFile(
                file_id,
                remote.revision,
                remote.checksum,
                object_sha256,
                journal.plan.target_paths[file_id],
                chunk_ids,
                remote.native_kind,
                index_status,
                index_reason,
            )
        return Manifest(
            files,
            self._model_identity(),
            journal.inventory.generated_at,
            None,
            journal.inventory.root_ids,
            journal.inventory.generated_at,
        )

    def _validate_artifact_metadata(
        self,
        inventory: RemoteInventory,
        artifacts: ArtifactSet,
        downloads: Sequence[RemoteFile],
        *,
        allow_promoted_payload: bool = False,
        allow_consumed_structured: bool = False,
        expected_object_hashes: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(artifacts, ArtifactSet) or not isinstance(
            artifacts.artifacts, tuple
        ):
            raise DriveRagError("artifact set is invalid", code="INVALID_ARTIFACT")
        if artifacts.run_id != inventory.run_id:
            raise DriveRagError(
                "artifact set run ID does not match inventory",
                code="INVALID_ARTIFACT",
            )
        if _SAFE_RUN_ID.fullmatch(inventory.run_id) is None:
            raise DriveRagError(
                "inventory run ID is unsafe for staging", code="INVALID_ARTIFACT"
            )
        expected = {remote.file_id: remote for remote in downloads}
        actual: dict[str, Artifact] = {}
        artifact_paths: set[Path] = set()
        run_root = resolve_below(
            self.state_root,
            self.state_root / "staging" / inventory.run_id,
        )
        for artifact in artifacts.artifacts:
            if not isinstance(artifact, Artifact):
                raise DriveRagError("artifact entry is invalid", code="INVALID_ARTIFACT")
            if artifact.file_id in actual:
                raise DriveRagError(
                    f"duplicate artifact for file {artifact.file_id}",
                    code="INVALID_ARTIFACT",
                )
            remote = expected.get(artifact.file_id)
            if remote is None:
                raise DriveRagError(
                    f"unexpected artifact for file {artifact.file_id}",
                    code="INVALID_ARTIFACT",
                )
            if artifact.revision != remote.revision:
                raise DriveRagError(
                    f"artifact revision differs for file {artifact.file_id}",
                    code="INVALID_ARTIFACT",
                )
            if (
                not isinstance(artifact.payload_sha256, str)
                or _SHA256.fullmatch(artifact.payload_sha256) is None
            ):
                raise DriveRagError(
                    f"artifact hash is invalid for file {artifact.file_id}",
                    code="INVALID_ARTIFACT",
                )
            payload = self._staging_path(
                run_root, artifact.payload_path, "artifact payload"
            )
            self._reject_reserved_artifact_path(run_root, payload)
            if payload in artifact_paths:
                raise DriveRagError(
                    "artifact paths must be unique",
                    code="INVALID_ARTIFACT",
                )
            artifact_paths.add(payload)
            if payload.exists():
                require_bounded_file(payload, "artifact payload")
            elif allow_promoted_payload:
                object_hash = (expected_object_hashes or {}).get(artifact.file_id)
                if object_hash is None or object_hash != artifact.payload_sha256:
                    raise DriveRagError(
                        f"promoted object identity differs for file {artifact.file_id}",
                        code="INVALID_JOURNAL",
                    )
                canonical = self.object_path(artifact.file_id, object_hash)
                require_bounded_file(canonical, "canonical object")
                if _sha256_file(canonical) != object_hash:
                    raise DriveRagError(
                        f"canonical object hash differs for file {artifact.file_id}",
                        code="INVALID_STATE",
                    )
            else:
                require_bounded_file(payload, "artifact payload")
            structured = None
            if artifact.structured_path is not None:
                structured = self._staging_path(
                    run_root,
                    artifact.structured_path,
                    "structured artifact",
                )
                self._reject_reserved_artifact_path(run_root, structured)
                if structured in artifact_paths:
                    raise DriveRagError(
                        "artifact paths must be unique",
                        code="INVALID_ARTIFACT",
                    )
                artifact_paths.add(structured)
                if structured.exists():
                    require_bounded_file(structured, "structured artifact")
                elif not allow_consumed_structured:
                    require_bounded_file(structured, "structured artifact")
            if remote.native_kind is not None and structured is None:
                raise DriveRagError(
                    f"native file {artifact.file_id} requires structured artifact",
                    code="INVALID_ARTIFACT",
                )
            if remote.native_kind is None and structured is not None:
                raise DriveRagError(
                    f"non-native file {artifact.file_id} has an extra structured artifact",
                    code="INVALID_ARTIFACT",
                )
            if payload == structured:
                raise DriveRagError(
                    "payload and structured artifact must differ",
                    code="INVALID_ARTIFACT",
                )
            actual[artifact.file_id] = artifact
        if set(actual) != set(expected):
            missing = sorted(set(expected) - set(actual))
            raise DriveRagError(
                f"artifact set is missing downloads: {', '.join(missing)}",
                code="INVALID_ARTIFACT",
            )

    def _staging_file(self, run_root: Path, value: object, label: str) -> Path:
        path = self._staging_path(run_root, value, label)
        require_bounded_file(path, label)
        return path

    @staticmethod
    def _reject_reserved_artifact_path(run_root: Path, path: Path) -> None:
        prepared_root = run_root / "prepared"
        try:
            path.relative_to(prepared_root)
        except ValueError:
            return
        raise DriveRagError(
            "connector artifacts must not use the reserved prepared namespace",
            code="INVALID_ARTIFACT",
        )

    def _staging_path(self, run_root: Path, value: object, label: str) -> Path:
        if not isinstance(value, str) or not value:
            raise DriveRagError(f"{label} path is invalid", code="INVALID_ARTIFACT")
        raw = Path(value)
        if not raw.is_absolute():
            raise DriveRagError(
                f"{label} path must be absolute", code="INVALID_ARTIFACT"
            )
        if raw.is_symlink():
            raise DriveRagError(f"{label} must not be a symlink", code="UNSAFE_PATH")
        try:
            path = resolve_below(run_root, raw)
        except DriveRagError as exc:
            raise DriveRagError(
                f"{label} must be below the run staging directory",
                code="INVALID_ARTIFACT",
            ) from exc
        if path == run_root:
            raise DriveRagError(
                f"{label} must name a file below the run staging directory",
                code="INVALID_ARTIFACT",
            )
        return path

    def _validate_journal(self, journal: Journal) -> None:
        if _SAFE_RUN_ID.fullmatch(journal.run_id) is None:
            raise DriveRagError(
                "journal run ID is unsafe", code="INVALID_JOURNAL"
            )
        if journal.phase not in JOURNAL_PHASES:
            raise DriveRagError("journal phase is invalid", code="INVALID_JOURNAL")
        if any(
            run_id != journal.run_id
            for run_id in (
                journal.inventory.run_id,
                journal.artifacts.run_id,
                journal.plan.run_id,
            )
        ):
            raise DriveRagError("journal run identities differ", code="INVALID_JOURNAL")
        target_roots = {folder.folder_id for folder in journal.target_folders}
        self._validate_folder_snapshot(journal.target_folders, target_roots)
        self._validate_folder_snapshot(
            journal.base_folders, set(journal.base_manifest.root_ids)
        )
        _validate_manifest(journal.base_manifest)
        if journal.phase == "planned":
            if journal.target_manifest is not None:
                raise DriveRagError(
                    "planned journal must not have a target manifest",
                    code="INVALID_JOURNAL",
                )
        else:
            if journal.target_manifest is None:
                raise DriveRagError(
                    "verified journal is missing its target manifest",
                    code="INVALID_JOURNAL",
                )
            self._validate_target_manifest_identity(journal)
        expected_plan = self._resolve_rendered_collisions(
            plan_sync(journal.inventory, journal.base_manifest, target_roots),
            self._remote_files(journal.inventory),
        )
        if journal.plan != expected_plan:
            raise DriveRagError(
                "journal plan differs from validated inventory",
                code="INVALID_JOURNAL",
            )
        target_hashes = (
            {
                file_id: committed.object_sha256
                for file_id, committed in journal.target_manifest.files.items()
                if committed.object_sha256 is not None
            }
            if journal.target_manifest is not None
            else None
        )
        self._validate_artifact_metadata(
            journal.inventory,
            journal.artifacts,
            journal.plan.downloads,
            allow_promoted_payload=(
                _PHASE_INDEX[journal.phase] >= _PHASE_INDEX["indexed"]
            ),
            allow_consumed_structured=(
                _PHASE_INDEX[journal.phase] >= _PHASE_INDEX["verified"]
            ),
            expected_object_hashes=target_hashes,
        )
        prepared_ids = [item.file_id for item in journal.prepared]
        if len(set(prepared_ids)) != len(prepared_ids):
            raise DriveRagError(
                "journal prepared references are duplicated", code="INVALID_JOURNAL"
            )
        if journal.phase == "planned" and journal.prepared:
            raise DriveRagError(
                "planned journal must not have prepared references",
                code="INVALID_JOURNAL",
            )
        if _PHASE_INDEX[journal.phase] >= _PHASE_INDEX["verified"] and set(
            prepared_ids
        ) != {remote.file_id for remote in journal.plan.downloads}:
            raise DriveRagError(
                "journal prepared references do not match downloads",
                code="INVALID_JOURNAL",
            )
        if _PHASE_INDEX[journal.phase] >= _PHASE_INDEX["verified"]:
            self._load_prepared(
                journal, allow_missing=(journal.phase == "committed")
            )
        if journal.phase == "activating":
            if journal.activation is None:
                raise DriveRagError(
                    "activating journal is missing its snapshot",
                    code="INVALID_JOURNAL",
                )
            self._load_activation_snapshot(journal)
        elif journal.phase == "committed":
            if journal.activation is not None:
                for ordinal, reference in enumerate(journal.activation):
                    self._validate_activation_reference(
                        journal,
                        reference,
                        ordinal,
                        allow_missing=True,
                    )
        elif journal.activation is not None:
            raise DriveRagError(
                "preactivation journal has an activation snapshot",
                code="INVALID_JOURNAL",
            )
        calculated_changed = self._plan_changed(
            journal.plan,
            journal.base_manifest,
            journal.base_folders,
            journal.target_folders,
        )
        if calculated_changed != journal.changed:
            raise DriveRagError(
                "journal changed flag is inconsistent", code="INVALID_JOURNAL"
            )

    def _validate_target_manifest_identity(self, journal: Journal) -> None:
        target = journal.target_manifest
        assert target is not None
        _validate_manifest(target)
        if (
            target.model_identity != self._model_identity()
            or target.last_success != journal.inventory.generated_at
            or target.last_failure is not None
            or target.root_ids != journal.inventory.root_ids
            or target.last_inventory_generated_at != journal.inventory.generated_at
            or set(target.files) != set(journal.plan.target_paths)
        ):
            raise DriveRagError(
                "journal target manifest identity is invalid",
                code="INVALID_JOURNAL",
            )
        remotes = self._remote_files(journal.inventory)
        downloads = {remote.file_id for remote in journal.plan.downloads}
        for file_id, committed in target.files.items():
            remote = remotes[file_id]
            if (
                committed.revision != remote.revision
                or committed.checksum != remote.checksum
                or committed.paths != journal.plan.target_paths[file_id]
                or committed.native_kind != remote.native_kind
            ):
                raise DriveRagError(
                    "journal target file identity is invalid",
                    code="INVALID_JOURNAL",
                )
            if file_id not in downloads:
                base = journal.base_manifest.files.get(file_id)
                if (
                    base is None
                    or committed.object_sha256 != base.object_sha256
                    or committed.active_chunk_ids != base.active_chunk_ids
                    or committed.index_status != base.index_status
                    or committed.index_reason != base.index_reason
                ):
                    raise DriveRagError(
                        "journal unchanged target differs from committed state",
                        code="INVALID_JOURNAL",
                    )

    def _assert_observed_manifest(self, journal: Journal) -> None:
        observed = self.manifest()
        failed_base = Manifest(
            journal.base_manifest.files,
            journal.base_manifest.model_identity,
            journal.base_manifest.last_success,
            SYNC_FAILED_PREVIOUS_VERSION_ACTIVE,
            journal.base_manifest.root_ids,
            journal.base_manifest.last_inventory_generated_at,
        )
        allowed = [journal.base_manifest, failed_base]
        if journal.phase in {"deleted", "activating"} and journal.target_manifest is not None:
            allowed.append(journal.target_manifest)
        if journal.phase == "committed":
            allowed = [journal.target_manifest]
        if not any(observed == candidate for candidate in allowed):
            raise DriveRagError(
                "current manifest is not bound to the pending journal",
                code="INVALID_JOURNAL",
            )

    def _validate_folder_snapshot(
        self, folders: Sequence[FolderConfig], expected_roots: set[str]
    ) -> None:
        if not isinstance(folders, tuple):
            raise DriveRagError("folder snapshot is invalid", code="INVALID_JOURNAL")
        ids: set[str] = set()
        aliases: set[str] = set()
        for folder in folders:
            if not isinstance(folder, FolderConfig) or not folder.enabled:
                raise DriveRagError(
                    "folder snapshot must contain enabled folders",
                    code="INVALID_JOURNAL",
                )
            self._validate_alias(folder.alias)
            folder_alias_key = alias_key(
                folder.alias, code="INVALID_JOURNAL", require_canonical=True
            )
            if folder.folder_id in ids or folder_alias_key in aliases:
                raise DriveRagError(
                    "folder snapshot identities must be unique",
                    code="INVALID_JOURNAL",
                )
            ids.add(folder.folder_id)
            aliases.add(folder_alias_key)
        if ids != expected_roots:
            raise DriveRagError(
                "folder snapshot root scope differs", code="INVALID_JOURNAL"
            )

    def _load_journal(self, *, required: bool) -> Journal | None:
        if not self._journal_path.exists() and not self._journal_path.is_symlink():
            if required:
                raise DriveRagError("pending journal is missing", code="INVALID_JOURNAL")
            return None
        require_bounded_file(self._journal_path, "journal")
        payload = read_json(self._journal_path)
        if set(payload) != {"schema_version", "journal"} or payload.get(
            "schema_version"
        ) != SCHEMA_VERSION or not isinstance(payload.get("journal"), dict):
            raise DriveRagError("pending journal schema is invalid", code="INVALID_JOURNAL")
        journal = Journal.from_dict(payload["journal"])
        self._validate_journal(journal)
        return journal

    def _write_journal(self, journal: Journal) -> None:
        self._validate_journal(journal)
        atomic_write_json(
            self._journal_path,
            {"schema_version": SCHEMA_VERSION, "journal": journal.to_dict()},
        )

    def _load_prepared(
        self, journal: Journal, *, allow_missing: bool = False
    ) -> dict[str, _PreparedFile]:
        result: dict[str, _PreparedFile] = {}
        remotes = self._remote_files(journal.inventory)
        artifacts = {
            artifact.file_id: artifact for artifact in journal.artifacts.artifacts
        }
        for reference in journal.prepared:
            expected_path = self._prepared_path(journal.run_id, reference.file_id)
            if reference.path != str(expected_path):
                raise DriveRagError(
                    "prepared reference path is not canonical",
                    code="INVALID_JOURNAL",
                )
            path = self._staging_path(
                self.state_root / "staging" / journal.run_id,
                reference.path,
                "prepared artifact",
            )
            if path != expected_path:
                raise DriveRagError(
                    "prepared reference does not resolve to its canonical path",
                    code="INVALID_JOURNAL",
                )
            if not path.exists() and allow_missing:
                continue
            require_bounded_file(path, "prepared artifact")
            if (
                _SHA256.fullmatch(reference.sha256) is None
                or _sha256_file(path) != reference.sha256
            ):
                raise DriveRagError(
                    f"prepared artifact hash differs for file {reference.file_id}",
                    code="INVALID_JOURNAL",
                )
            payload = read_json(path)
            if payload.pop("schema_version", None) != SCHEMA_VERSION:
                raise DriveRagError(
                    "prepared artifact schema is invalid", code="INVALID_JOURNAL"
                )
            prepared = _PreparedFile.from_dict(payload)
            if prepared.file_id != reference.file_id:
                raise DriveRagError(
                    "prepared artifact identity differs", code="INVALID_JOURNAL"
                )
            remote = remotes.get(prepared.file_id)
            artifact = artifacts.get(prepared.file_id)
            chunk_ids = tuple(chunk.chunk_id for chunk in prepared.chunks)
            if (
                remote is None
                or artifact is None
                or prepared.revision != remote.revision
                or prepared.mime_type != remote.mime_type
                or prepared.object_sha256 != artifact.payload_sha256
                or any(len(vector) != self.embedder.dimension for vector in prepared.embeddings)
            ):
                raise DriveRagError(
                    "prepared artifact differs from journal identity",
                    code="INVALID_JOURNAL",
                )
            for ordinal, chunk in enumerate(prepared.chunks):
                digest = hashlib.sha256(
                    f"{prepared.file_id}\0{prepared.revision}\0{chunk.locator}\0{ordinal}\0{chunk.text}".encode()
                ).hexdigest()
                if chunk.chunk_id != f"{prepared.file_id}:{digest}":
                    raise DriveRagError(
                        "prepared chunk identity differs from its content",
                        code="INVALID_JOURNAL",
                    )
            if journal.target_manifest is not None:
                target = journal.target_manifest.files.get(prepared.file_id)
                if (
                    target is None
                    or target.object_sha256 != prepared.object_sha256
                    or target.active_chunk_ids != chunk_ids
                    or target.index_status != prepared.index_status
                    or target.index_reason != prepared.index_reason
                ):
                    raise DriveRagError(
                        "prepared artifact differs from target manifest",
                        code="INVALID_JOURNAL",
                    )
            result[prepared.file_id] = prepared
        return result

    def _prepared_path(self, run_id: str, file_id: str) -> Path:
        identity = hashlib.sha256(file_id.encode("utf-8")).hexdigest()
        return resolve_below(
            self.state_root,
            self.state_root / "staging" / run_id / "prepared" / f"{identity}.json",
        )

    def _activation_path(self, run_id: str, ordinal: int) -> Path:
        return resolve_below(
            self.state_root,
            self.state_root
            / "staging"
            / run_id
            / "prepared"
            / f"activation-{ordinal:06d}.json",
        )

    def _validate_activation_reference(
        self,
        journal: Journal,
        reference: ActivationReference,
        ordinal: int,
        *,
        allow_missing: bool,
    ) -> Path | None:
        expected = self._activation_path(journal.run_id, ordinal)
        if reference.path != str(expected) or _SHA256.fullmatch(reference.sha256) is None:
            raise DriveRagError(
                "activation reference identity is invalid", code="INVALID_JOURNAL"
            )
        path = self._staging_path(
            self.state_root / "staging" / journal.run_id,
            reference.path,
            "activation snapshot",
        )
        if path != expected:
            raise DriveRagError(
                "activation reference is not canonical", code="INVALID_JOURNAL"
            )
        if not path.exists() and allow_missing:
            return None
        require_bounded_file(path, "activation snapshot")
        if _sha256_file(path) != reference.sha256:
            raise DriveRagError(
                "activation snapshot hash differs", code="INVALID_JOURNAL"
            )
        return path

    def _load_committed_folders(
        self, manifest: Manifest
    ) -> tuple[FolderConfig, ...]:
        if not self._folders_path.exists():
            if manifest.root_ids:
                raise DriveRagError(
                    "committed folder aliases are missing", code="INVALID_STATE"
                )
            return ()
        payload = read_json(self._folders_path)
        if set(payload) != {"schema_version", "folders"} or payload.get(
            "schema_version"
        ) != SCHEMA_VERSION or not isinstance(payload.get("folders"), list):
            raise DriveRagError(
                "committed folder alias schema is invalid", code="INVALID_STATE"
            )
        folders = tuple(
            FolderConfig.from_dict(item)
            for item in payload["folders"]
            if isinstance(item, dict)
        )
        if len(folders) != len(payload["folders"]):
            raise DriveRagError(
                "committed folder aliases are invalid", code="INVALID_STATE"
            )
        self._validate_folder_snapshot(folders, set(manifest.root_ids))
        return folders

    def _mark_failure(self, base: Manifest) -> None:
        failed = Manifest(
            base.files,
            base.model_identity,
            base.last_success,
            SYNC_FAILED_PREVIOUS_VERSION_ACTIVE,
            base.root_ids,
            base.last_inventory_generated_at,
        )
        atomic_write_json(
            self._manifest_path,
            {"schema_version": SCHEMA_VERSION, **failed.to_dict()},
        )

    def _discard_pending(self, journal: Journal) -> None:
        try:
            run_root = resolve_below(
                self.state_root,
                self.state_root / "staging" / journal.run_id,
            )
            if run_root.exists():
                if run_root.is_symlink():
                    raise DriveRagError(
                        "staging run directory must not be a symlink",
                        code="UNSAFE_PATH",
                    )
                shutil.rmtree(run_root)
            self._journal_path.unlink(missing_ok=True)
        except OSError as exc:
            raise DriveRagError(
                "could not remove committed staging state",
                code="STATE_WRITE_FAILED",
            ) from exc

    def _validate_pdf(self, path: Path, file_id: str) -> None:
        try:
            with path.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise ValueError("missing PDF signature")
            document = fitz.open(path)
            document.page_count
            document.close()
        except (OSError, ValueError, fitz.FileDataError) as exc:
            raise DriveRagError(
                f"artifact PDF is unreadable for file {file_id}",
                code="INVALID_ARTIFACT",
            ) from exc

    def _promote_object(
        self,
        source: Path | None,
        destination: Path,
        expected_hash: str,
        file_id: str,
    ) -> None:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            require_bounded_file(destination, "canonical object")
            if _sha256_file(destination) != expected_hash:
                raise DriveRagError(
                    f"canonical object hash differs for file {file_id}",
                    code="INVALID_STATE",
                )
            return
        if source is None or not source.exists():
            raise DriveRagError(
                f"artifact payload is missing for file {file_id}",
                code="INVALID_ARTIFACT",
            )
        require_bounded_file(source, "artifact payload")
        if _sha256_file(source) != expected_hash:
            raise DriveRagError(
                f"artifact hash differs for file {file_id}", code="INVALID_ARTIFACT"
            )
        os.replace(source, destination)
        os.chmod(destination, 0o600)

    def _replace_from_object(self, source: Path, destination: Path) -> None:
        require_bounded_file(source, "canonical object")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with source.open("rb") as source_stream, os.fdopen(
                descriptor, "wb"
            ) as target_stream:
                shutil.copyfileobj(source_stream, target_stream)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _unlink_mirror(self, path: Path) -> None:
        if path.is_symlink():
            raise DriveRagError("mirror path must not be a symlink", code="UNSAFE_PATH")
        if path.exists():
            if not path.is_file():
                raise DriveRagError("mirror path is not a file", code="INVALID_STATE")
            path.unlink()
        current = path.parent
        mirrors_root = self.state_root / "mirrors"
        while current != mirrors_root:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _unlink_object(self, path: Path) -> None:
        if path.is_symlink():
            raise DriveRagError("object path must not be a symlink", code="UNSAFE_PATH")
        if path.exists():
            if not path.is_file():
                raise DriveRagError("object path is not a file", code="INVALID_STATE")
            path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass

    def _mirror_path(
        self,
        remote: RemoteFile,
        path: RemotePath,
        aliases: Mapping[str, FolderConfig],
    ) -> Path:
        return self._mirror_path_from_kind(path, remote.native_kind, aliases)

    def _mirror_path_from_kind(
        self,
        path: RemotePath,
        native_kind: str | None,
        aliases: Mapping[str, FolderConfig],
    ) -> Path:
        folder = aliases.get(path.root_id)
        if folder is None:
            raise DriveRagError(
                f"folder alias is missing for root {path.root_id}",
                code="INVALID_STATE",
            )
        parts = self._render_parts(path.parts, native_kind)
        return resolve_below(
            self.state_root / "mirrors",
            self.state_root / "mirrors" / folder.alias / Path(*parts),
        )

    @staticmethod
    def _render_parts(parts: tuple[str, ...], native_kind: str | None) -> tuple[str, ...]:
        if native_kind is None or parts[-1].casefold().endswith(".pdf"):
            return parts
        return (*parts[:-1], f"{parts[-1]}.pdf")

    def _root_metadata(
        self,
        remote: RemoteFile,
        paths: Sequence[RemotePath],
        aliases: Mapping[str, FolderConfig],
    ) -> dict[str, dict[str, str]]:
        selected: dict[str, RemotePath] = {}
        for path in sorted(paths, key=lambda item: (item.root_id, item.parts)):
            selected.setdefault(path.root_id, path)
        original: dict[str, RemotePath] = {}
        for path in sorted(
            remote.paths, key=lambda item: (item.root_id, item.parts)
        ):
            original.setdefault(path.root_id, path)
        roots: dict[str, dict[str, str]] = {}
        for root_id, path in selected.items():
            drive_path = original.get(root_id)
            if drive_path is None:
                raise DriveRagError(
                    f"original Drive path is missing for root {root_id}",
                    code="INVALID_INVENTORY",
                )
            roots[root_id] = {
                "alias": aliases[root_id].alias,
                "drive_path": "/".join(drive_path.parts),
                "drive_url": remote.drive_url,
                "local_path": str(self._mirror_path(remote, path, aliases)),
            }
        return roots

    @staticmethod
    def _remote_files(inventory: RemoteInventory) -> dict[str, RemoteFile]:
        result: dict[str, RemoteFile] = {}
        for remote in inventory.files:
            remote = replace(
                remote,
                paths=tuple(
                    sorted(
                        set(remote.paths),
                        key=lambda path: (path.root_id, path.parent_ids, path.parts),
                    )
                ),
            )
            existing = result.get(remote.file_id)
            if existing is None:
                result[remote.file_id] = remote
                continue
            if replace(existing, paths=()) != replace(remote, paths=()):
                raise DriveRagError(
                    f"conflicting duplicate file ID: {remote.file_id}",
                    code="INVALID_INVENTORY",
                )
            result[remote.file_id] = replace(
                existing,
                paths=tuple(
                    sorted(
                        set(existing.paths) | set(remote.paths),
                        key=lambda path: (path.root_id, path.parent_ids, path.parts),
                    )
                ),
            )
        return result

    @staticmethod
    def _folder_map(folders: Sequence[FolderConfig]) -> dict[str, FolderConfig]:
        return {folder.folder_id: folder for folder in folders}

    @staticmethod
    def _validate_alias(alias: str) -> None:
        canonical_alias(alias, code="INVALID_JOURNAL", require_canonical=True)

    @staticmethod
    def _paths_or_aliases_changed(
        old_paths: Sequence[RemotePath],
        new_paths: Sequence[RemotePath],
        old_aliases: Mapping[str, FolderConfig],
        new_aliases: Mapping[str, FolderConfig],
    ) -> bool:
        if tuple(old_paths) != tuple(new_paths):
            return True
        roots = {path.root_id for path in old_paths} | {path.root_id for path in new_paths}
        return any(
            old_aliases.get(root_id) is None
            or new_aliases.get(root_id) is None
            or old_aliases[root_id].alias != new_aliases[root_id].alias
            for root_id in roots
        )

    def _plan_changed(
        self,
        plan,
        base_manifest: Manifest,
        base_folders: Sequence[FolderConfig],
        target_folders: Sequence[FolderConfig],
    ) -> bool:
        if plan.downloads or plan.deleted_file_ids:
            return True
        old_aliases = self._folder_map(base_folders)
        new_aliases = self._folder_map(target_folders)
        for file_id in plan.unchanged_file_ids:
            if self._paths_or_aliases_changed(
                base_manifest.files[file_id].paths,
                plan.target_paths[file_id],
                old_aliases,
                new_aliases,
            ):
                return True
        return set(base_manifest.root_ids) != {
            folder.folder_id for folder in target_folders
        }

    def _is_committed_retry(
        self,
        inventory: RemoteInventory,
        artifacts: ArtifactSet,
        manifest: Manifest,
        target_folders: Sequence[FolderConfig],
    ) -> bool:
        if manifest.last_inventory_generated_at != inventory.generated_at:
            return False
        if manifest.last_success != inventory.generated_at or manifest.last_failure is not None:
            return False
        if (
            not isinstance(artifacts, ArtifactSet)
            or artifacts.run_id != inventory.run_id
            or artifacts.artifacts
        ):
            raise DriveRagError(
                "an idempotent committed retry must not supply artifacts",
                code="INVALID_ARTIFACT",
            )
        target_roots = {folder.folder_id for folder in target_folders}
        prove_complete(inventory, target_roots)
        retry_base = replace(
            manifest, last_inventory_generated_at="0001-01-01T00:00:00Z"
        )
        retry_plan = self._resolve_rendered_collisions(
            plan_sync(inventory, retry_base, target_roots),
            self._remote_files(inventory),
        )
        if retry_plan.downloads or retry_plan.deleted_file_ids:
            return False
        if set(retry_plan.unchanged_file_ids) != set(manifest.files):
            return False
        if any(
            retry_plan.target_paths[file_id] != manifest.files[file_id].paths
            for file_id in manifest.files
        ):
            return False
        committed_folders = self._load_committed_folders(manifest)
        return self._folder_map(committed_folders) == self._folder_map(target_folders)

    def _resolve_rendered_collisions(
        self,
        plan: SyncPlan,
        remotes: Mapping[str, RemoteFile],
    ) -> SyncPlan:
        entries: list[tuple[str, RemotePath, RemotePath]] = []
        for file_id in sorted(plan.target_paths):
            remote = remotes[file_id]
            for path in plan.target_paths[file_id]:
                rendered = replace(
                    path,
                    parts=self._render_parts(path.parts, remote.native_kind),
                )
                entries.append((file_id, rendered, rendered))

        for _ in range(len(entries) + 1):
            owners: dict[tuple[str, tuple[str, ...]], list[int]] = {}
            for index, (_, _, candidate) in enumerate(entries):
                owners.setdefault((candidate.root_id, candidate.parts), []).append(
                    index
                )
            conflicts = [indexes for indexes in owners.values() if len(indexes) > 1]
            if not conflicts:
                break
            if any(
                len({entries[index][0] for index in indexes}) == 1
                for indexes in conflicts
            ):
                raise DriveRagError(
                    "rendered targets are duplicated for one file",
                    code="INVALID_INVENTORY",
                )
            changed = False
            for indexes in conflicts:
                for index in indexes:
                    file_id, original, candidate = entries[index]
                    name = original.parts[-1]
                    suffix = Path(name).suffix
                    stem = name[: -len(suffix)] if suffix else name
                    digest = hashlib.sha256(file_id.encode("utf-8")).hexdigest()[:8]
                    suffixed = replace(
                        original,
                        parts=(
                            *original.parts[:-1],
                            f"{stem}__{digest}{suffix}",
                        ),
                    )
                    if suffixed != candidate:
                        entries[index] = (file_id, original, suffixed)
                        changed = True
            if not changed:
                raise DriveRagError(
                    "rendered target collision remains after deterministic suffixing",
                    code="INVALID_INVENTORY",
                )
        else:  # pragma: no cover - bounded defensive guard
            raise DriveRagError(
                "could not resolve rendered target collisions",
                code="INVALID_INVENTORY",
            )

        targets: dict[str, list[RemotePath]] = {
            file_id: [] for file_id in sorted(plan.target_paths)
        }
        for file_id, _, candidate in entries:
            targets[file_id].append(candidate)
        return replace(
            plan,
            target_paths={
                file_id: tuple(paths) for file_id, paths in targets.items()
            },
        )

    def _validate_rendered_targets(
        self,
        targets: Mapping[str, Sequence[RemotePath]],
        remotes: Mapping[str, RemoteFile],
        aliases: Mapping[str, FolderConfig],
    ) -> None:
        owners: dict[Path, str] = {}
        for file_id, paths in targets.items():
            remote = remotes[file_id]
            for path in paths:
                rendered = self._mirror_path(remote, path, aliases)
                existing = owners.get(rendered)
                if existing is not None and existing != file_id:
                    raise DriveRagError(
                        "exported mirror paths collide after PDF naming",
                        code="INVALID_INVENTORY",
                    )
                owners[rendered] = file_id

    def _model_identity(self) -> str:
        return json.dumps(
            {
                "model_id": self.embedder.model_id,
                "dimension": self.embedder.dimension,
                "distance": "cosine",
                "schema_version": SCHEMA_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
