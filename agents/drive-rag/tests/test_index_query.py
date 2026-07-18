import json

import pytest

import drive_rag_lib.embed as embed_module
import drive_rag
from drive_rag_lib.embed import FastEmbedE5
from drive_rag_lib.index import ChromaIndex
from drive_rag_lib.inventory import load_manifest
from drive_rag_lib.models import (
    Chunk,
    FolderConfig,
    Manifest,
    ManifestFile,
    RemotePath,
)
from drive_rag_lib.protocol import DriveRagError, atomic_write_json
from drive_rag_lib.query import QueryService
from drive_rag_lib.registry import Registry
from support import FakeEmbedder


def test_query_filters_root_and_cites_locator(tmp_path):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    chunk = Chunk("file-a:1", "Budget is 30.", "sheet:Budget!A1:B2", {})
    root = {
        "root-a": {
            "alias": "Finance",
            "drive_path": "Finance/Budget",
            "drive_url": "https://drive.google.com/file/d/file-a/view",
            "local_path": "mirrors/Finance/Budget.pdf",
        }
    }
    index.upsert(
        "file-a",
        "rev-1",
        [chunk],
        FakeEmbedder().embed_passages([chunk.text]),
        root,
        mime_type="application/vnd.google-apps.spreadsheet",
    )
    evidence = QueryService(index, FakeEmbedder()).query("budget", ("root-a",), 8)
    assert evidence[0].locator == "sheet:Budget!A1:B2"
    assert evidence[0].folder_alias == "Finance"


def test_query_alias_scope_is_casefolded_exact_and_never_approximate():
    folders = [
        FolderConfig(
            "root-a",
            "https://drive.google.com/drive/folders/root-a",
            "Finance",
            True,
        ),
        FolderConfig(
            "root-b",
            "https://drive.google.com/drive/folders/root-b",
            "Financial Planning",
            True,
        ),
    ]

    assert drive_rag.select_query_folders(folders, ["finance"]) == [folders[0]]
    with pytest.raises(DriveRagError, match="unknown enabled folder alias"):
        drive_rag.select_query_folders(folders, ["fin"])


def test_query_alias_scope_rejects_ambiguous_casefolded_configuration():
    folders = [
        FolderConfig(
            "root-a",
            "https://drive.google.com/drive/folders/root-a",
            "Finance",
            True,
        ),
        FolderConfig(
            "root-b",
            "https://drive.google.com/drive/folders/root-b",
            "finance",
            True,
        ),
    ]

    with pytest.raises(DriveRagError, match="alias") as error:
        drive_rag.select_query_folders(folders, ["Finance"])

    assert error.value.code == "INVALID_STATE"


def test_delete_file_removes_all_root_records(tmp_path):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    chunk = Chunk("file-a:1", "Policy", "page:1", {})
    roots = {
        key: {
            "alias": key,
            "drive_path": key,
            "drive_url": "u",
            "local_path": key,
        }
        for key in ("root-a", "root-b")
    }
    index.upsert(
        "file-a",
        "rev-1",
        [chunk],
        [[1.0, 0.0, 0.0]],
        roots,
        mime_type="text/plain",
    )
    index.delete_file("file-a")
    assert index.count_file("file-a") == 0


class FakeEncoding:
    ids = [1, 2, 3]


class FakeTokenizer:
    def encode(self, text):
        assert text == "count me"
        return FakeEncoding()


class FakeFastEmbedModel:
    tokenizer = FakeTokenizer()


class FakeTextEmbedding:
    registrations = []
    constructions = []

    @classmethod
    def list_supported_models(cls):
        return []

    @classmethod
    def add_custom_model(cls, **kwargs):
        cls.registrations.append(kwargs)

    def __init__(self, **kwargs):
        self.constructions.append(kwargs)
        self.model = FakeFastEmbedModel()

    def embed(self, texts):
        return ([float(len(text)), 1.0] for text in texts)


