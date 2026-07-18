"""Bounded retrieval and evidence citations."""

from __future__ import annotations

import json
import math
from pathlib import PurePosixPath
from typing import Sequence

from .index import MIME_TYPE_PATTERN
from .models import Evidence
from .protocol import DriveRagError


class QueryService:
    def __init__(
        self,
        index,
        embedder,
        *,
        distance_threshold: float = 0.45,
        configured: bool = True,
        index_stale: bool = False,
    ) -> None:
        self.index = index
        self.embedder = embedder
        self.distance_threshold = distance_threshold
        self.configured = configured
        self.index_stale = index_stale

    def query(
        self, question: str, root_ids: Sequence[str] = (), limit: int = 8
    ) -> tuple[Evidence, ...]:
        if not self.configured:
            raise DriveRagError(
                "no enabled Drive folders are configured",
                code="CONFIGURATION_REQUIRED",
            )
        if self.index_stale:
            raise DriveRagError(
                "the persistent index is not synchronized with the manifest",
                code="INDEX_STALE",
            )
        if not isinstance(question, str) or not question.strip():
            raise DriveRagError(
                "question must be a non-empty string", code="INVALID_REQUEST"
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise DriveRagError(
                "evidence limit must be a positive integer", code="INVALID_REQUEST"
            )
        raw = self.index.query(self.embedder.embed_query(question), root_ids, 32)
        try:
            ids = raw["ids"][0]
            documents = raw["documents"][0]
            metadatas = raw["metadatas"][0]
            distances = raw["distances"][0]
            candidates = list(zip(ids, documents, metadatas, distances, strict=True))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise DriveRagError(
                "persistent index returned malformed candidates", code="INDEX_STALE"
            ) from exc
        evidence: list[Evidence] = []
        seen_chunks: set[str] = set()
        required_metadata = {
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
        for record_id, document, metadata, distance in candidates:
            try:
                identity = json.loads(record_id)
                if (
                    not isinstance(identity, list)
                    or len(identity) != 2
                    or any(not isinstance(item, str) or not item for item in identity)
                    or not isinstance(document, str)
                    or not isinstance(metadata, dict)
                    or not required_metadata.issubset(metadata)
                    or any(
                        not isinstance(metadata[field], str) or not metadata[field]
                        for field in required_metadata
                    )
                    or MIME_TYPE_PATTERN.fullmatch(metadata["mime_type"]) is None
                    or metadata["root_id"] != identity[1]
                ):
                    raise ValueError("invalid candidate identity")
                numeric_distance = float(distance)
                if not math.isfinite(numeric_distance):
                    raise ValueError("invalid candidate distance")
                chunk_id, record_root_id = identity
                if root_ids and record_root_id not in root_ids:
                    raise ValueError("candidate escaped exact root filter")
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise DriveRagError(
                    "persistent index returned malformed candidates",
                    code="INDEX_STALE",
                ) from exc
            if chunk_id in seen_chunks or numeric_distance > self.distance_threshold:
                continue
            seen_chunks.add(chunk_id)
            drive_path = str(metadata["drive_path"])
            evidence.append(
                Evidence(
                    excerpt=str(document)[:1000],
                    file_id=str(metadata["drive_file_id"]),
                    file_name=PurePosixPath(drive_path).name,
                    folder_alias=str(metadata["folder_alias"]),
                    drive_path=drive_path,
                    drive_url=str(metadata["drive_url"]),
                    local_path=str(metadata["local_path"]),
                    locator=str(metadata["locator"]),
                    revision=str(metadata["revision"]),
                    content_hash=str(metadata["content_hash"]),
                    mime_type=str(metadata["mime_type"]),
                    distance=numeric_distance,
                )
            )
            if len(evidence) >= min(limit, 8):
                break
        if not evidence:
            raise DriveRagError(
                "no retrieved chunk met the relevance threshold",
                code="NO_RELEVANT_EVIDENCE",
            )
        return tuple(evidence)
