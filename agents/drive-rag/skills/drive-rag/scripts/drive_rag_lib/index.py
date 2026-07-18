"""Persistent exact-root Chroma index for Drive chunks."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Callable, Mapping, Sequence

import chromadb

from .models import Chunk, Manifest
from .protocol import DriveRagError, SCHEMA_VERSION


COLLECTION_NAME = "drive_rag_v1"
DISTANCE = "cosine"
ROOT_FIELDS = frozenset({"alias", "drive_path", "drive_url", "local_path"})
MIME_TYPE_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*"
)


class ChromaIndex:
    def __init__(
        self,
        path: Path,
        model_id: str,
        dimension: int,
        *,
        rebuild: bool = False,
        create_if_missing: bool = True,
    ) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise DriveRagError("model ID must be non-empty", code="INVALID_INDEX_INPUT")
        if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
            raise DriveRagError(
                "embedding dimension must be a positive integer",
                code="INVALID_INDEX_INPUT",
            )
        self.path = Path(path)
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        database = self.path / "chroma.sqlite3"
        if not create_if_missing and (database.is_symlink() or not database.is_file()):
            raise DriveRagError(
                "persistent index database is missing",
                code="INDEX_STALE",
            )
        self.client = chromadb.PersistentClient(path=self.path)
        self.model_identity: dict[str, str | int] = {
            "model_id": model_id,
            "dimension": dimension,
            "distance": DISTANCE,
            "schema_version": SCHEMA_VERSION,
        }
        existing_names: set[str] = set()
        for collection in self.client.list_collections():
            name = collection if isinstance(collection, str) else getattr(
                collection, "name", None
            )
            if not isinstance(name, str) or not name.strip() or name in existing_names:
                raise DriveRagError(
                    "persistent index collection inventory is invalid",
                    code="INDEX_STALE" if not create_if_missing else "INVALID_STATE",
                )
            existing_names.add(name)
        if not create_if_missing and existing_names != {COLLECTION_NAME}:
            raise DriveRagError(
                "persistent index collection inventory differs from committed state",
                code="INDEX_STALE",
            )
        if rebuild and COLLECTION_NAME in existing_names:
            self.client.delete_collection(COLLECTION_NAME)
            existing_names.remove(COLLECTION_NAME)
        if COLLECTION_NAME in existing_names:
            collection = self.client.get_collection(COLLECTION_NAME)
            actual = collection.metadata or {}
            if any(actual.get(key) != value for key, value in self.model_identity.items()):
                raise DriveRagError(
                    "persistent index model identity or dimension differs; rebuild explicitly",
                    code="INDEX_MODEL_MISMATCH",
                )
            if actual.get("hnsw:space") != DISTANCE:
                raise DriveRagError(
                    "persistent index distance differs; rebuild explicitly",
                    code="INDEX_MODEL_MISMATCH",
                )
            self.collection = collection
        elif not create_if_missing:
            raise DriveRagError(
                "persistent index collection is missing",
                code="INDEX_STALE",
            )
        else:
            self.collection = self.client.create_collection(
                COLLECTION_NAME,
                metadata={"hnsw:space": DISTANCE, **self.model_identity},
            )
        self.dimension = dimension

    @staticmethod
    def _record_id(chunk_id: str, root_id: str) -> str:
        return json.dumps([chunk_id, root_id], ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _require_identity(value: str, name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise DriveRagError(
                f"{name} must be a non-empty string", code="INVALID_INDEX_INPUT"
            )

    def _validate_upsert(
        self,
        file_id: str,
        revision: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        roots: Mapping[str, Mapping[str, str]],
        mime_type: str,
    ) -> None:
        self._require_identity(file_id, "file ID")
        self._require_identity(revision, "revision")
        if (
            not isinstance(mime_type, str)
            or MIME_TYPE_PATTERN.fullmatch(mime_type) is None
        ):
            raise DriveRagError(
                "MIME type must be a valid type/subtype value",
                code="INVALID_INDEX_INPUT",
            )
        if len(chunks) != len(embeddings):
            raise DriveRagError(
                "chunk and embedding counts differ", code="INVALID_INDEX_INPUT"
            )
        chunk_ids: set[str] = set()
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            self._require_identity(chunk.chunk_id, "chunk ID")
            if not chunk.chunk_id.startswith(f"{file_id}:") or not chunk.chunk_id[
                len(file_id) + 1 :
            ]:
                raise DriveRagError(
                    "chunk ID must be scoped to its Drive file ID",
                    code="INVALID_INDEX_INPUT",
                )
            if chunk.chunk_id in chunk_ids:
                raise DriveRagError(
                    "chunk IDs must be unique", code="INVALID_INDEX_INPUT"
                )
            chunk_ids.add(chunk.chunk_id)
            if not isinstance(chunk.text, str) or not chunk.text.strip():
                raise DriveRagError(
                    "chunk text must be non-empty", code="INVALID_INDEX_INPUT"
                )
            self._require_identity(chunk.locator, "locator")
            supplied_content_hash = chunk.metadata.get("content_hash")
            if supplied_content_hash is not None and (
                not isinstance(supplied_content_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", supplied_content_hash) is None
            ):
                raise DriveRagError(
                    "content hash must be a lowercase SHA-256 digest",
                    code="INVALID_INDEX_INPUT",
                )
            if len(embedding) != self.dimension:
                raise DriveRagError(
                    f"embedding dimension must be {self.dimension}",
                    code="INVALID_INDEX_INPUT",
                )
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in embedding
            ):
                raise DriveRagError(
                    "embedding values must be finite numbers",
                    code="INVALID_INDEX_INPUT",
                )
        if not isinstance(roots, Mapping) or not roots:
            raise DriveRagError(
                "at least one root is required", code="INVALID_INDEX_INPUT"
            )
        for root_id, root in roots.items():
            self._require_identity(root_id, "root ID")
            if not isinstance(root, Mapping) or set(root) != ROOT_FIELDS:
                raise DriveRagError(
                    "root metadata has invalid fields", code="INVALID_INDEX_INPUT"
                )
            for field in ROOT_FIELDS:
                self._require_identity(root[field], field)

    def upsert(
        self,
        file_id: str,
        revision: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        roots: Mapping[str, Mapping[str, str]],
        *,
        mime_type: str,
    ) -> None:
        """Replace a file's records atomically at the Chroma layer."""

        self._upsert(
            file_id,
            revision,
            chunks,
            embeddings,
            roots,
            mime_type=mime_type,
            retire_stale=True,
        )

    def stage_upsert(
        self,
        file_id: str,
        revision: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        roots: Mapping[str, Mapping[str, str]],
        *,
        mime_type: str,
    ) -> None:
        """Add a candidate revision without retiring the committed revision."""

        self._upsert(
            file_id,
            revision,
            chunks,
            embeddings,
            roots,
            mime_type=mime_type,
            retire_stale=False,
        )

    def _upsert(
        self,
        file_id: str,
        revision: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        roots: Mapping[str, Mapping[str, str]],
        *,
        mime_type: str,
        retire_stale: bool,
    ) -> None:
        self._validate_upsert(
            file_id, revision, chunks, embeddings, roots, mime_type
        )
        previous = self._collect_where(
            {"drive_file_id": file_id},
            ["documents", "metadatas", "embeddings"],
        )
        expected = self.expected_records(
            file_id,
            revision,
            chunks,
            embeddings,
            roots,
            mime_type=mime_type,
        )
        ids = list(expected)
        documents = [expected[record_id][0] for record_id in ids]
        metadatas = [expected[record_id][1] for record_id in ids]
        vectors = [expected[record_id][2] for record_id in ids]

        batch_size = self.client.get_max_batch_size()
        try:
            for start in range(0, len(ids), batch_size):
                stop = start + batch_size
                self.collection.upsert(
                    ids=ids[start:stop],
                    embeddings=vectors[start:stop],
                    documents=documents[start:stop],
                    metadatas=metadatas[start:stop],
                )
            if retire_stale:
                existing = self._collect_where(
                    {"drive_file_id": file_id}, []
                )["ids"]
                stale_ids = sorted(set(existing) - set(ids))
                self._delete_ids(stale_ids)
        except Exception as exc:
            try:
                self._delete_where({"drive_file_id": file_id})
                previous_ids = previous["ids"]
                previous_embeddings = previous["embeddings"]
                previous_documents = previous["documents"]
                previous_metadatas = previous["metadatas"]
                if previous_ids:
                    assert previous_embeddings is not None
                    assert previous_documents is not None
                    assert previous_metadatas is not None
                    for start in range(0, len(previous_ids), batch_size):
                        stop = start + batch_size
                        self.collection.upsert(
                            ids=previous_ids[start:stop],
                            embeddings=previous_embeddings[start:stop],
                            documents=previous_documents[start:stop],
                            metadatas=previous_metadatas[start:stop],
                        )
            except Exception as rollback_exc:
                raise DriveRagError(
                    "persistent index write and rollback failed",
                    code="INDEX_ROLLBACK_FAILED",
                ) from rollback_exc
            raise DriveRagError(
                f"could not update persistent index: {type(exc).__name__}",
                code="INDEX_WRITE_FAILED",
            ) from exc

    def expected_records(
        self,
        file_id: str,
        revision: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        roots: Mapping[str, Mapping[str, str]],
        *,
        mime_type: str,
    ) -> dict[str, tuple[str, dict[str, str], list[float]]]:
        """Build the exact records represented by prepared chunks and roots."""

        self._validate_upsert(
            file_id, revision, chunks, embeddings, roots, mime_type
        )
        result: dict[str, tuple[str, dict[str, str], list[float]]] = {}
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            content_hash = str(
                chunk.metadata.get("content_hash")
                or hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
            )
            for root_id, root in roots.items():
                record_id = self._record_id(chunk.chunk_id, root_id)
                result[record_id] = (
                    chunk.text,
                    {
                        "drive_file_id": file_id,
                        "revision": revision,
                        "root_id": root_id,
                        "folder_alias": root["alias"],
                        "drive_path": root["drive_path"],
                        "drive_url": root["drive_url"],
                        "local_path": root["local_path"],
                        "locator": chunk.locator,
                        "content_hash": content_hash,
                        "mime_type": mime_type,
                    },
                    [float(value) for value in embedding],
                )
        return result

    def assert_manifest_consistent(self, manifest: Manifest) -> None:
        """Fail closed unless Chroma exactly represents the committed manifest."""

        if not isinstance(manifest, Manifest):
            raise DriveRagError(
                "committed manifest is invalid", code="INDEX_STALE"
            )
        try:
            expected_count = self._manifest_record_count(manifest)
            if self.collection.count() != expected_count:
                raise DriveRagError(
                    "persistent index record count differs from the manifest",
                    code="INDEX_STALE",
                )
            for batch in self._record_batches(
                self._iter_manifest_records(manifest)
            ):
                actual = self._get_exact_records(batch, ["metadatas"])
                for record_id, file_id, revision, root_id in batch:
                    metadata = actual[record_id][1]
                    if (
                        not isinstance(metadata, dict)
                        or metadata.get("drive_file_id") != file_id
                        or metadata.get("revision") != revision
                        or metadata.get("root_id") != root_id
                        or not isinstance(metadata.get("mime_type"), str)
                        or MIME_TYPE_PATTERN.fullmatch(metadata["mime_type"]) is None
                    ):
                        raise DriveRagError(
                            "persistent index metadata differs from the manifest",
                            code="INDEX_STALE",
                        )
        except DriveRagError:
            raise
        except Exception as exc:
            raise DriveRagError(
                "could not verify persistent index readiness", code="INDEX_STALE"
            ) from exc

    @classmethod
    def _iter_manifest_records(cls, manifest: Manifest):
        for file_id in sorted(manifest.files):
            committed = manifest.files[file_id]
            root_ids = sorted({path.root_id for path in committed.paths})
            for chunk_id in committed.active_chunk_ids:
                for root_id in root_ids:
                    yield (
                        cls._record_id(chunk_id, root_id),
                        file_id,
                        committed.revision,
                        root_id,
                    )

    @staticmethod
    def _manifest_record_count(manifest: Manifest) -> int:
        return sum(
            len(committed.active_chunk_ids)
            * len({path.root_id for path in committed.paths})
            for committed in manifest.files.values()
        )

    def _record_batches(self, records):
        batch_size = self.client.get_max_batch_size()
        batch: list[tuple[str, str, str, str]] = []
        for record in records:
            batch.append(record)
            if len(batch) == batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def _get_exact_records(
        self,
        expected: Sequence[tuple[str, str, str, str]],
        include: Sequence[str],
    ) -> dict[str, tuple[object, object, object]]:
        ids = [item[0] for item in expected]
        records = self.collection.get(ids=ids, include=list(include))
        returned = records["ids"]
        if len(returned) != len(ids) or len(set(returned)) != len(returned):
            raise DriveRagError(
                "persistent index is missing or duplicating expected records",
                code="INDEX_STALE",
            )
        if set(returned) != set(ids):
            raise DriveRagError(
                "persistent index returned unexpected record identities",
                code="INDEX_STALE",
            )
        documents = records.get("documents")
        metadatas = records.get("metadatas")
        embeddings = records.get("embeddings")
        documents = documents if documents is not None else [None] * len(returned)
        metadatas = metadatas if metadatas is not None else [None] * len(returned)
        embeddings = embeddings if embeddings is not None else [None] * len(returned)
        if not (
            len(documents) == len(returned)
            and len(metadatas) == len(returned)
            and len(embeddings) == len(returned)
        ):
            raise DriveRagError(
                "persistent index record fields are incomplete", code="INDEX_STALE"
            )
        return {
            record_id: (document, metadata, embedding)
            for record_id, document, metadata, embedding in zip(
                returned, documents, metadatas, embeddings, strict=True
            )
        }

    def delete_file(self, file_id: str) -> None:
        self._require_identity(file_id, "file ID")
        self._delete_where({"drive_file_id": file_id})

    def delete_root(self, root_id: str) -> None:
        self._require_identity(root_id, "root ID")
        self._delete_where({"root_id": root_id})

    def _delete_ids(self, ids: Sequence[str]) -> None:
        batch_size = self.client.get_max_batch_size()
        for start in range(0, len(ids), batch_size):
            self.collection.delete(ids=list(ids[start : start + batch_size]))

    def _delete_where(self, where: Mapping[str, object]) -> None:
        batch_size = self.client.get_max_batch_size()
        while True:
            ids = self.collection.get(
                where=dict(where), limit=batch_size, offset=0, include=[]
            )["ids"]
            if not ids:
                return
            self._delete_ids(ids)

    def _iter_where_pages(
        self, where: Mapping[str, object], include: Sequence[str]
    ):
        batch_size = self.client.get_max_batch_size()
        offset = 0
        while True:
            records = self.collection.get(
                where=dict(where),
                limit=batch_size,
                offset=offset,
                include=list(include),
            )
            ids = records["ids"]
            if len(ids) > batch_size:
                raise DriveRagError(
                    "persistent index page exceeds its bound", code="INDEX_STALE"
                )
            if not ids:
                return
            yield records
            offset += len(ids)
            if len(ids) < batch_size:
                return

    def _collect_where(
        self, where: Mapping[str, object], include: Sequence[str]
    ) -> dict[str, list[object]]:
        result: dict[str, list[object]] = {
            "ids": [],
            "documents": [],
            "metadatas": [],
            "embeddings": [],
        }
        for page in self._iter_where_pages(where, include):
            result["ids"].extend(page["ids"])
            for key in ("documents", "metadatas", "embeddings"):
                values = page.get(key)
                if values is not None:
                    result[key].extend(values)
        return result

    def retire_file_except(
        self,
        file_id: str,
        active_chunk_ids: Sequence[str],
        root_ids: Sequence[str],
    ) -> None:
        """Retire old candidate records only after filesystem promotion."""

        self._require_identity(file_id, "file ID")
        chunks = tuple(active_chunk_ids)
        roots = tuple(root_ids)
        if len(set(chunks)) != len(chunks) or any(
            not isinstance(chunk_id, str)
            or not chunk_id.startswith(f"{file_id}:")
            for chunk_id in chunks
        ):
            raise DriveRagError(
                "active chunk identities are invalid", code="INDEX_STALE"
            )
        if len(set(roots)) != len(roots) or any(
            not isinstance(root_id, str) or not root_id.strip()
            for root_id in roots
        ):
            raise DriveRagError(
                "active root identities are invalid", code="INDEX_STALE"
            )
        desired = {
            self._record_id(chunk_id, root_id)
            for chunk_id in chunks
            for root_id in roots
        }
        existing: set[str] = set()
        for page in self._iter_where_pages(
            {"drive_file_id": file_id}, []
        ):
            existing.update(page["ids"])
        missing = desired - existing
        if missing:
            raise DriveRagError(
                "staged index is missing promoted records", code="INDEX_STALE"
            )
        stale = sorted(existing - desired)
        self._delete_ids(stale)

    @classmethod
    def _manifest_records(
        cls, manifest: Manifest, file_ids: set[str] | None = None
    ) -> dict[str, tuple[str, str, str]]:
        result: dict[str, tuple[str, str, str]] = {}
        for file_id, committed in manifest.files.items():
            if file_ids is not None and file_id not in file_ids:
                continue
            for chunk_id in committed.active_chunk_ids:
                for root_id in {path.root_id for path in committed.paths}:
                    result[cls._record_id(chunk_id, root_id)] = (
                        file_id,
                        committed.revision,
                        root_id,
                    )
        return result

    @staticmethod
    def _record_matches(
        actual_document: object,
        actual_metadata: object,
        actual_embedding: object,
        expected: tuple[str, Mapping[str, str], Sequence[float]],
    ) -> bool:
        document, metadata, embedding = expected
        try:
            actual_vector = [float(value) for value in actual_embedding]  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return False
        return (
            actual_document == document
            and actual_metadata == dict(metadata)
            and len(actual_vector) == len(embedding)
            and all(
                math.isclose(actual, float(wanted), rel_tol=1e-6, abs_tol=1e-7)
                for actual, wanted in zip(actual_vector, embedding, strict=True)
            )
        )

    def _assert_base_record(
        self,
        record_id: str,
        actual: tuple[object, object, object],
        base: Manifest,
        file_id: str,
        revision: str,
        root_id: str,
        base_roots: Mapping[
            str, Mapping[str, Mapping[str, str]]
        ] | None = None,
    ) -> None:
        document, metadata, embedding = actual
        try:
            vector = [float(value) for value in embedding]  # type: ignore[union-attr]
        except (TypeError, ValueError) as exc:
            raise DriveRagError(
                "committed index embedding is invalid", code="INDEX_STALE"
            ) from exc
        required_fields = {
            "drive_file_id",
            "revision",
            "root_id",
            "folder_alias",
            "drive_path",
            "drive_url",
            "local_path",
            "locator",
            "content_hash",
            "mime_type",
        }
        identity = json.loads(record_id)
        chunk_id = identity[0]
        active_chunks = base.files[file_id].active_chunk_ids
        ordinal = active_chunks.index(chunk_id)
        content_hash = (
            hashlib.sha256(document.encode("utf-8")).hexdigest()
            if isinstance(document, str)
            else None
        )
        chunk_digest = (
            hashlib.sha256(
                f"{file_id}\0{revision}\0{metadata.get('locator')}\0{ordinal}\0{document}".encode()
            ).hexdigest()
            if isinstance(metadata, dict) and isinstance(document, str)
            else None
        )
        expected_root = (
            base_roots.get(file_id, {}).get(root_id)
            if base_roots is not None
            else None
        )
        if (
            not isinstance(document, str)
            or not document
            or not isinstance(metadata, dict)
            or set(metadata) != required_fields
            or metadata.get("drive_file_id") != file_id
            or metadata.get("revision") != revision
            or metadata.get("root_id") != root_id
            or any(
                not isinstance(metadata.get(field), str)
                or not metadata[field].strip()
                for field in {
                    "folder_alias",
                    "drive_path",
                    "drive_url",
                    "local_path",
                }
            )
            or not metadata["drive_url"].startswith("https://")
            or not Path(metadata["local_path"]).is_absolute()
            or not isinstance(metadata.get("locator"), str)
            or not metadata["locator"]
            or metadata.get("content_hash") != content_hash
            or chunk_id != f"{file_id}:{chunk_digest}"
            or not isinstance(metadata.get("mime_type"), str)
            or MIME_TYPE_PATTERN.fullmatch(metadata["mime_type"]) is None
            or len(vector) != self.dimension
            or any(not math.isfinite(value) for value in vector)
            or (
                expected_root is not None
                and any(
                    metadata.get(field) != value
                    for field, value in expected_root.items()
                )
            )
        ):
            raise DriveRagError(
                "committed index metadata changed during transition",
                code="INDEX_STALE",
            )

    @classmethod
    def _manifest_contains_record(
        cls, manifest: Manifest, record_id: str, file_id: str
    ) -> bool:
        try:
            identity = json.loads(record_id)
            if (
                not isinstance(identity, list)
                or len(identity) != 2
                or not all(isinstance(item, str) for item in identity)
            ):
                return False
            chunk_id, root_id = identity
            committed = manifest.files.get(file_id)
            return (
                committed is not None
                and chunk_id in committed.active_chunk_ids
                and root_id in {path.root_id for path in committed.paths}
            )
        except (json.JSONDecodeError, TypeError):
            return False

    def assert_transition_records(
        self,
        base: Manifest,
        target: Manifest,
        candidates: Mapping[
            str, tuple[str, Mapping[str, str], Sequence[float]]
        ],
        base_roots: Mapping[
            str, Mapping[str, Mapping[str, str]]
        ] | None = None,
    ) -> None:
        """Verify exact staged candidates while the old revision still exists."""

        if any(
            not self._manifest_contains_record(
                target,
                record_id,
                str(candidates[record_id][1].get("drive_file_id")),
            )
            for record_id in candidates
        ):
            raise DriveRagError(
                "staged candidates are not target records", code="INDEX_STALE"
            )
        candidate_only = [
            record_id
            for record_id in sorted(candidates)
            if not self._manifest_contains_record(
                base,
                record_id,
                str(candidates[record_id][1].get("drive_file_id")),
            )
        ]
        expected_count = self._manifest_record_count(base) + len(candidate_only)
        if self.collection.count() != expected_count:
            raise DriveRagError(
                "staged index record count differs from the transition",
                code="INDEX_STALE",
            )

        def expected_records():
            yield from self._iter_manifest_records(base)
            for record_id in candidate_only:
                metadata = candidates[record_id][1]
                yield (
                    record_id,
                    str(metadata["drive_file_id"]),
                    str(metadata["revision"]),
                    str(metadata["root_id"]),
                )

        for batch in self._record_batches(expected_records()):
            actual = self._get_exact_records(
                batch, ["documents", "metadatas", "embeddings"]
            )
            for record_id, file_id, revision, root_id in batch:
                if self._manifest_contains_record(base, record_id, file_id):
                    self._assert_base_record(
                        record_id,
                        actual[record_id],
                        base,
                        file_id,
                        revision,
                        root_id,
                        base_roots,
                    )
                candidate = candidates.get(record_id)
                if candidate is not None and not self._record_matches(
                    *actual[record_id], candidate
                ):
                    raise DriveRagError(
                        "staged index metadata differs from prepared state",
                        code="INDEX_STALE",
                    )

    def assert_transition_snapshot(
        self,
        snapshot: Mapping[str, Sequence[object]],
        base: Manifest,
        target: Manifest,
        candidates: Mapping[
            str, tuple[str, Mapping[str, str], Sequence[float]]
        ],
        base_roots: Mapping[
            str, Mapping[str, Mapping[str, str]]
        ] | None = None,
        file_ids: set[str] | None = None,
    ) -> None:
        """Verify an immutable preactivation snapshot against journal state."""

        base_records = self._manifest_records(base, file_ids)
        target_records = self._manifest_records(target, file_ids)
        if file_ids is not None:
            candidates = {
                record_id: expected
                for record_id, expected in candidates.items()
                if expected[1].get("drive_file_id") in file_ids
            }
        allowed = set(base_records) | set(candidates)
        if set(snapshot) != {"ids", "documents", "metadatas", "embeddings"}:
            raise DriveRagError(
                "activation snapshot fields are invalid", code="INDEX_STALE"
            )
        try:
            ids = list(snapshot["ids"])
            documents = list(snapshot["documents"])
            metadatas = list(snapshot["metadatas"])
            embeddings = list(snapshot["embeddings"])
        except (KeyError, TypeError) as exc:
            raise DriveRagError(
                "activation snapshot records are invalid", code="INDEX_STALE"
            ) from exc
        if (
            len(ids) != len(documents)
            or len(ids) != len(metadatas)
            or len(ids) != len(embeddings)
            or any(not isinstance(record_id, str) for record_id in ids)
            or len(set(ids)) != len(ids)
            or set(ids) != allowed
            or set(candidates) - set(target_records)
        ):
            raise DriveRagError(
                "staged index record identities differ from the transition",
                code="INDEX_STALE",
            )
        actual = {
            record_id: (document, metadata, embedding)
            for record_id, document, metadata, embedding in zip(
                ids, documents, metadatas, embeddings,
                strict=True,
            )
        }
        for record_id, (file_id, revision, root_id) in base_records.items():
            document, metadata, embedding = actual[record_id]
            try:
                vector = [float(value) for value in embedding]  # type: ignore[union-attr]
            except (TypeError, ValueError) as exc:
                raise DriveRagError(
                    "committed index embedding is invalid", code="INDEX_STALE"
                ) from exc
            required_fields = {
                "drive_file_id",
                "revision",
                "root_id",
                "folder_alias",
                "drive_path",
                "drive_url",
                "local_path",
                "locator",
                "content_hash",
                "mime_type",
            }
            identity = json.loads(record_id)
            chunk_id = identity[0]
            active_chunks = base.files[file_id].active_chunk_ids
            ordinal = active_chunks.index(chunk_id)
            content_hash = (
                hashlib.sha256(document.encode("utf-8")).hexdigest()
                if isinstance(document, str)
                else None
            )
            chunk_digest = (
                hashlib.sha256(
                    f"{file_id}\0{revision}\0{metadata.get('locator')}\0{ordinal}\0{document}".encode()
                ).hexdigest()
                if isinstance(metadata, dict) and isinstance(document, str)
                else None
            )
            expected_root = (
                base_roots.get(file_id, {}).get(root_id)
                if base_roots is not None
                else None
            )
            if (
                not isinstance(document, str)
                or not document
                or not isinstance(metadata, dict)
                or set(metadata) != required_fields
                or metadata.get("drive_file_id") != file_id
                or metadata.get("revision") != revision
                or metadata.get("root_id") != root_id
                or any(
                    not isinstance(metadata.get(field), str)
                    or not metadata[field].strip()
                    for field in {
                        "folder_alias",
                        "drive_path",
                        "drive_url",
                        "local_path",
                    }
                )
                or not metadata["drive_url"].startswith("https://")
                or not Path(metadata["local_path"]).is_absolute()
                or not isinstance(metadata.get("locator"), str)
                or not metadata["locator"]
                or metadata.get("content_hash") != content_hash
                or chunk_id != f"{file_id}:{chunk_digest}"
                or not isinstance(metadata.get("mime_type"), str)
                or MIME_TYPE_PATTERN.fullmatch(metadata["mime_type"]) is None
                or len(vector) != self.dimension
                or any(not math.isfinite(value) for value in vector)
                or (
                    expected_root is not None
                    and any(
                        metadata.get(field) != value
                        for field, value in expected_root.items()
                    )
                )
            ):
                raise DriveRagError(
                    "committed index metadata changed during transition",
                    code="INDEX_STALE",
                )
        for record_id, expected in candidates.items():
            if not self._record_matches(*actual[record_id], expected):
                raise DriveRagError(
                    "staged index metadata differs from prepared state",
                    code="INDEX_STALE",
                )

    def assert_snapshot_embeddings_match_current(
        self,
        snapshot: Mapping[str, Sequence[object]],
        file_ids: set[str],
        embed_missing: Callable[[Sequence[str]], list[list[float]]],
    ) -> None:
        """Bind rollback vectors for unchanged affected files to live Chroma."""

        if not file_ids:
            return
        snapshot_vectors: dict[tuple[str, str], tuple[str, list[float]]] = {}
        for record_id, document, metadata, embedding in zip(
            snapshot["ids"],
            snapshot["documents"],
            snapshot["metadatas"],
            snapshot["embeddings"],
            strict=True,
        ):
            if (
                not isinstance(record_id, str)
                or not isinstance(document, str)
                or not isinstance(metadata, dict)
            ):
                raise DriveRagError(
                    "activation snapshot records are invalid", code="INDEX_STALE"
                )
            file_id = metadata.get("drive_file_id")
            if file_id not in file_ids:
                continue
            identity = json.loads(record_id)
            key = (file_id, identity[0])
            vector = [float(value) for value in embedding]  # type: ignore[union-attr]
            previous = snapshot_vectors.get(key)
            if previous is not None and (
                previous[0] != document
                or not self._vectors_match(previous[1], vector)
            ):
                raise DriveRagError(
                    "activation snapshot root vectors disagree", code="INDEX_STALE"
                )
            snapshot_vectors[key] = (document, vector)
        missing: list[tuple[str, list[float]]] = []
        for file_id in file_ids:
            live: dict[str, list[list[float]]] = {}
            for current in self._iter_where_pages(
                {"drive_file_id": file_id}, ["embeddings"]
            ):
                embeddings = current["embeddings"]
                if embeddings is None:
                    raise DriveRagError(
                        "live index embeddings are missing", code="INDEX_STALE"
                    )
                for record_id, embedding in zip(
                    current["ids"], embeddings, strict=True
                ):
                    identity = json.loads(record_id)
                    live.setdefault(identity[0], []).append(
                        [float(value) for value in embedding]
                    )
            for (snapshot_file_id, chunk_id), (document, vector) in snapshot_vectors.items():
                if snapshot_file_id == file_id and not any(
                    self._vectors_match(vector, candidate)
                    for candidate in live.get(chunk_id, ())
                ):
                    if live.get(chunk_id):
                        raise DriveRagError(
                            "activation snapshot embedding differs from live index",
                            code="INDEX_STALE",
                        )
                    missing.append((document, vector))
        batch_size = self.client.get_max_batch_size()
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            computed = embed_missing([item[0] for item in batch])
            if len(computed) != len(batch) or any(
                not self._vectors_match(expected, actual)
                for (_, expected), actual in zip(batch, computed, strict=True)
            ):
                raise DriveRagError(
                    "activation snapshot embedding differs from source content",
                    code="INDEX_STALE",
                )

    @staticmethod
    def _vectors_match(
        left: Sequence[float], right: Sequence[float]
    ) -> bool:
        return len(left) == len(right) and all(
            math.isclose(actual, wanted, rel_tol=1e-6, abs_tol=1e-7)
            for actual, wanted in zip(left, right, strict=True)
        )

    def assert_target_records(
        self,
        target: Manifest,
        candidates: Mapping[
            str, tuple[str, Mapping[str, str], Sequence[float]]
        ],
        roots: Mapping[str, Mapping[str, Mapping[str, str]]],
        mime_types: Mapping[str, str],
    ) -> None:
        """Verify the complete target index immediately before manifest commit."""

        self.assert_manifest_consistent(target)
        required_fields = {
            "drive_file_id",
            "revision",
            "root_id",
            "folder_alias",
            "drive_path",
            "drive_url",
            "local_path",
            "locator",
            "content_hash",
            "mime_type",
        }
        previous_chunk: tuple[str, str, list[float]] | None = None
        for batch in self._record_batches(self._iter_manifest_records(target)):
            actual = self._get_exact_records(
                batch, ["documents", "metadatas", "embeddings"]
            )
            for record_id, file_id, revision, root_id in batch:
                document, metadata, embedding = actual[record_id]
                root = roots[file_id][root_id]
                identity = json.loads(record_id)
                chunk_id = identity[0]
                active_chunks = target.files[file_id].active_chunk_ids
                try:
                    ordinal = active_chunks.index(chunk_id)
                except ValueError as exc:  # pragma: no cover - stream guarantees this
                    raise DriveRagError(
                        "target chunk identity is not active", code="INDEX_STALE"
                    ) from exc
                content_hash = (
                    hashlib.sha256(document.encode("utf-8")).hexdigest()
                    if isinstance(document, str)
                    else None
                )
                chunk_digest = (
                    hashlib.sha256(
                        f"{file_id}\0{revision}\0{metadata.get('locator')}\0{ordinal}\0{document}".encode()
                    ).hexdigest()
                    if isinstance(metadata, dict) and isinstance(document, str)
                    else None
                )
                try:
                    vector = [float(value) for value in embedding]  # type: ignore[union-attr]
                except (TypeError, ValueError) as exc:
                    raise DriveRagError(
                        "target index embedding is invalid", code="INDEX_STALE"
                    ) from exc
                if (
                    not isinstance(document, str)
                    or not document
                    or not isinstance(metadata, dict)
                    or set(metadata) != required_fields
                    or metadata.get("drive_file_id") != file_id
                    or metadata.get("revision") != revision
                    or metadata.get("root_id") != root_id
                    or metadata.get("folder_alias") != root["alias"]
                    or metadata.get("drive_path") != root["drive_path"]
                    or metadata.get("drive_url") != root["drive_url"]
                    or metadata.get("local_path") != root["local_path"]
                    or metadata.get("mime_type") != mime_types[file_id]
                    or not isinstance(metadata.get("locator"), str)
                    or not metadata["locator"]
                    or not isinstance(metadata.get("content_hash"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", metadata["content_hash"])
                    is None
                    or metadata["content_hash"] != content_hash
                    or chunk_id != f"{file_id}:{chunk_digest}"
                    or len(vector) != self.dimension
                    or any(not math.isfinite(value) for value in vector)
                ):
                    raise DriveRagError(
                        "target index metadata differs from journal state",
                        code="INDEX_STALE",
                    )
                key = (file_id, chunk_id)
                if (
                    previous_chunk is not None
                    and previous_chunk[:2] == key
                    and not self._vectors_match(previous_chunk[2], vector)
                ):
                    raise DriveRagError(
                        "target root embeddings disagree", code="INDEX_STALE"
                    )
                previous_chunk = (file_id, chunk_id, vector)
                expected = candidates.get(record_id)
                if expected is not None and not self._record_matches(
                    *actual[record_id], expected
                ):
                    raise DriveRagError(
                        "target index content differs from prepared state",
                        code="INDEX_STALE",
                    )

    def iter_file_record_pages(self, file_ids: set[str]):
        """Yield bounded, file-filtered record pages for activation capture."""

        batch_size = self.client.get_max_batch_size()
        for file_id in sorted(file_ids):
            offset = 0
            while True:
                records = self.collection.get(
                    where={"drive_file_id": file_id},
                    limit=batch_size,
                    offset=offset,
                    include=["documents", "metadatas", "embeddings"],
                )
                ids = list(records["ids"])
                documents = records["documents"]
                metadatas = records["metadatas"]
                embeddings = records["embeddings"]
                if documents is None or metadatas is None or embeddings is None:
                    raise DriveRagError(
                        "activation page fields are incomplete", code="INDEX_STALE"
                    )
                if not (
                    len(ids) == len(documents)
                    and len(ids) == len(metadatas)
                    and len(ids) == len(embeddings)
                    and len(ids) <= batch_size
                ):
                    raise DriveRagError(
                        "activation page is invalid", code="INDEX_STALE"
                    )
                if not ids:
                    break
                yield {
                    "ids": ids,
                    "documents": list(documents),
                    "metadatas": [dict(item) for item in metadatas],
                    "embeddings": [
                        [float(value) for value in embedding]
                        for embedding in embeddings
                    ],
                }
                offset += len(ids)
                if len(ids) < batch_size:
                    break

    def restore_records(self, snapshot: Mapping[str, Sequence[object]]) -> None:
        try:
            batch_size = self.client.get_max_batch_size()
            while True:
                current = self.collection.get(
                    limit=batch_size, offset=0, include=[]
                )["ids"]
                if not current:
                    break
                self._delete_ids(current)
            ids = list(snapshot["ids"])
            documents = list(snapshot["documents"])
            metadatas = list(snapshot["metadatas"])
            embeddings = list(snapshot["embeddings"])
            for start in range(0, len(ids), batch_size):
                stop = start + batch_size
                self.collection.upsert(
                    ids=ids[start:stop],
                    documents=documents[start:stop],
                    metadatas=metadatas[start:stop],
                    embeddings=embeddings[start:stop],
                )
        except Exception as exc:
            raise DriveRagError(
                "could not restore persistent index transition",
                code="INDEX_ROLLBACK_FAILED",
            ) from exc

    def restore_file_records(
        self,
        snapshot: Mapping[str, Sequence[object]],
        file_ids: set[str],
    ) -> None:
        """Restore only files whose activation can mutate their records."""

        try:
            for file_id in sorted(file_ids):
                self._delete_where({"drive_file_id": file_id})
            ids = list(snapshot["ids"])
            documents = list(snapshot["documents"])
            metadatas = list(snapshot["metadatas"])
            embeddings = list(snapshot["embeddings"])
            batch_size = self.client.get_max_batch_size()
            for start in range(0, len(ids), batch_size):
                stop = start + batch_size
                self.collection.upsert(
                    ids=ids[start:stop],
                    documents=documents[start:stop],
                    metadatas=metadatas[start:stop],
                    embeddings=embeddings[start:stop],
                )
        except Exception as exc:
            raise DriveRagError(
                "could not restore affected index transition",
                code="INDEX_ROLLBACK_FAILED",
            ) from exc

    def repath_file(
        self,
        file_id: str,
        revision: str,
        active_chunk_ids: Sequence[str],
        roots: Mapping[str, Mapping[str, str]],
        *,
        mime_type: str,
    ) -> None:
        """Replace exact-root metadata while reusing committed vectors and text."""

        self._require_identity(file_id, "file ID")
        self._require_identity(revision, "revision")
        expected = set(active_chunk_ids)
        if len(expected) != len(active_chunk_ids) or any(
            not isinstance(item, str) or not item.startswith(f"{file_id}:")
            for item in active_chunk_ids
        ):
            raise DriveRagError(
                "active chunk identities are invalid", code="INDEX_STALE"
            )
        records = self._collect_where(
            {"drive_file_id": file_id},
            ["documents", "metadatas", "embeddings"],
        )
        if not expected:
            if records["ids"]:
                raise DriveRagError(
                    "unindexed manifest file has persistent records",
                    code="INDEX_STALE",
                )
            return
        documents = records["documents"]
        metadatas = records["metadatas"]
        embeddings = records["embeddings"]
        if documents is None or metadatas is None or embeddings is None:
            raise DriveRagError(
                "persistent index cannot reconstruct committed records",
                code="INDEX_STALE",
            )
        unique: dict[str, tuple[Chunk, list[float]]] = {}
        try:
            for record_id, document, metadata, embedding in zip(
                records["ids"], documents, metadatas, embeddings, strict=True
            ):
                identity = json.loads(record_id)
                if (
                    not isinstance(identity, list)
                    or len(identity) != 2
                    or not isinstance(identity[0], str)
                    or identity[0] not in expected
                    or not isinstance(document, str)
                    or not isinstance(metadata, dict)
                    or metadata.get("drive_file_id") != file_id
                    or metadata.get("revision") != revision
                    or not isinstance(metadata.get("locator"), str)
                    or not isinstance(metadata.get("content_hash"), str)
                ):
                    raise ValueError("record identity mismatch")
                chunk_id = identity[0]
                candidate = (
                    Chunk(
                        chunk_id,
                        document,
                        metadata["locator"],
                        {"content_hash": metadata["content_hash"]},
                    ),
                    [float(value) for value in embedding],
                )
                existing = unique.get(chunk_id)
                if existing is not None and existing != candidate:
                    raise ValueError("root records disagree")
                unique[chunk_id] = candidate
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DriveRagError(
                "persistent index records do not match the committed file",
                code="INDEX_STALE",
            ) from exc
        if set(unique) != expected:
            raise DriveRagError(
                "persistent index is missing committed chunks", code="INDEX_STALE"
            )
        ordered = [unique[chunk_id] for chunk_id in active_chunk_ids]
        self.upsert(
            file_id,
            revision,
            [item[0] for item in ordered],
            [item[1] for item in ordered],
            roots,
            mime_type=mime_type,
        )

    def count_file(self, file_id: str) -> int:
        self._require_identity(file_id, "file ID")
        return sum(
            len(page["ids"])
            for page in self._iter_where_pages(
                {"drive_file_id": file_id}, []
            )
        )

    def query(
        self,
        embedding: Sequence[float],
        root_ids: Sequence[str],
        candidates: int = 32,
    ) -> dict[str, object]:
        if len(embedding) != self.dimension:
            raise DriveRagError(
                f"query embedding dimension must be {self.dimension}",
                code="INVALID_INDEX_INPUT",
            )
        if any(not isinstance(item, str) or not item.strip() for item in root_ids):
            raise DriveRagError(
                "root filters must be non-empty strings", code="INVALID_INDEX_INPUT"
            )
        if self.collection.count() == 0:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        where = {"root_id": {"$in": list(root_ids)}} if root_ids else None
        return self.collection.query(
            query_embeddings=[[float(value) for value in embedding]],
            n_results=min(candidates, self.collection.count()),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