def test_e5_registers_mean_normalized_model_and_prefixes_inputs(
    tmp_path, monkeypatch
):
    FakeTextEmbedding.registrations.clear()
    FakeTextEmbedding.constructions.clear()
    monkeypatch.setattr(embed_module, "TextEmbedding", FakeTextEmbedding)

    embedder = FastEmbedE5(tmp_path / "state")

    assert FakeTextEmbedding.registrations == [
        {
            "model": "intfloat/multilingual-e5-small",
            "pooling": embed_module.PoolingType.MEAN,
            "normalization": True,
            "sources": embed_module.ModelSource(
                hf="intfloat/multilingual-e5-small"
            ),
            "dim": 384,
            "model_file": "onnx/model.onnx",
        }
    ]
    assert FakeTextEmbedding.constructions == [
        {
            "model_name": "intfloat/multilingual-e5-small",
            "cache_dir": str(tmp_path / "state" / "models"),
        }
    ]
    assert embedder.embed_passages(["hello"]) == [[14.0, 1.0]]
    assert embedder.embed_query("hello") == [12.0, 1.0]
    assert embedder.count("count me") == 3


def test_existing_e5_registration_is_reused(tmp_path, monkeypatch):
    class AlreadyRegistered(FakeTextEmbedding):
        @classmethod
        def list_supported_models(cls):
            return [{"model": "intfloat/multilingual-e5-small"}]

        @classmethod
        def add_custom_model(cls, **kwargs):
            raise AssertionError("duplicate registration")

    monkeypatch.setattr(embed_module, "TextEmbedding", AlreadyRegistered)
    FastEmbedE5(tmp_path / "state")


def test_model_identity_mismatch_requires_explicit_rebuild(tmp_path):
    path = tmp_path / "chroma"
    first = ChromaIndex(path, "test/old", 3)
    first.upsert(
        "file-a",
        "rev-1",
        [Chunk("file-a:1", "old", "page:1", {})],
        [[1.0, 0.0, 0.0]],
        {"root-a": _root("A")},
        mime_type="text/plain",
    )

    with pytest.raises(DriveRagError) as error:
        ChromaIndex(path, "test/new", 4)

    assert error.value.code == "INDEX_MODEL_MISMATCH"
    preserved = ChromaIndex(path, "test/old", 3)
    assert preserved.count_file("file-a") == 1
    assert preserved.collection.get(include=["metadatas"])["metadatas"][0][
        "mime_type"
    ] == "text/plain"
    rebuilt = ChromaIndex(path, "test/new", 4, rebuild=True)
    assert rebuilt.count_file("file-a") == 0
    assert rebuilt.model_identity == {
        "model_id": "test/new",
        "dimension": 4,
        "distance": "cosine",
        "schema_version": "1",
    }


def _root(alias, *, path=None):
    return {
        "alias": alias,
        "drive_path": path or f"{alias}/File.txt",
        "drive_url": f"https://drive.google.com/{alias}",
        "local_path": f"mirrors/{alias}/File.txt",
    }


def _committed_manifest(file_id, revision, chunk_ids, root_ids, *, failure=None):
    paths = tuple(
        RemotePath(root_id, (root_id,), (f"{file_id}.txt",))
        for root_id in root_ids
    )
    return Manifest(
        {
            file_id: ManifestFile(
                file_id,
                revision,
                None,
                None,
                paths,
                tuple(chunk_ids),
            )
        },
        json.dumps(
            {
                "model_id": "test/fake",
                "dimension": 3,
                "distance": "cosine",
                "schema_version": "1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "2026-07-18T10:00:00Z",
        failure,
        tuple(root_ids),
        "2026-07-18T09:59:00Z",
    )


def test_upsert_batches_retires_old_records_and_reopens_persistently(
    tmp_path, monkeypatch
):
    path = tmp_path / "chroma"
    index = ChromaIndex(path, "test/fake", 3)
    chunks = [
        Chunk(f"file-a:{number}", f"text {number}", f"page:{number}", {})
        for number in range(3)
    ]
    calls = []
    original_upsert = index.collection.upsert

    def recording_upsert(**kwargs):
        calls.append(len(kwargs["ids"]))
        return original_upsert(**kwargs)

    monkeypatch.setattr(index.client, "get_max_batch_size", lambda: 2)
    monkeypatch.setattr(index.collection, "upsert", recording_upsert)
    index.upsert(
        "file-a",
        "rev-1",
        chunks,
        [[1.0, 0.0, 0.0]] * 3,
        {"root-a": _root("A")},
        mime_type="text/plain",
    )
    assert calls == [2, 1]

    index.upsert(
        "file-a",
        "rev-2",
        [Chunk("file-a:new", "new", "page:1", {})],
        [[0.0, 1.0, 0.0]],
        {"root-a": _root("A")},
        mime_type="text/markdown",
    )

    reopened = ChromaIndex(path, "test/fake", 3)
    records = reopened.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas", "documents"]
    )
    assert records["documents"] == ["new"]
    assert records["metadatas"][0]["revision"] == "rev-2"


def test_invalid_upsert_leaves_previous_file_records_active(tmp_path):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    index.upsert(
        "file-a",
        "rev-1",
        [Chunk("file-a:old", "old", "page:1", {})],
        [[1.0, 0.0, 0.0]],
        {"root-a": _root("A")},
        mime_type="text/plain",
    )

    with pytest.raises(DriveRagError, match="dimension"):
        index.upsert(
            "file-a",
            "rev-2",
            [Chunk("file-a:new", "new", "page:2", {})],
            [[1.0, 0.0]],
            {"root-a": _root("A")},
            mime_type="text/plain",
        )

    records = index.collection.get(
        where={"drive_file_id": "file-a"}, include=["documents"]
    )
    assert records["documents"] == ["old"]


def test_delete_root_only_removes_that_ownership(tmp_path):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    index.upsert(
        "file-a",
        "rev-1",
        [Chunk("file-a:1", "policy", "page:1", {})],
        [[1.0, 0.0, 0.0]],
        {"root-a": _root("A"), "root-b": _root("B")},
        mime_type="application/pdf",
    )
    index.delete_root("root-a")
    records = index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )
    assert [item["root_id"] for item in records["metadatas"]] == ["root-b"]


def test_query_deduplicates_chunks_across_roots_and_is_bounded(tmp_path):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    chunks = [
        Chunk(f"file-a:{number}", "budget " * 300, f"page:{number}", {})
        for number in range(12)
    ]
    index.upsert(
        "file-a",
        "rev-1",
        chunks,
        FakeEmbedder().embed_passages([chunk.text for chunk in chunks]),
        {"root-a": _root("A"), "root-b": _root("B")},
        mime_type="text/plain",
    )

    evidence = QueryService(index, FakeEmbedder()).query(
        "budget", ("root-a", "root-b"), 99
    )

    assert len(evidence) == 8
    assert len({item.locator for item in evidence}) == 8
    assert all(len(item.excerpt) <= 1200 for item in evidence)
    assert sum(len(item.excerpt) for item in evidence) <= 8000


def test_query_returns_explicit_no_evidence_status(tmp_path):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    index.upsert(
        "file-a",
        "rev-1",
        [Chunk("file-a:1", "policy", "page:1", {})],
        [[0.0, 1.0, 0.0]],
        {"root-a": _root("A")},
        mime_type="text/plain",
    )

    with pytest.raises(DriveRagError) as error:
        QueryService(index, FakeEmbedder(), distance_threshold=0.01).query(
            "budget", ("root-a",), 8
        )
    assert error.value.code == "NO_RELEVANT_EVIDENCE"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"configured": False}, "CONFIGURATION_REQUIRED"),
        ({"index_stale": True}, "INDEX_STALE"),
    ],
)
def test_query_fails_closed_before_embedding_when_not_ready(tmp_path, kwargs, code):
    class ForbiddenEmbedder(FakeEmbedder):
        def embed_query(self, text):
            raise AssertionError("question was embedded before readiness check")

    service = QueryService(
        ChromaIndex(tmp_path / "chroma", "test/fake", 3),
        ForbiddenEmbedder(),
        **kwargs,
    )
    with pytest.raises(DriveRagError) as error:
        service.query("question")
    assert error.value.code == code


def test_failed_batched_write_restores_previous_file_records(tmp_path, monkeypatch):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    index.upsert(
        "file-a",
        "rev-1",
        [Chunk("file-a:old", "old", "page:1", {})],
        [[1.0, 0.0, 0.0]],
        {"root-a": _root("A")},
        mime_type="application/pdf",
    )
    original_upsert = index.collection.upsert
    attempts = 0

    def fail_second_new_batch(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("injected write failure")
        return original_upsert(**kwargs)

    monkeypatch.setattr(index.client, "get_max_batch_size", lambda: 1)
    monkeypatch.setattr(index.collection, "upsert", fail_second_new_batch)
    with pytest.raises(DriveRagError) as error:
        index.upsert(
            "file-a",
            "rev-2",
            [
                Chunk("file-a:new-1", "new 1", "page:1", {}),
                Chunk("file-a:new-2", "new 2", "page:2", {}),
            ],
            [[0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            {"root-a": _root("A")},
            mime_type="text/plain",
        )
    assert error.value.code == "INDEX_WRITE_FAILED"
    records = index.collection.get(
        where={"drive_file_id": "file-a"}, include=["documents", "metadatas"]
    )
    assert records["documents"] == ["old"]
    assert records["metadatas"][0]["revision"] == "rev-1"
    assert records["metadatas"][0]["mime_type"] == "application/pdf"


def test_cli_upserts_deletes_and_explicitly_rebuilds_index(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    payload = state / "staging" / "index.json"
    payload.parent.mkdir()
    payload.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "file_id": "file-a",
                "revision": "rev-1",
                "mime_type": "text/plain",
                "chunks": [
                    Chunk("file-a:1", "budget", "page:1", {}).to_dict()
                ],
                "embeddings": [[1.0] + [0.0] * 383],
                "roots": {"root-a": _root("Finance")},
            }
        ),
        encoding="utf-8",
    )

    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "index",
            "upsert",
            "--input",
            str(payload),
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == "index.upsert"
    assert result["status"] == "ok"
    persisted = ChromaIndex(
        state / "chroma", "intfloat/multilingual-e5-small", 384
    )
    assert persisted.count_file("file-a") == 1
    assert persisted.collection.get(include=["metadatas"])["metadatas"][0][
        "mime_type"
    ] == "text/plain"

    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "index",
            "delete-file",
            "--file-id",
            "file-a",
        ]
    ) == 0
    capsys.readouterr()
    assert ChromaIndex(
        state / "chroma", "intfloat/multilingual-e5-small", 384
    ).count_file("file-a") == 0

    assert drive_rag.main(
        ["--state-root", str(state), "index", "rebuild"]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == "index.rebuild"
    assert result["model_identity"]["dimension"] == 384


def test_cli_rejects_index_input_outside_state_root(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "index",
            "upsert",
            "--input",
            str(outside),
        ]
    ) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "UNSAFE_PATH"
    assert not (state / "chroma" / "chroma.sqlite3").exists()


def test_cli_query_reports_configuration_and_staleness_before_model_load(
    tmp_path, capsys, monkeypatch
):
    state = tmp_path / "state"
    state.mkdir()

    def forbidden_model(*args, **kwargs):
        raise AssertionError("embedding model loaded before readiness proof")

    monkeypatch.setattr(drive_rag, "FastEmbedE5", forbidden_model, raising=False)
    query_args = [
        "--state-root",
        str(state),
        "query",
        "--question",
        "What is the budget?",
    ]
    assert drive_rag.main(query_args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "CONFIGURATION_REQUIRED"

    registry = Registry.load(state)
    registry.add(
        FolderConfig(
            "root-a",
            "https://drive.google.com/drive/folders/root-a",
            "Finance",
        )
    )
    assert drive_rag.main(query_args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "INDEX_STALE"


@pytest.mark.parametrize(
    ("coverage", "coverage_reason", "expected_status"),
    (
        ("complete", None, "ok"),
        ("partial", "connector omitted completeness marker", "PARTIAL_INDEX"),
    ),
)
def test_cli_query_scopes_alias_and_emits_citation_with_coverage(
    tmp_path, capsys, monkeypatch, coverage, coverage_reason, expected_status
):
    state = tmp_path / "state"
    state.mkdir()
    registry = Registry.load(state)
    for root_id, alias in (("root-a", "Finance"), ("root-b", "People")):
        registry.add(
            FolderConfig(
                root_id,
                f"https://drive.google.com/drive/folders/{root_id}",
                alias,
            )
        )

    class FakeE5:
        model_id = "intfloat/multilingual-e5-small"
        dimension = 384

        def embed_query(self, text):
            return [1.0] + [0.0] * 383

    index = ChromaIndex(
        state / "chroma", "intfloat/multilingual-e5-small", 384
    )
    file_a_chunk = f"file-a:{'a' * 64}"
    file_b_chunk = f"file-b:{'b' * 64}"
    index.upsert(
        "file-a",
        "rev-7",
        [Chunk(file_a_chunk, "Budget is 30.", "sheet:Budget!A1:B2", {})],
        [[1.0] + [0.0] * 383],
        {"root-a": _root("Finance", path="Finance/Budget")},
        mime_type="application/vnd.google-apps.spreadsheet",
    )
    index.upsert(
        "file-b",
        "rev-2",
        [Chunk(file_b_chunk, "People budget is 99.", "page:2", {})],
        [[1.0] + [0.0] * 383],
        {"root-b": _root("People")},
        mime_type="application/pdf",
    )
    manifest = Manifest(
        {
            "file-a": ManifestFile(
                "file-a",
                "rev-7",
                None,
                None,
                (RemotePath("root-a", ("root-a",), ("Budget",)),),
                (file_a_chunk,),
            ),
            "file-b": ManifestFile(
                "file-b",
                "rev-2",
                None,
                None,
                (RemotePath("root-b", ("root-b",), ("File.txt",)),),
                (file_b_chunk,),
            ),
        },
        json.dumps(index.model_identity, sort_keys=True, separators=(",", ":")),
        "2026-07-18T10:00:00Z",
        None,
        ("root-a", "root-b"),
        "2026-07-18T09:59:00Z",
        coverage,
        coverage_reason,
    )
    atomic_write_json(
        state / "manifests" / "current.json",
        {"schema_version": "1", **manifest.to_dict()},
    )
    monkeypatch.setattr(drive_rag, "FastEmbedE5", lambda _state: FakeE5())

    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "query",
            "--question",
            "budget",
            "--folder-alias",
            "Finance",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == expected_status
    assert result["coverage"] == coverage
    assert result["coverage_reason"] == coverage_reason
    assert bool(result["warnings"]) is (coverage == "partial")
    assert len(result["evidence"]) == 1
    citation = result["evidence"][0]
    assert citation["file_id"] == "file-a"
    assert citation["file_name"] == "Budget"
    assert citation["folder_alias"] == "Finance"
    assert citation["locator"] == "sheet:Budget!A1:B2"
    assert citation["revision"] == "rev-7"
    assert citation["mime_type"] == "application/vnd.google-apps.spreadsheet"
    assert citation["drive_url"].startswith("https://drive.google.com/")


def test_corrupt_index_candidate_is_reported_as_stale():
    class CorruptIndex:
        def query(self, *args):
            return {
                "ids": [["not-json"]],
                "documents": [["body"]],
                "metadatas": [[{}]],
                "distances": [[0.0]],
            }

    with pytest.raises(DriveRagError) as error:
        QueryService(CorruptIndex(), FakeEmbedder()).query("question")
    assert error.value.code == "INDEX_STALE"
    assert "body" not in str(error.value)


def test_chunk_identity_cannot_overwrite_another_drive_file(tmp_path):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    index.upsert(
        "file-a",
        "rev-1",
        [Chunk("file-a:shared", "alpha", "page:1", {})],
        [[1.0, 0.0, 0.0]],
        {"root-a": _root("A")},
        mime_type="text/plain",
    )

    with pytest.raises(DriveRagError, match="chunk ID"):
        index.upsert(
            "file-b",
            "rev-1",
            [Chunk("file-a:shared", "bravo", "page:1", {})],
            [[0.0, 1.0, 0.0]],
            {"root-a": _root("A")},
            mime_type="text/plain",
        )

    assert index.count_file("file-a") == 1
    assert index.count_file("file-b") == 0


def test_untrusted_content_hash_cannot_be_stored_as_metadata(tmp_path):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    with pytest.raises(DriveRagError, match="content hash"):
        index.upsert(
            "file-a",
            "rev-1",
            [
                Chunk(
                    "file-a:1",
                    "document body",
                    "page:1",
                    {"content_hash": "SECRET_DOCUMENT_BODY"},
                )
            ],
            [[1.0, 0.0, 0.0]],
            {"root-a": _root("A")},
            mime_type="text/plain",
        )
    assert index.count_file("file-a") == 0


def test_cli_delete_marks_successful_manifest_stale_before_index_mutation(
    tmp_path, capsys, monkeypatch
):
    state = tmp_path / "state"
    state.mkdir()
    registry = Registry.load(state)
    registry.add(
        FolderConfig(
            "root-a",
            "https://drive.google.com/drive/folders/root-a",
            "Finance",
        )
    )
    index = ChromaIndex(
        state / "chroma", "intfloat/multilingual-e5-small", 384
    )
    manifest = Manifest(
        {},
        json.dumps(index.model_identity, sort_keys=True, separators=(",", ":")),
        "2026-07-18T10:00:00Z",
        None,
        ("root-a",),
        "2026-07-18T09:59:00Z",
    )
    manifest_path = state / "manifests" / "current.json"
    atomic_write_json(
        manifest_path,
        {"schema_version": "1", **manifest.to_dict()},
    )

    class FailingIndex:
        def delete_file(self, file_id):
            reloaded = load_manifest(manifest_path)
            assert reloaded.last_success is None
            raise DriveRagError("injected", code="INDEX_DELETE_FAILED")

    monkeypatch.setattr(drive_rag, "_open_index", lambda state_root: FailingIndex())
    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "index",
            "delete-file",
            "--file-id",
            "file-a",
        ]
    ) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INDEX_DELETE_FAILED"
    assert load_manifest(manifest_path).last_success is None


def test_manifest_consistency_survives_reopen_and_detects_partial_or_empty_index(
    tmp_path, monkeypatch
):
    path = tmp_path / "chroma"
    chunk_id = f"file-a:{'a' * 64}"
    index = ChromaIndex(path, "test/fake", 3)
    index.upsert(
        "file-a",
        "rev-1",
        [Chunk(chunk_id, "policy", "page:1", {})],
        [[1.0, 0.0, 0.0]],
        {"root-a": _root("A"), "root-b": _root("B")},
        mime_type="application/pdf",
    )
    manifest = _committed_manifest(
        "file-a", "rev-1", (chunk_id,), ("root-a", "root-b")
    )

    reopened = ChromaIndex(path, "test/fake", 3)
    batches = []
    original_get = reopened.collection.get

    def recording_get(**kwargs):
        batches.append(tuple(kwargs.get("ids") or ()))
        return original_get(**kwargs)

    monkeypatch.setattr(reopened.client, "get_max_batch_size", lambda: 1)
    monkeypatch.setattr(reopened.collection, "get", recording_get)
    reopened.assert_manifest_consistent(manifest)
    assert [len(batch) for batch in batches] == [1, 1]

    snapshot = original_get(include=["metadatas"])
    record_ids = snapshot["ids"]
    corrupted = dict(snapshot["metadatas"][0])
    corrupted["revision"] = "wrong-revision"
    reopened.collection.update(ids=[record_ids[0]], metadatas=[corrupted])
    with pytest.raises(DriveRagError) as wrong_metadata:
        reopened.assert_manifest_consistent(manifest)
    assert wrong_metadata.value.code == "INDEX_STALE"
    reopened.collection.update(
        ids=[record_ids[0]], metadatas=[snapshot["metadatas"][0]]
    )

    reopened.collection.delete(ids=[record_ids[0]])
    with pytest.raises(DriveRagError) as partial:
        reopened.assert_manifest_consistent(manifest)
    assert partial.value.code == "INDEX_STALE"

    rebuilt = ChromaIndex(path, "test/fake", 3, rebuild=True)
    with pytest.raises(DriveRagError) as empty:
        rebuilt.assert_manifest_consistent(manifest)
    assert empty.value.code == "INDEX_STALE"


def test_manifest_consistency_rejects_uncommitted_extra_records(tmp_path):
    chunk_a = f"file-a:{'a' * 64}"
    chunk_b = f"file-b:{'b' * 64}"
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    index.upsert(
        "file-a",
        "rev-1",
        [Chunk(chunk_a, "alpha", "page:1", {})],
        [[1.0, 0.0, 0.0]],
        {"root-a": _root("A")},
        mime_type="text/plain",
    )
    index.upsert(
        "file-b",
        "rev-1",
        [Chunk(chunk_b, "bravo", "page:1", {})],
        [[0.0, 1.0, 0.0]],
        {"root-a": _root("A")},
        mime_type="text/plain",
    )

    with pytest.raises(DriveRagError) as error:
        index.assert_manifest_consistent(
            _committed_manifest("file-a", "rev-1", (chunk_a,), ("root-a",))
        )
    assert error.value.code == "INDEX_STALE"


def test_mime_type_persists_and_is_returned_with_evidence(tmp_path):
    path = tmp_path / "chroma"
    chunk = Chunk("file-a:1", "Budget is 30.", "page:1", {})
    ChromaIndex(path, "test/fake", 3).upsert(
        "file-a",
        "rev-1",
        [chunk],
        FakeEmbedder().embed_passages([chunk.text]),
        {"root-a": _root("Finance")},
        mime_type="application/pdf",
    )

    reopened = ChromaIndex(path, "test/fake", 3)
    records = reopened.collection.get(include=["metadatas"])
    assert records["metadatas"][0]["mime_type"] == "application/pdf"
    evidence = QueryService(reopened, FakeEmbedder()).query(
        "budget", ("root-a",), 8
    )
    assert evidence[0].mime_type == "application/pdf"


def test_invalid_mime_type_is_rejected_before_index_mutation(tmp_path):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    with pytest.raises(DriveRagError, match="MIME type") as error:
        index.upsert(
            "file-a",
            "rev-1",
            [Chunk("file-a:1", "body", "page:1", {})],
            [[1.0, 0.0, 0.0]],
            {"root-a": _root("A")},
            mime_type="SECRET_DOCUMENT_BODY",
        )
    assert error.value.code == "INVALID_INDEX_INPUT"
    assert index.count_file("file-a") == 0


def test_query_rejects_corrupted_persisted_mime_without_citing_it(tmp_path):
    index = ChromaIndex(tmp_path / "chroma", "test/fake", 3)
    chunk = Chunk("file-a:1", "Budget is 30.", "page:1", {})
    index.upsert(
        "file-a",
        "rev-1",
        [chunk],
        FakeEmbedder().embed_passages([chunk.text]),
        {"root-a": _root("Finance")},
        mime_type="application/pdf",
    )
    snapshot = index.collection.get(include=["metadatas"])
    corrupted = dict(snapshot["metadatas"][0])
    corrupted["mime_type"] = "SECRET_DOCUMENT_BODY"
    index.collection.update(ids=snapshot["ids"], metadatas=[corrupted])

    with pytest.raises(DriveRagError) as error:
        QueryService(index, FakeEmbedder()).query("budget", ("root-a",), 8)
    assert error.value.code == "INDEX_STALE"
    assert "SECRET_DOCUMENT_BODY" not in str(error.value)


def test_cli_query_rejects_last_failure_before_loading_model(
    tmp_path, capsys, monkeypatch
):
    state = tmp_path / "state"
    state.mkdir()
    Registry.load(state).add(
        FolderConfig(
            "root-a",
            "https://drive.google.com/drive/folders/root-a",
            "Finance",
        )
    )
    identity = {
        "model_id": "intfloat/multilingual-e5-small",
        "dimension": 384,
        "distance": "cosine",
        "schema_version": "1",
    }
    manifest = Manifest(
        {},
        json.dumps(identity, sort_keys=True, separators=(",", ":")),
        "2026-07-18T10:00:00Z",
        "2026-07-18T10:01:00Z",
        ("root-a",),
        "2026-07-18T09:59:00Z",
    )
    atomic_write_json(
        state / "manifests" / "current.json",
        {"schema_version": "1", **manifest.to_dict()},
    )
    monkeypatch.setattr(
        drive_rag,
        "FastEmbedE5",
        lambda _state: (_ for _ in ()).throw(
            AssertionError("model loaded despite failed sync")
        ),
    )

    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "query",
            "--question",
            "budget",
        ]
    ) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "INDEX_STALE"


def test_cli_query_checks_committed_records_before_loading_model(
    tmp_path, capsys, monkeypatch
):
    state = tmp_path / "state"
    state.mkdir()
    Registry.load(state).add(
        FolderConfig(
            "root-a",
            "https://drive.google.com/drive/folders/root-a",
            "Finance",
        )
    )
    chunk_id = f"file-a:{'a' * 64}"
    manifest = _committed_manifest(
        "file-a", "rev-1", (chunk_id,), ("root-a",)
    )
    identity = {
        "model_id": "intfloat/multilingual-e5-small",
        "dimension": 384,
        "distance": "cosine",
        "schema_version": "1",
    }
    manifest = Manifest(
        manifest.files,
        json.dumps(identity, sort_keys=True, separators=(",", ":")),
        manifest.last_success,
        None,
        manifest.root_ids,
        manifest.last_inventory_generated_at,
    )
    atomic_write_json(
        state / "manifests" / "current.json",
        {"schema_version": "1", **manifest.to_dict()},
    )
    ChromaIndex(
        state / "chroma", "intfloat/multilingual-e5-small", 384
    )
    monkeypatch.setattr(
        drive_rag,
        "FastEmbedE5",
        lambda _state: (_ for _ in ()).throw(
            AssertionError("model loaded despite lost committed record")
        ),
    )

    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "query",
            "--question",
            "budget",
        ]
    ) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "INDEX_STALE"
