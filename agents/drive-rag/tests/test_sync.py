from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from drive_rag_lib.models import (
    Artifact,
    ArtifactSet,
    FolderConfig,
    Manifest,
    ManifestFile,
    RemoteInventory,
    RemotePath,
)
from drive_rag_lib.protocol import DriveRagError
from drive_rag_lib.index import COLLECTION_NAME
from drive_rag_lib.paths import ensure_state_root
from drive_rag_lib.query import QueryService
from drive_rag_lib.registry import Registry
from drive_rag_lib.sync import SyncEngine
from drive_rag_lib.sync import load_artifact_set
import drive_rag_lib.sync as sync_module
import drive_rag


def test_remote_deletion_removes_mirror_vectors_and_object(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    mirror = engine.state_root / "mirrors" / "Finance" / "Policy.pdf"
    committed = engine.manifest().files["file-a"]
    object_path = engine.object_path("file-a", committed.object_sha256)
    assert mirror.exists() and object_path.exists()
    assert engine.index.count_file("file-a") > 0

    result = engine.apply(sync_fixture.empty_inventory, sync_fixture.empty_artifacts)

    assert result.status == "SYNC_OK_CHANGED"
    assert not mirror.exists()
    assert not object_path.exists()
    assert engine.index.count_file("file-a") == 0
    assert "file-a" not in engine.manifest().files


def test_partial_inventory_commits_discovered_files_without_deleting_existing(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    inventory, artifacts = sync_fixture.changed(text="Partial update", revision="2")
    incomplete = replace(
        inventory,
        complete=False,
        incomplete_reason="connector omitted completeness marker",
    )

    result = engine.apply(incomplete, artifacts)

    assert result.status == "PARTIAL_INDEX"
    assert engine.index.count_file("file-a") > 0
    assert (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()
    manifest = engine.manifest()
    assert manifest.files["file-a"].revision == "2"
    assert manifest.coverage == "partial"
    assert manifest.coverage_reason == "connector omitted completeness marker"


def test_initial_partial_inventory_indexes_native_pdf_and_structured_content(sync_fixture):
    engine = sync_fixture.engine
    partial = replace(
        sync_fixture.first_inventory,
        complete=False,
        incomplete_reason="connector omitted completeness marker",
    )

    result = engine.apply(partial, sync_fixture.first_artifacts)

    assert result.status == "PARTIAL_INDEX"
    manifest = engine.manifest()
    assert manifest.coverage == "partial"
    assert manifest.files["file-a"].revision == "1"
    assert manifest.files["file-a"].active_chunk_ids
    assert engine.index.count_file("file-a") > 0
    assert (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()


def test_partial_empty_listing_preserves_committed_mirror_and_vectors(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    incomplete = replace(
        sync_fixture.empty_inventory,
        complete=False,
        incomplete_reason="listing may be truncated",
    )

    result = engine.apply(incomplete, sync_fixture.empty_artifacts)

    assert result.status == "PARTIAL_INDEX"
    assert "file-a" in engine.manifest().files
    assert engine.index.count_file("file-a") > 0
    assert (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()


def test_unchanged_partial_refresh_repairs_local_paths_after_state_relocation(
    sync_fixture, tmp_path
):
    from chromadb.api.shared_system_client import SharedSystemClient

    original = sync_fixture.engine
    original.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    moved_root = tmp_path / "relocated-state"
    shutil.move(str(original.state_root), moved_root)
    SharedSystemClient.clear_system_cache()
    moved = SyncEngine.open(moved_root, sync_fixture.embedder)
    inventory = replace(
        sync_fixture.first_inventory,
        run_id="run-relocated",
        complete=False,
        incomplete_reason="connector omitted completeness marker",
        generated_at="2026-07-18T10:01:00Z",
    )

    result = moved.apply(inventory, ArtifactSet(inventory.run_id, ()))

    assert result.status == "PARTIAL_INDEX"
    assert moved.manifest().last_success == inventory.generated_at
    assert not moved.has_pending_journal()
    records = moved.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )
    expected = str(moved_root / "mirrors" / "Finance" / "Policy.pdf")
    assert records["metadatas"]
    assert {item["local_path"] for item in records["metadatas"]} == {expected}


def test_corrupt_changed_artifact_keeps_previous_version(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    before = engine.manifest().files["file-a"]
    inventory, artifacts = sync_fixture.changed_with_corrupt_payload()

    with pytest.raises(DriveRagError, match="hash"):
        engine.apply(inventory, artifacts)

    after = engine.manifest()
    assert after.files["file-a"] == before
    assert after.last_failure is not None
    assert engine.index.count_file("file-a") > 0
    assert not engine.has_pending_journal()


def test_unsupported_file_is_mirrored_with_explicit_unindexed_reason(sync_fixture):
    engine = sync_fixture.engine
    payload = engine.state_root / "staging" / "run-unsupported" / "archive.bin"
    payload.parent.mkdir(parents=True, exist_ok=True)
    expected_payload = b"opaque unsupported bytes"
    payload.write_bytes(expected_payload)
    remote = replace(
        sync_fixture.source_file,
        file_id="file-unsupported",
        name="archive.bin",
        mime_type="application/octet-stream",
        revision="9",
        modified_time="2026-07-18T13:00:00Z",
        drive_url="https://drive.google.com/file/d/file-unsupported/view",
        size=payload.stat().st_size,
        paths=(RemotePath("root-a", ("root-a",), ("archive.bin",)),),
        native_kind=None,
    )
    inventory = RemoteInventory(
        "run-unsupported",
        True,
        ("root-a",),
        (remote,),
        None,
        "2026-07-18T13:00:00Z",
    )
    artifacts = ArtifactSet(
        inventory.run_id,
        (
            Artifact(
                remote.file_id,
                remote.revision,
                str(payload),
                hashlib.sha256(payload.read_bytes()).hexdigest(),
                None,
            ),
        ),
    )

    engine.apply(inventory, artifacts)

    committed = engine.manifest().files[remote.file_id]
    assert committed.index_status == "unindexed"
    assert committed.index_reason == "UNSUPPORTED_FORMAT"
    assert committed.active_chunk_ids == ()
    assert (
        engine.state_root / "mirrors" / "Finance" / "archive.bin"
    ).read_bytes() == expected_payload
    assert engine.index.count_file(remote.file_id) == 0
    status = engine.status().to_dict()
    assert status["indexed_files"] == 0
    assert status["unindexed_files"] == 1
    assert status["unindexed_reasons"] == {"UNSUPPORTED_FORMAT": 1}


def test_extraction_limited_file_is_mirrored_and_recorded_unindexed(
    sync_fixture, monkeypatch
):
    engine = sync_fixture.engine
    payload = engine.state_root / "staging" / "run-limited" / "large.pdf"
    payload.parent.mkdir(parents=True, exist_ok=True)
    expected_payload = b"verified oversized PDF bytes"
    payload.write_bytes(expected_payload)
    remote = replace(
        sync_fixture.source_file,
        file_id="file-limited",
        name="large.pdf",
        mime_type="application/pdf",
        revision="10",
        modified_time="2026-07-18T13:01:00Z",
        drive_url="https://drive.google.com/file/d/file-limited/view",
        size=payload.stat().st_size,
        paths=(RemotePath("root-a", ("root-a",), ("large.pdf",)),),
        native_kind=None,
    )
    inventory = RemoteInventory(
        "run-limited",
        True,
        ("root-a",),
        (remote,),
        None,
        "2026-07-18T13:01:00Z",
    )
    artifacts = ArtifactSet(
        inventory.run_id,
        (
            Artifact(
                remote.file_id,
                remote.revision,
                str(payload),
                hashlib.sha256(payload.read_bytes()).hexdigest(),
                None,
            ),
        ),
    )

    def extraction_limit(*_args, **_kwargs):
        raise DriveRagError("resource limit", code="EXTRACTION_LIMIT_EXCEEDED")

    monkeypatch.setattr(sync_module, "extract_file", extraction_limit)

    engine.apply(inventory, artifacts)

    committed = engine.manifest().files[remote.file_id]
    assert committed.index_status == "unindexed"
    assert committed.index_reason == "EXTRACTION_LIMIT_EXCEEDED"
    assert committed.active_chunk_ids == ()
    assert (engine.state_root / "mirrors" / "Finance" / "large.pdf").read_bytes() == expected_payload
    assert engine.index.count_file(remote.file_id) == 0
    assert engine.status().to_dict()["unindexed_reasons"] == {
        "EXTRACTION_LIMIT_EXCEEDED": 1
    }


def test_supported_empty_document_is_explicitly_indexed_not_unsupported(sync_fixture):
    structured = Path(sync_fixture.first_artifacts.artifacts[0].structured_path)
    structured.write_text(
        json.dumps({"kind": "document", "sections": []}), encoding="utf-8"
    )

    sync_fixture.engine.apply(
        sync_fixture.first_inventory, sync_fixture.first_artifacts
    )

    committed = sync_fixture.engine.manifest().files["file-a"]
    assert committed.index_status == "indexed"
    assert committed.index_reason is None
    assert committed.active_chunk_ids == ()
    status = sync_fixture.engine.status().to_dict()
    assert status["indexed_files"] == 1
    assert status["unindexed_files"] == 0


def test_unindexed_status_survives_alias_move_recovery_and_deletion(sync_fixture):
    engine = sync_fixture.engine
    payload = engine.state_root / "staging" / "run-binary" / "archive.bin"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b"opaque unsupported bytes")
    remote = replace(
        sync_fixture.source_file,
        file_id="file-binary",
        name="archive.bin",
        mime_type="application/octet-stream",
        revision="1",
        drive_url="https://drive.google.com/file/d/file-binary/view",
        size=payload.stat().st_size,
        paths=(RemotePath("root-a", ("root-a",), ("archive.bin",)),),
        native_kind=None,
    )
    first = RemoteInventory(
        "run-binary", True, ("root-a",), (remote,), None, "2026-07-18T13:00:00Z"
    )
    artifacts = ArtifactSet(
        first.run_id,
        (
            Artifact(
                remote.file_id,
                remote.revision,
                str(payload),
                hashlib.sha256(payload.read_bytes()).hexdigest(),
                None,
            ),
        ),
    )
    engine.apply(first, artifacts)
    Registry.load(engine.state_root).rename("root-a", "Archive")
    moved = replace(
        first, run_id="run-binary-move", generated_at="2026-07-18T14:00:00Z"
    )

    def interrupt(phase):
        if phase == "promoted":
            raise RuntimeError("interrupt moved unsupported file")

    recovering = SyncEngine.open(
        engine.state_root, engine.embedder, phase_callback=interrupt
    )
    with pytest.raises(RuntimeError, match="interrupt moved"):
        recovering.apply(moved, ArtifactSet(moved.run_id, ()))
    recovering.phase_callback = None
    recovering.recover()
    committed = recovering.manifest().files[remote.file_id]
    assert committed.index_status == "unindexed"
    assert committed.index_reason == "UNSUPPORTED_FORMAT"
    assert (engine.state_root / "mirrors" / "Archive" / "archive.bin").is_file()

    empty = RemoteInventory(
        "run-binary-empty", True, ("root-a",), (), None, "2026-07-18T15:00:00Z"
    )
    recovering.apply(empty, ArtifactSet(empty.run_id, ()))
    assert remote.file_id not in recovering.manifest().files
    assert not (engine.state_root / "mirrors" / "Archive" / "archive.bin").exists()


@pytest.mark.parametrize(
    "phase",
    [
        "planned",
        "verified",
        "indexed",
        "promoted",
        "deleted",
        "activating",
        "committed",
    ],
)
def test_recovery_is_idempotent_after_every_phase(sync_fixture, phase):
    def inject(observed):
        if observed == phase:
            raise RuntimeError(f"injected-{phase}")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=inject,
    )
    with pytest.raises(RuntimeError, match=f"injected-{phase}"):
        engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)

    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()

    assert recovered.status == "SYNC_OK_CHANGED"
    assert recovered.engine.index.count_file("file-a") == 1
    assert (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()
    assert recovered.engine.manifest().files["file-a"].revision == "1"
    assert not recovered.engine.has_pending_journal()
    assert recovered.engine.recover().status == "SYNC_OK_NO_CHANGES"


@pytest.mark.parametrize("phase", ["indexed", "promoted", "deleted", "activating"])
def test_changed_file_recovers_after_every_post_index_phase(sync_fixture, phase):
    sync_fixture.engine.apply(
        sync_fixture.first_inventory, sync_fixture.first_artifacts
    )
    inventory, artifacts = sync_fixture.changed(text="Budget retention", revision="2")
    def inject(observed):
        if observed == phase:
            raise RuntimeError(f"injected-{phase}")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=inject,
    )
    with pytest.raises(RuntimeError):
        engine.apply(inventory, artifacts)
    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()
    assert recovered.engine.manifest().files["file-a"].revision == "2"
    assert recovered.engine.index.count_file("file-a") == 1


def test_move_updates_paths_without_reembedding(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    calls = sync_fixture.embedder.passage_calls
    moved_file = replace(
        sync_fixture.source_file,
        paths=(RemotePath("root-a", ("root-a", "folder-b"), ("Policies", "Policy")),),
    )
    moved = RemoteInventory(
        "run-move",
        True,
        ("root-a",),
        (moved_file,),
        None,
        "2026-07-18T11:00:00Z",
    )

    engine.apply(moved, ArtifactSet("run-move", ()))

    assert sync_fixture.embedder.passage_calls == calls
    assert not (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()
    assert (
        engine.state_root / "mirrors" / "Finance" / "Policies" / "Policy.pdf"
    ).exists()
    records = engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )
    assert records["metadatas"][0]["drive_path"] == "Policies/Policy"


def test_disabling_overlapping_root_keeps_shared_object_and_other_root(sync_fixture):
    state = sync_fixture.engine.state_root
    Registry.load(state).add(
        FolderConfig(
            "root-b",
            "https://drive.google.com/drive/folders/root-b",
            "Legal",
            True,
        )
    )
    both_file = replace(
        sync_fixture.source_file,
        paths=(
            RemotePath("root-a", ("root-a",), ("Policy",)),
            RemotePath("root-b", ("root-b",), ("Policy",)),
        ),
    )
    both_inventory = replace(
        sync_fixture.first_inventory,
        root_ids=("root-a", "root-b"),
        files=(both_file,),
    )
    sync_fixture.engine.apply(both_inventory, sync_fixture.first_artifacts)
    committed = sync_fixture.engine.manifest().files["file-a"]
    object_path = sync_fixture.engine.object_path("file-a", committed.object_sha256)
    Registry.load(state).set_enabled("root-a", False)
    only_b_file = replace(
        both_file,
        paths=(RemotePath("root-b", ("root-b",), ("Policy",)),),
    )
    only_b = RemoteInventory(
        "run-only-b",
        True,
        ("root-b",),
        (only_b_file,),
        None,
        "2026-07-18T11:00:00Z",
    )

    sync_fixture.engine.apply(only_b, ArtifactSet("run-only-b", ()))

    assert not (state / "mirrors" / "Finance" / "Policy.pdf").exists()
    assert (state / "mirrors" / "Legal" / "Policy.pdf").exists()
    assert object_path.exists()
    records = sync_fixture.engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )
    assert [item["root_id"] for item in records["metadatas"]] == ["root-b"]


def test_duplicate_overlapping_inventory_entries_merge_paths_and_survive_alias_repath(
    sync_fixture
):
    engine = sync_fixture.engine
    Registry.load(engine.state_root).add(
        FolderConfig(
            "root-b",
            "https://drive.google.com/drive/folders/root-b",
            "Legal",
            True,
        )
    )
    root_a = replace(
        sync_fixture.source_file,
        paths=(RemotePath("root-a", ("root-a",), ("Policy",)),),
    )
    root_b = replace(
        sync_fixture.source_file,
        paths=(RemotePath("root-b", ("root-b",), ("Policy",)),),
    )
    inventory = replace(
        sync_fixture.first_inventory,
        root_ids=("root-a", "root-b"),
        files=(root_a, root_b),
    )

    engine.apply(inventory, sync_fixture.first_artifacts)

    calls = sync_fixture.embedder.passage_calls
    assert (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()
    assert (engine.state_root / "mirrors" / "Legal" / "Policy.pdf").exists()
    Registry.load(engine.state_root).rename("root-b", "Compliance")
    renamed = replace(
        inventory,
        run_id="run-overlap-alias",
        generated_at="2026-07-18T13:00:00Z",
    )

    engine.apply(renamed, ArtifactSet(renamed.run_id, ()))

    assert sync_fixture.embedder.passage_calls == calls
    assert not (engine.state_root / "mirrors" / "Legal" / "Policy.pdf").exists()
    assert (
        engine.state_root / "mirrors" / "Compliance" / "Policy.pdf"
    ).exists()
    metadatas = engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )["metadatas"]
    assert {item["folder_alias"] for item in metadatas} == {
        "Finance",
        "Compliance",
    }
    assert {item["drive_path"] for item in metadatas} == {"Policy"}


def test_unchanged_file_can_gain_overlapping_root_without_reembedding(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    calls = sync_fixture.embedder.passage_calls
    Registry.load(engine.state_root).add(
        FolderConfig(
            "root-b",
            "https://drive.google.com/drive/folders/root-b",
            "Legal",
            True,
        )
    )
    shared = replace(
        sync_fixture.source_file,
        paths=(
            RemotePath("root-a", ("root-a",), ("Policy",)),
            RemotePath("root-b", ("root-b",), ("Policy",)),
        ),
    )
    inventory = replace(
        sync_fixture.first_inventory,
        run_id="run-add-root",
        root_ids=("root-a", "root-b"),
        files=(shared,),
        generated_at="2026-07-18T13:00:00Z",
    )

    engine.apply(inventory, ArtifactSet(inventory.run_id, ()))

    assert sync_fixture.embedder.passage_calls == calls
    assert (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()
    assert (engine.state_root / "mirrors" / "Legal" / "Policy.pdf").exists()
    records = engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )
    assert {item["root_id"] for item in records["metadatas"]} == {
        "root-a",
        "root-b",
    }


def test_artifact_set_rejects_extras_identity_escape_and_symlinks(sync_fixture, tmp_path):
    inventory = sync_fixture.first_inventory
    good = sync_fixture.first_artifacts.artifacts[0]
    extra = replace(good, file_id="extra")
    with pytest.raises(DriveRagError, match="artifact"):
        sync_fixture.engine.apply(inventory, ArtifactSet("run-1", (good, extra)))

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(Path(good.payload_path).read_bytes())
    escaped = replace(good, payload_path=str(outside))
    with pytest.raises(DriveRagError, match="staging"):
        sync_fixture.engine.apply(inventory, ArtifactSet("run-1", (escaped,)))

    link = Path(good.payload_path).with_name("link.pdf")
    link.symlink_to(Path(good.payload_path))
    symlinked = replace(good, payload_path=str(link))
    with pytest.raises(DriveRagError, match="symlink"):
        sync_fixture.engine.apply(inventory, ArtifactSet("run-1", (symlinked,)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("file_id", 42),
        ("revision", None),
        ("payload_path", object()),
        ("payload_sha256", 42),
        ("structured_path", 42),
    ],
)
def test_direct_malformed_artifact_inputs_are_typed(sync_fixture, field, value):
    artifact = replace(sync_fixture.first_artifacts.artifacts[0], **{field: value})
    with pytest.raises(DriveRagError, match="artifact"):
        sync_fixture.engine.apply(
            sync_fixture.first_inventory,
            ArtifactSet("run-1", (artifact,)),
        )


def test_artifact_paths_must_be_absolute_and_unique(sync_fixture):
    good = sync_fixture.first_artifacts.artifacts[0]
    relative = replace(
        good,
        payload_path=Path(good.payload_path).name,
        structured_path=Path(good.structured_path).name,
    )
    with pytest.raises(DriveRagError, match="absolute"):
        sync_fixture.engine.apply(
            sync_fixture.first_inventory, ArtifactSet("run-1", (relative,))
        )
    assert not sync_fixture.engine.has_pending_journal()

    second_file = replace(
        sync_fixture.source_file,
        file_id="file-b",
        name="Policy B",
        drive_url="https://docs.google.com/document/d/file-b/edit",
        paths=(RemotePath("root-a", ("root-a",), ("Policy B",)),),
    )
    two_files = replace(
        sync_fixture.first_inventory,
        files=(sync_fixture.source_file, second_file),
    )
    shared = replace(good, file_id="file-b")
    with pytest.raises(DriveRagError, match="unique"):
        sync_fixture.engine.apply(
            two_files, ArtifactSet("run-1", (good, shared))
        )
    assert not sync_fixture.engine.has_pending_journal()


@pytest.mark.parametrize("phase", ["planned", "verified", "indexed", "promoted", "deleted"])
@pytest.mark.parametrize("tamper", ["outside", "symlink"])
def test_tampered_artifact_path_is_rejected_in_every_recovery_phase(
    sync_fixture, phase, tamper
):
    artifact = sync_fixture.first_artifacts.artifacts[0]
    source = Path(artifact.payload_path)
    original_bytes = source.read_bytes()
    outside = sync_fixture.engine.state_root.parent / f"outside-{phase}-{tamper}.pdf"
    outside.write_bytes(original_bytes)

    def inject(observed):
        if observed == phase:
            raise RuntimeError(f"injected-{phase}")

    interrupted = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=inject,
    )
    with pytest.raises(RuntimeError, match=f"injected-{phase}"):
        interrupted.apply(
            sync_fixture.first_inventory, sync_fixture.first_artifacts
        )

    pending = interrupted.state_root / "journal" / "pending.json"
    payload = json.loads(pending.read_text(encoding="utf-8"))
    if tamper == "outside":
        payload["journal"]["artifacts"]["artifacts"][0]["payload_path"] = str(
            outside
        )
        pending.write_text(json.dumps(payload), encoding="utf-8")
    else:
        source.unlink(missing_ok=True)
        source.symlink_to(outside)

    with pytest.raises(DriveRagError, match="artifact|staging|symlink|journal"):
        SyncEngine.open(interrupted.state_root, interrupted.embedder).recover()

    assert outside.exists()
    assert outside.read_bytes() == original_bytes


def test_connector_artifacts_cannot_occupy_reserved_prepared_namespace(sync_fixture):
    engine = sync_fixture.engine
    payload = engine._prepared_path("run-1", "file-a")
    payload.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload.write_text("retention policy", encoding="utf-8")
    remote = replace(
        sync_fixture.source_file,
        name="Policy.txt",
        mime_type="text/plain",
        drive_url="https://drive.google.com/file/d/file-a/view",
        size=payload.stat().st_size,
        paths=(RemotePath("root-a", ("root-a",), ("Policy.txt",)),),
        native_kind=None,
    )
    inventory = replace(sync_fixture.first_inventory, files=(remote,))
    artifact = Artifact(
        "file-a",
        "1",
        str(payload),
        hashlib.sha256(payload.read_bytes()).hexdigest(),
        None,
    )

    with pytest.raises(DriveRagError, match="prepared|reserved"):
        engine.apply(inventory, ArtifactSet("run-1", (artifact,)))

    assert not engine.has_pending_journal()
    assert engine.manifest().files == {}


@pytest.mark.parametrize(
    "phase", ["verified", "indexed", "promoted", "deleted", "committed"]
)
@pytest.mark.parametrize("tamper", ["same-hash", "updated-hash", "symlink"])
def test_prepared_reference_must_remain_canonical_in_every_later_phase(
    sync_fixture, phase, tamper
):
    def inject(observed):
        if observed == phase:
            raise RuntimeError(f"injected-{phase}")

    interrupted = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=inject,
    )
    with pytest.raises(RuntimeError, match=f"injected-{phase}"):
        interrupted.apply(
            sync_fixture.first_inventory, sync_fixture.first_artifacts
        )

    pending = interrupted.state_root / "journal" / "pending.json"
    payload = json.loads(pending.read_text(encoding="utf-8"))
    reference = payload["journal"]["prepared"][0]
    canonical = Path(reference["path"])
    canonical_sha = hashlib.sha256(canonical.read_bytes()).hexdigest()
    alternate = canonical.parent.parent / f"connector-{tamper}.json"
    if tamper == "same-hash":
        alternate.write_bytes(canonical.read_bytes())
    elif tamper == "updated-hash":
        changed = json.loads(canonical.read_text(encoding="utf-8"))
        changed["chunks"][0]["text"] = "connector substituted content"
        alternate.write_text(json.dumps(changed), encoding="utf-8")
    else:
        alternate.symlink_to(canonical)
    reference["path"] = str(alternate)
    reference["sha256"] = hashlib.sha256(canonical.read_bytes()).hexdigest()
    if tamper == "updated-hash":
        reference["sha256"] = hashlib.sha256(alternate.read_bytes()).hexdigest()
    pending.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DriveRagError, match="prepared|canonical|journal|symlink"):
        SyncEngine.open(interrupted.state_root, interrupted.embedder).recover()

    reference["path"] = str(canonical)
    reference["sha256"] = canonical_sha
    pending.write_text(json.dumps(payload), encoding="utf-8")
    recovered = SyncEngine.open(interrupted.state_root, interrupted.embedder).recover()
    assert recovered.engine.manifest().files["file-a"].revision == "1"
    assert recovered.engine.index.count_file("file-a") == 1


def test_artifact_models_are_strict_and_immutable():
    artifact = Artifact("file-a", "1", "/tmp/file", "a" * 64, None)
    assert Artifact.from_dict(artifact.to_dict()) == artifact
    with pytest.raises((AttributeError, TypeError)):
        artifact.file_id = "other"
    with pytest.raises(DriveRagError):
        Artifact.from_dict({**artifact.to_dict(), "extra": True})
    artifacts = ArtifactSet("run-1", (artifact,))
    assert ArtifactSet.from_dict(artifacts.to_dict()) == artifacts


def test_missing_staged_input_blocks_recovery_without_guessing(sync_fixture):
    def inject(phase):
        if phase == "planned":
            raise RuntimeError("injected")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=inject,
    )
    with pytest.raises(RuntimeError):
        engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    Path(sync_fixture.first_artifacts.artifacts[0].payload_path).unlink()

    with pytest.raises(DriveRagError, match="artifact"):
        SyncEngine.open(engine.state_root, engine.embedder).recover()

    assert SyncEngine.open(engine.state_root, engine.embedder).manifest().files == {}


def test_interrupted_changed_sync_makes_query_explicitly_stale(sync_fixture):
    sync_fixture.engine.apply(
        sync_fixture.first_inventory, sync_fixture.first_artifacts
    )
    inventory, artifacts = sync_fixture.changed(revision="2")

    def inject(phase):
        if phase == "promoted":
            raise RuntimeError("injected")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=inject,
    )
    with pytest.raises(RuntimeError):
        engine.apply(inventory, artifacts)

    with pytest.raises(DriveRagError, match="manifest|index|synchron",):
        engine.index.assert_manifest_consistent(engine.manifest())
    with pytest.raises(DriveRagError, match="pending"):
        engine.assert_query_ready()


def test_promotion_failure_preserves_committed_and_staged_index_until_recovery(
    sync_fixture
):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    before = engine.manifest().files["file-a"]
    mirror = engine.state_root / "mirrors" / "Finance" / "Policy.pdf"
    old_mirror = mirror.read_bytes()
    inventory, artifacts = sync_fixture.changed(revision="2")
    payload = Path(artifacts.artifacts[0].payload_path)
    valid_payload = payload.read_bytes()

    def corrupt_after_index(phase):
        if phase == "indexed":
            payload.write_bytes(b"corrupt-after-index")

    failing = SyncEngine.open(
        engine.state_root,
        engine.embedder,
        phase_callback=corrupt_after_index,
    )
    with pytest.raises(DriveRagError, match="hash"):
        failing.apply(inventory, artifacts)

    assert failing.manifest().files["file-a"] == before
    assert mirror.read_bytes() == old_mirror
    records = failing.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )
    assert {item["revision"] for item in records["metadatas"]} == {"1", "2"}
    with pytest.raises(DriveRagError, match="pending"):
        failing.assert_query_ready()

    payload.write_bytes(valid_payload)
    recovered = SyncEngine.open(failing.state_root, failing.embedder).recover()
    assert recovered.engine.manifest().files["file-a"].revision == "2"
    final = recovered.engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )
    assert {item["revision"] for item in final["metadatas"]} == {"2"}
    assert len(final["ids"]) == 1


def _snapshot_index(collection):
    records = collection.get(include=["documents", "metadatas", "embeddings"])
    return {
        "ids": list(records["ids"]),
        "documents": list(records["documents"]),
        "metadatas": [dict(item) for item in records["metadatas"]],
        "embeddings": [
            [float(value) for value in embedding]
            for embedding in records["embeddings"]
        ],
    }


def _replace_index_with_snapshot(collection, snapshot):
    current = collection.get(include=[])["ids"]
    if current:
        collection.delete(ids=current)
    if snapshot["ids"]:
        collection.upsert(**snapshot)


def test_index_snapshot_restore_honors_chroma_batch_limit(
    sync_fixture, monkeypatch
):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    original = _snapshot_index(engine.index.collection)
    expanded = {key: [] for key in original}
    for character in ("a", "b", "c"):
        expanded["ids"].append(
            json.dumps(
                [f"file-a:{character * 64}", "root-a"],
                separators=(",", ":"),
            )
        )
        expanded["documents"].append(original["documents"][0])
        expanded["metadatas"].append(dict(original["metadatas"][0]))
        expanded["embeddings"].append(list(original["embeddings"][0]))

    monkeypatch.setattr(engine.index.client, "get_max_batch_size", lambda: 1)
    original_delete = engine.index.collection.delete
    original_upsert = engine.index.collection.upsert

    def bounded_delete(*args, **kwargs):
        assert len(kwargs.get("ids", ())) <= 1
        return original_delete(*args, **kwargs)

    def bounded_upsert(*args, **kwargs):
        assert len(kwargs["ids"]) <= 1
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(engine.index.collection, "delete", bounded_delete)
    monkeypatch.setattr(engine.index.collection, "upsert", bounded_upsert)

    engine.index.restore_records(expanded)

    assert set(engine.index.collection.get(include=[])["ids"]) == set(
        expanded["ids"]
    )


@pytest.mark.parametrize("phase", ["promoted", "deleted"])
@pytest.mark.parametrize(
    "tamper",
    [
        "revision",
        "drive_file_id",
        "root_id",
        "mime_type",
        "drive_path",
        "drive_url",
        "local_path",
        "content_hash",
        "locator",
        "chunk_id",
        "extra",
        "missing",
    ],
)
def test_target_index_tampering_never_commits_and_preserves_old_revision(
    sync_fixture, phase, tamper
):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    inventory, artifacts = sync_fixture.changed(revision="2")

    def inject(observed):
        if observed == phase:
            raise RuntimeError(f"injected-{phase}")

    interrupted = SyncEngine.open(
        engine.state_root,
        engine.embedder,
        phase_callback=inject,
    )
    with pytest.raises(RuntimeError, match=f"injected-{phase}"):
        interrupted.apply(inventory, artifacts)

    collection = interrupted.index.collection
    snapshot = _snapshot_index(collection)
    target_index = next(
        index
        for index, metadata in enumerate(snapshot["metadatas"])
        if metadata["revision"] == "2"
    )
    record_id = snapshot["ids"][target_index]
    metadata = dict(snapshot["metadatas"][target_index])
    document = snapshot["documents"][target_index]
    embedding = snapshot["embeddings"][target_index]
    if tamper in {
        "revision",
        "drive_file_id",
        "root_id",
        "drive_path",
        "drive_url",
        "local_path",
        "locator",
    }:
        metadata[tamper] = f"evil-{tamper}"
        collection.upsert(
            ids=[record_id],
            documents=[document],
            metadatas=[metadata],
            embeddings=[embedding],
        )
    elif tamper == "mime_type":
        metadata["mime_type"] = "text/plain"
        collection.upsert(
            ids=[record_id],
            documents=[document],
            metadatas=[metadata],
            embeddings=[embedding],
        )
    elif tamper == "content_hash":
        metadata["content_hash"] = "f" * 64
        collection.upsert(
            ids=[record_id],
            documents=[document],
            metadatas=[metadata],
            embeddings=[embedding],
        )
    elif tamper == "chunk_id":
        collection.delete(ids=[record_id])
        evil_id = json.dumps([f"file-a:{'e' * 64}", "root-a"], separators=(",", ":"))
        collection.upsert(
            ids=[evil_id],
            documents=[document],
            metadatas=[metadata],
            embeddings=[embedding],
        )
    elif tamper == "extra":
        extra_id = json.dumps([f"file-a:{'f' * 64}", "root-a"], separators=(",", ":"))
        collection.upsert(
            ids=[extra_id],
            documents=[document],
            metadatas=[metadata],
            embeddings=[embedding],
        )
    else:
        collection.delete(ids=[record_id])

    with pytest.raises(DriveRagError, match="index|record|metadata|staged"):
        SyncEngine.open(interrupted.state_root, interrupted.embedder).recover()

    failed = SyncEngine.open(interrupted.state_root, interrupted.embedder)
    assert failed.manifest().files["file-a"].revision == "1"
    remaining = failed.index.collection.get(include=["metadatas"])["metadatas"]
    assert any(item.get("revision") == "1" for item in remaining)

    _replace_index_with_snapshot(failed.index.collection, snapshot)
    recovered = SyncEngine.open(failed.state_root, failed.embedder).recover()
    assert recovered.engine.manifest().files["file-a"].revision == "2"
    final = recovered.engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )
    assert len(final["ids"]) == 1
    assert {item["revision"] for item in final["metadatas"]} == {"2"}


class _HardInterruption(BaseException):
    pass


def test_hard_exit_after_changed_file_activation_recovers(sync_fixture, monkeypatch):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    inventory, artifacts = sync_fixture.changed(revision="2")
    interrupted = SyncEngine.open(engine.state_root, engine.embedder)
    original = interrupted.index.retire_file_except

    def retire_then_exit(*args, **kwargs):
        original(*args, **kwargs)
        raise _HardInterruption("after changed activation")

    monkeypatch.setattr(interrupted.index, "retire_file_except", retire_then_exit)
    with pytest.raises(_HardInterruption):
        interrupted.apply(inventory, artifacts)
    with pytest.raises(DriveRagError, match="pending"):
        interrupted.assert_query_ready()

    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()
    assert recovered.engine.manifest().files["file-a"].revision == "2"
    assert recovered.engine.index.count_file("file-a") == 1


def test_hard_exit_after_remote_deletion_activation_recovers(
    sync_fixture, monkeypatch
):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    interrupted = SyncEngine.open(engine.state_root, engine.embedder)
    original = interrupted.index.delete_file

    def delete_then_exit(*args, **kwargs):
        original(*args, **kwargs)
        raise _HardInterruption("after deletion activation")

    monkeypatch.setattr(interrupted.index, "delete_file", delete_then_exit)
    with pytest.raises(_HardInterruption):
        interrupted.apply(sync_fixture.empty_inventory, sync_fixture.empty_artifacts)

    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()
    assert recovered.engine.manifest().files == {}
    assert recovered.engine.index.count_file("file-a") == 0


@pytest.mark.parametrize("transition", ["add", "remove-overlap"])
def test_hard_exit_after_root_repath_activation_recovers(
    sync_fixture, monkeypatch, transition
):
    engine = sync_fixture.engine
    Registry.load(engine.state_root).add(
        FolderConfig(
            "root-b",
            "https://drive.google.com/drive/folders/root-b",
            "Legal",
            True,
        )
    )
    both = replace(
        sync_fixture.source_file,
        paths=(
            RemotePath("root-a", ("root-a",), ("Policy",)),
            RemotePath("root-b", ("root-b",), ("Policy",)),
        ),
    )
    if transition == "add":
        Registry.load(engine.state_root).set_enabled("root-b", False)
        engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
        Registry.load(engine.state_root).set_enabled("root-b", True)
        inventory = replace(
            sync_fixture.first_inventory,
            run_id="run-hard-add-root",
            root_ids=("root-a", "root-b"),
            files=(both,),
            generated_at="2026-07-18T13:00:00Z",
        )
    else:
        first = replace(
            sync_fixture.first_inventory,
            root_ids=("root-a", "root-b"),
            files=(both,),
        )
        engine.apply(first, sync_fixture.first_artifacts)
        Registry.load(engine.state_root).set_enabled("root-a", False)
        only_b = replace(
            both,
            paths=(RemotePath("root-b", ("root-b",), ("Policy",)),),
        )
        inventory = RemoteInventory(
            "run-hard-remove-root",
            True,
            ("root-b",),
            (only_b,),
            None,
            "2026-07-18T13:00:00Z",
        )
    interrupted = SyncEngine.open(engine.state_root, engine.embedder)
    original = interrupted.index.repath_file

    def repath_then_exit(*args, **kwargs):
        original(*args, **kwargs)
        raise _HardInterruption("after repath activation")

    monkeypatch.setattr(interrupted.index, "repath_file", repath_then_exit)
    with pytest.raises(_HardInterruption):
        interrupted.apply(inventory, ArtifactSet(inventory.run_id, ()))

    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()
    expected_roots = {"root-a", "root-b"} if transition == "add" else {"root-b"}
    records = recovered.engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )
    assert {item["root_id"] for item in records["metadatas"]} == expected_roots


def test_hard_exit_at_partial_multibatch_activation_recovers(
    sync_fixture, monkeypatch
):
    engine = sync_fixture.engine
    registry = Registry.load(engine.state_root)
    for root_id, alias in (("root-b", "Legal"), ("root-c", "HR")):
        registry.add(
            FolderConfig(
                root_id,
                f"https://drive.google.com/drive/folders/{root_id}",
                alias,
                True,
            )
        )
    paths = tuple(
        RemotePath(root_id, (root_id,), ("Policy",))
        for root_id in ("root-a", "root-b", "root-c")
    )
    first_file = replace(sync_fixture.source_file, paths=paths)
    first = replace(
        sync_fixture.first_inventory,
        root_ids=("root-a", "root-b", "root-c"),
        files=(first_file,),
    )
    engine.apply(first, sync_fixture.first_artifacts)
    inventory, artifacts = sync_fixture.changed(revision="2")
    inventory = replace(
        inventory,
        root_ids=("root-a", "root-b", "root-c"),
        files=(replace(inventory.files[0], paths=paths),),
    )
    interrupted = SyncEngine.open(engine.state_root, engine.embedder)
    monkeypatch.setattr(interrupted.index.client, "get_max_batch_size", lambda: 1)
    original_delete = interrupted.index.collection.delete
    exited = False

    def delete_one_then_exit(*args, **kwargs):
        nonlocal exited
        ids = kwargs.get("ids", ())
        assert len(ids) <= 1
        result = original_delete(*args, **kwargs)
        if ids and not exited:
            exited = True
            raise _HardInterruption("partial activation batch")
        return result

    monkeypatch.setattr(
        interrupted.index.collection, "delete", delete_one_then_exit
    )
    with pytest.raises(_HardInterruption):
        interrupted.apply(inventory, artifacts)
    pending = json.loads(interrupted._journal_path.read_text(encoding="utf-8"))
    assert len(pending["journal"]["activation"]) == 6

    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()
    assert recovered.engine.manifest().files["file-a"].revision == "2"
    records = recovered.engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )
    assert len(records["ids"]) == 3
    assert {item["revision"] for item in records["metadatas"]} == {"2"}


@pytest.mark.parametrize("when", ["before", "after"])
def test_hard_exit_at_manifest_write_boundary_recovers(
    sync_fixture, monkeypatch, when
):
    import drive_rag_lib.sync as sync_module

    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    inventory, artifacts = sync_fixture.changed(revision="2")
    original = sync_module.atomic_write_json
    exited = False

    def interrupt_manifest(path, payload):
        nonlocal exited
        if Path(path) == engine._manifest_path and not exited:
            exited = True
            if when == "after":
                original(path, payload)
            raise _HardInterruption(f"{when} manifest write")
        return original(path, payload)

    monkeypatch.setattr(sync_module, "atomic_write_json", interrupt_manifest)
    with pytest.raises(_HardInterruption):
        engine.apply(inventory, artifacts)
    monkeypatch.setattr(sync_module, "atomic_write_json", original)

    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()
    assert recovered.engine.manifest().files["file-a"].revision == "2"
    assert recovered.engine.index.count_file("file-a") == 1


def test_activation_snapshot_rejects_rehashed_arbitrary_metadata(sync_fixture):
    def interrupt(phase):
        if phase == "activating":
            raise RuntimeError("activation checkpoint")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=interrupt,
    )
    with pytest.raises(RuntimeError, match="activation checkpoint"):
        engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)

    journal_path = engine._journal_path
    journal_payload = json.loads(journal_path.read_text(encoding="utf-8"))
    activation_reference = journal_payload["journal"]["activation"][0]
    activation_path = Path(activation_reference["path"])
    activation_payload = json.loads(activation_path.read_text(encoding="utf-8"))
    activation_payload["activation"]["metadatas"][0]["folder_alias"] = "Arbitrary"
    activation_path.write_text(
        json.dumps(activation_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    activation_reference["sha256"] = hashlib.sha256(
        activation_path.read_bytes()
    ).hexdigest()
    journal_path.write_text(
        json.dumps(journal_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(DriveRagError, match="metadata"):
        SyncEngine.open(engine.state_root, engine.embedder).recover()


def test_unchanged_repath_activation_cannot_replace_embedding(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    Registry.load(engine.state_root).add(
        FolderConfig(
            "root-b",
            "https://drive.google.com/drive/folders/root-b",
            "Empty",
            True,
        )
    )
    inventory = replace(
        sync_fixture.first_inventory,
        run_id="run-empty-root",
        root_ids=("root-a", "root-b"),
        generated_at="2026-07-18T14:00:00Z",
    )

    def interrupt(phase):
        if phase == "activating":
            raise RuntimeError("activation checkpoint")

    interrupted = SyncEngine.open(
        engine.state_root,
        engine.embedder,
        phase_callback=interrupt,
    )
    with pytest.raises(RuntimeError, match="activation checkpoint"):
        interrupted.apply(inventory, ArtifactSet(inventory.run_id, ()))

    journal_payload = json.loads(
        interrupted._journal_path.read_text(encoding="utf-8")
    )
    activation = journal_payload["journal"]["activation"]
    assert isinstance(activation, list) and activation
    activation_path = Path(activation[0]["path"])
    activation_payload = json.loads(activation_path.read_text(encoding="utf-8"))
    activation_payload["activation"]["embeddings"][0] = [0.0, 0.0, 0.0]
    activation_path.write_text(
        json.dumps(activation_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    activation[0]["sha256"] = hashlib.sha256(activation_path.read_bytes()).hexdigest()
    interrupted._journal_path.write_text(
        json.dumps(journal_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(DriveRagError, match="embedding"):
        SyncEngine.open(engine.state_root, engine.embedder).recover()
    record = engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["embeddings"]
    )
    assert [float(value) for value in record["embeddings"][0]] == [1.0, 0.0, 0.0]


def test_hard_exit_during_unchanged_restore_recovers(sync_fixture, monkeypatch):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    Registry.load(engine.state_root).rename("root-a", "Governance")
    inventory = replace(
        sync_fixture.first_inventory,
        run_id="run-hard-restore",
        generated_at="2026-07-18T14:01:00Z",
    )
    interrupted = SyncEngine.open(engine.state_root, engine.embedder)
    original_delete = interrupted.index.collection.delete
    exited = False

    def delete_then_exit(*args, **kwargs):
        nonlocal exited
        result = original_delete(*args, **kwargs)
        if kwargs.get("ids") and not exited:
            exited = True
            raise _HardInterruption("during affected restore")
        return result

    monkeypatch.setattr(interrupted.index.collection, "delete", delete_then_exit)
    with pytest.raises(_HardInterruption, match="affected restore"):
        interrupted.apply(inventory, ArtifactSet(inventory.run_id, ()))
    monkeypatch.setattr(interrupted.index.collection, "delete", original_delete)

    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()
    assert recovered.engine.manifest().root_ids == ("root-a",)
    metadata = recovered.engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )["metadatas"]
    assert {item["folder_alias"] for item in metadata} == {"Governance"}


def test_activation_shards_are_byte_bounded(sync_fixture, monkeypatch):
    import drive_rag_lib.sync as sync_module

    monkeypatch.setattr(
        sync_module, "_ACTIVATION_SHARD_BYTES", 1_000, raising=False
    )
    engine = sync_fixture.engine
    registry = Registry.load(engine.state_root)
    for root_id, alias in (("root-b", "Legal"), ("root-c", "HR")):
        registry.add(
            FolderConfig(
                root_id,
                f"https://drive.google.com/drive/folders/{root_id}",
                alias,
                True,
            )
        )
    paths = tuple(
        RemotePath(root_id, (root_id,), ("Policy",))
        for root_id in ("root-a", "root-b", "root-c")
    )
    first = replace(
        sync_fixture.first_inventory,
        root_ids=("root-a", "root-b", "root-c"),
        files=(replace(sync_fixture.source_file, paths=paths),),
    )

    def interrupt(phase):
        if phase == "activating":
            raise RuntimeError("inspect byte shards")

    interrupted = SyncEngine.open(
        engine.state_root, engine.embedder, phase_callback=interrupt
    )
    with pytest.raises(RuntimeError, match="inspect byte shards"):
        interrupted.apply(first, sync_fixture.first_artifacts)
    pending = json.loads(interrupted._journal_path.read_text(encoding="utf-8"))
    references = pending["journal"]["activation"]
    assert len(references) > 1
    assert all(Path(item["path"]).stat().st_size <= 1_000 for item in references)


def test_activation_and_verification_never_use_unbounded_collection_get(
    sync_fixture, monkeypatch
):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    inventory, artifacts = sync_fixture.changed(revision="2")

    def interrupt(phase):
        if phase == "deleted":
            raise RuntimeError("inspect bounded activation")

    interrupted = SyncEngine.open(
        engine.state_root, engine.embedder, phase_callback=interrupt
    )
    with pytest.raises(RuntimeError, match="bounded activation"):
        interrupted.apply(inventory, artifacts)

    recovering = SyncEngine.open(engine.state_root, engine.embedder)
    monkeypatch.setattr(recovering.index.client, "get_max_batch_size", lambda: 1)
    original_get = recovering.index.collection.get
    calls = []

    def bounded_get(*args, **kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("ids") is None:
            assert kwargs.get("where") is not None, "collection-wide get is forbidden"
            assert kwargs.get("limit") == 1, "filtered get must be paginated"
            assert isinstance(kwargs.get("offset"), int)
        else:
            assert len(kwargs["ids"]) <= 1
        return original_get(*args, **kwargs)

    monkeypatch.setattr(recovering.index.collection, "get", bounded_get)
    def inspect_activation(phase):
        if phase == "activating":
            raise RuntimeError("inspect activation scope")

    recovering.phase_callback = inspect_activation
    with pytest.raises(RuntimeError, match="activation scope"):
        recovering.recover()
    pending = json.loads(recovering._journal_path.read_text(encoding="utf-8"))
    captured_file_ids = set()
    for reference in pending["journal"]["activation"]:
        shard = json.loads(Path(reference["path"]).read_text(encoding="utf-8"))
        captured_file_ids.update(
            item["drive_file_id"]
            for item in shard["activation"]["metadatas"]
        )
    assert captured_file_ids == {"file-a"}
    assert any(call.get("offset", 0) > 0 for call in calls)
    assert sum(call.get("ids") is not None for call in calls) > 1
    recovering.phase_callback = None
    recovered = recovering.recover()
    assert recovered.engine.manifest().files["file-a"].revision == "2"


@pytest.mark.parametrize("validator", ["transition", "target"])
@pytest.mark.parametrize(
    "mutation", ["valid", "extra", "missing", "balanced", "duplicate", "corrupt"]
)
def test_global_index_verification_is_bounded_and_exact_across_many_files(
    sync_fixture, monkeypatch, validator, mutation
):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    manifest = engine.manifest()
    files = dict(manifest.files)
    roots = {
        "file-a": {
            "root-a": {
                "alias": "Finance",
                "drive_path": "Policy",
                "drive_url": "https://docs.google.com/document/d/file-a/edit",
                "local_path": str(
                    engine.state_root / "mirrors" / "Finance" / "Policy.pdf"
                ),
            }
        }
    }
    base_roots = {
        "file-a": {
            "root-a": {
                "folder_alias": "Finance",
                "local_path": roots["file-a"]["root-a"]["local_path"],
                "drive_url": roots["file-a"]["root-a"]["drive_url"],
            }
        }
    }
    mime_types = {"file-a": sync_fixture.source_file.mime_type}
    inserted_ids = []
    for ordinal in range(8):
        file_id = f"unrelated-{ordinal}"
        revision = "1"
        locator = f"section:Unrelated-{ordinal}"
        document = f"unrelated content {ordinal}"
        digest = hashlib.sha256(
            f"{file_id}\0{revision}\0{locator}\0{0}\0{document}".encode()
        ).hexdigest()
        chunk_id = f"{file_id}:{digest}"
        record_id = json.dumps([chunk_id, "root-a"], separators=(",", ":"))
        local_path = str(
            engine.state_root
            / "mirrors"
            / "Finance"
            / f"Unrelated-{ordinal}.pdf"
        )
        root = {
            "alias": "Finance",
            "drive_path": f"Unrelated-{ordinal}",
            "drive_url": f"https://drive.google.com/file/d/{file_id}/view",
            "local_path": local_path,
        }
        metadata = {
            "drive_file_id": file_id,
            "revision": revision,
            "root_id": "root-a",
            "folder_alias": root["alias"],
            "drive_path": root["drive_path"],
            "drive_url": root["drive_url"],
            "local_path": root["local_path"],
            "locator": locator,
            "content_hash": hashlib.sha256(document.encode()).hexdigest(),
            "mime_type": "application/pdf",
        }
        engine.index.collection.upsert(
            ids=[record_id],
            documents=[document],
            metadatas=[metadata],
            embeddings=[[1.0, 0.0, 0.0]],
        )
        files[file_id] = ManifestFile(
            file_id,
            revision,
            None,
            "a" * 64,
            (RemotePath("root-a", ("root-a",), (f"Unrelated-{ordinal}.pdf",)),),
            (chunk_id,),
            "document",
        )
        roots[file_id] = {"root-a": root}
        base_roots[file_id] = {
            "root-a": {
                "folder_alias": root["alias"],
                "drive_path": root["drive_path"],
                "local_path": root["local_path"],
                "drive_url": root["drive_url"],
            }
        }
        mime_types[file_id] = "application/pdf"
        inserted_ids.append(record_id)
    expanded = replace(manifest, files=files)
    extra_id = json.dumps([f"extra:{'e' * 64}", "root-a"], separators=(",", ":"))
    sample = engine.index.collection.get(
        ids=[inserted_ids[-1]], include=["documents", "metadatas", "embeddings"]
    )
    if mutation in {"missing", "balanced"}:
        engine.index.collection.delete(ids=[inserted_ids[-1]])
    if mutation in {"extra", "balanced"}:
        extra_metadata = dict(sample["metadatas"][0])
        extra_metadata["drive_file_id"] = "extra"
        engine.index.collection.upsert(
            ids=[extra_id],
            documents=[sample["documents"][0]],
            metadatas=[extra_metadata],
            embeddings=[sample["embeddings"][0]],
        )
    if mutation == "corrupt":
        corrupt_metadata = dict(sample["metadatas"][0])
        corrupt_metadata["drive_path"] = "Corrupt"
        engine.index.collection.upsert(
            ids=[inserted_ids[-1]],
            documents=[sample["documents"][0]],
            metadatas=[corrupt_metadata],
            embeddings=[sample["embeddings"][0]],
        )

    monkeypatch.setattr(engine.index.client, "get_max_batch_size", lambda: 2)
    original_get = engine.index.collection.get
    id_batches = []

    def bounded_get(*args, **kwargs):
        ids = kwargs.get("ids")
        assert ids is not None, "global verification must use exact ID batches"
        assert 0 < len(ids) <= 2
        id_batches.append(tuple(ids))
        result = original_get(*args, **kwargs)
        if mutation == "duplicate" and inserted_ids[-3] in ids and len(ids) == 2:
            duplicated = dict(result)
            duplicated["ids"] = [result["ids"][0], result["ids"][0]]
            for key in ("documents", "metadatas", "embeddings"):
                values = result.get(key)
                if values is not None:
                    duplicated[key] = [values[0], values[0]]
            return duplicated
        return result

    monkeypatch.setattr(engine.index.collection, "get", bounded_get)

    def verify():
        if validator == "transition":
            engine.index.assert_transition_records(
                expanded, expanded, {}, base_roots
            )
        else:
            engine.index.assert_target_records(expanded, {}, roots, mime_types)

    if mutation == "valid":
        verify()
        assert len(id_batches) > 1
    else:
        with pytest.raises(DriveRagError, match="index|record|metadata|count"):
            verify()


def test_legacy_deleted_journal_without_activation_field_recovers(sync_fixture):
    def interrupt(phase):
        if phase == "deleted":
            raise RuntimeError("legacy deleted checkpoint")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=interrupt,
    )
    with pytest.raises(RuntimeError, match="legacy deleted checkpoint"):
        engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)

    payload = json.loads(engine._journal_path.read_text(encoding="utf-8"))
    payload["journal"].pop("activation")
    engine._journal_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()
    assert recovered.engine.manifest().files["file-a"].revision == "1"


def test_status_is_content_free_and_reports_committed_counts_without_index_reads(
    sync_fixture, monkeypatch
):
    engine = sync_fixture.engine
    Registry.load(engine.state_root).add(
        FolderConfig(
            "root-b",
            "https://drive.google.com/drive/folders/root-b",
            "Legal",
            True,
        )
    )
    shared = replace(
        sync_fixture.source_file,
        paths=(
            RemotePath("root-a", ("root-a",), ("Policy",)),
            RemotePath("root-b", ("root-b",), ("Policy",)),
        ),
    )
    inventory = replace(
        sync_fixture.first_inventory,
        root_ids=("root-a", "root-b"),
        files=(shared,),
    )
    engine.apply(inventory, sync_fixture.first_artifacts)

    def reject_get(*args, **kwargs):
        raise AssertionError("status must not read Chroma records")

    monkeypatch.setattr(engine.index.collection, "get", reject_get)

    status = engine.status().to_dict()
    encoded = json.dumps(status)

    assert status["folder_count"] == 2
    assert status["indexed_files"] == 1
    assert status["indexed_chunks"] == 1
    assert status["pending_journal"] is False
    assert status["schedule_state"] == "NOT_CONFIGURED"
    assert "Retention policy" not in encoded


def test_status_reports_only_a_validated_persisted_schedule(sync_fixture):
    from drive_rag_lib.schedule import (
        SCHEDULE_PROJECT_MODE,
        SCHEDULE_PROJECT_PATH,
        SCHEDULE_PROMPT,
        SCHEDULE_RRULE,
        record_schedule,
    )

    record = record_schedule(sync_fixture.engine.state_root, "task-observed-123")
    payload = json.loads(
        (sync_fixture.engine.state_root / "config" / "schedule.json").read_text(
            encoding="utf-8"
        )
    )

    assert sync_fixture.engine.status().schedule_state == "CONFIGURED"
    assert payload == {"schema_version": "1", "schedule": record.to_dict()}
    assert payload["schedule"] == {
        "task_id": "task-observed-123",
        "rrule": SCHEDULE_RRULE,
        "project_mode": SCHEDULE_PROJECT_MODE,
        "project_path": SCHEDULE_PROJECT_PATH,
        "enabled": True,
        "prompt": SCHEDULE_PROMPT,
    }


def test_malformed_schedule_record_cannot_report_configured(sync_fixture):
    path = sync_fixture.engine.state_root / "config" / "schedule.json"
    path.write_text(
        json.dumps({"schema_version": "1", "schedule": {"task_id": "forged"}}),
        encoding="utf-8",
    )

    with pytest.raises(DriveRagError, match="schedule") as error:
        sync_fixture.engine.status()

    assert error.value.code == "INVALID_SCHEDULE_RECORD"


def test_clearing_an_observed_disabled_schedule_returns_not_configured(sync_fixture):
    from drive_rag_lib.schedule import clear_schedule, record_schedule

    record_schedule(sync_fixture.engine.state_root, "task-to-disable")
    assert clear_schedule(sync_fixture.engine.state_root, "task-to-disable") is True
    assert sync_fixture.engine.status().schedule_state == "NOT_CONFIGURED"
    assert clear_schedule(sync_fixture.engine.state_root, "task-to-disable") is False


def test_schedule_clear_requires_the_observed_local_task_id(sync_fixture):
    from drive_rag_lib.schedule import clear_schedule, load_schedule, record_schedule

    record_schedule(sync_fixture.engine.state_root, "task-local-identity")

    with pytest.raises(DriveRagError, match="task ID") as error:
        clear_schedule(sync_fixture.engine.state_root, "different-task")

    assert error.value.code == "SCHEDULE_IDENTITY_MISMATCH"
    assert load_schedule(sync_fixture.engine.state_root).task_id == "task-local-identity"


def test_schedule_record_is_idempotent_and_refuses_a_different_cached_id(
    sync_fixture,
):
    from drive_rag_lib.schedule import load_schedule, record_schedule

    first = record_schedule(sync_fixture.engine.state_root, "task-original")
    path = sync_fixture.engine.state_root / "config" / "schedule.json"
    original = path.read_bytes()
    assert record_schedule(sync_fixture.engine.state_root, "task-original") == first
    assert path.read_bytes() == original

    with pytest.raises(DriveRagError, match="task ID") as error:
        record_schedule(sync_fixture.engine.state_root, "task-duplicate")

    assert error.value.code == "SCHEDULE_IDENTITY_MISMATCH"
    assert load_schedule(sync_fixture.engine.state_root).task_id == "task-original"
    assert path.read_bytes() == original


def test_schedule_record_cli_preserves_existing_identity_on_mismatch(tmp_path, capsys):
    state = tmp_path / "state"
    base = ["--state-root", str(state), "schedule", "record", "--task-id"]

    assert drive_rag.main([*base, "task-original"]) == 0
    capsys.readouterr()
    assert drive_rag.main([*base, "task-duplicate"]) == 2
    failed = json.loads(capsys.readouterr().out)

    assert failed["operation"] == "schedule.record"
    assert failed["error"]["code"] == "SCHEDULE_IDENTITY_MISMATCH"
    assert drive_rag.main(
        ["--state-root", str(state), "schedule", "show"]
    ) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["task_id"] == "task-original"


def test_schedule_show_is_content_free_and_reports_cached_claim(tmp_path, capsys):
    from drive_rag_lib.schedule import SCHEDULE_PROMPT, record_schedule

    state = tmp_path / "state"
    record_schedule(state, "task-show-123")

    assert drive_rag.main(
        ["--state-root", str(state), "schedule", "show"]
    ) == 0
    raw = capsys.readouterr().out
    assert SCHEDULE_PROMPT not in raw
    shown = json.loads(raw)
    assert shown == {
        "configured": True,
        "enabled": True,
        "operation": "schedule.show",
        "project_mode": "local",
        "project_path": "/root/codexcode",
        "prompt_sha256": hashlib.sha256(SCHEDULE_PROMPT.encode("utf-8")).hexdigest(),
        "rrule": "FREQ=HOURLY;INTERVAL=1",
        "schema_version": "1",
        "status": "ok",
        "task_id": "task-show-123",
    }


def test_schedule_show_reports_absent_without_fabricating_identity(tmp_path, capsys):
    assert drive_rag.main(
        ["--state-root", str(tmp_path / "state"), "schedule", "show"]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "configured": False,
        "operation": "schedule.show",
        "schema_version": "1",
        "status": "ok",
    }


def test_schedule_show_rejects_malformed_cached_claim(tmp_path, capsys):
    from drive_rag_lib.paths import ensure_state_root

    state = ensure_state_root(tmp_path / "state")
    (state / "config" / "schedule.json").write_text(
        json.dumps({"schema_version": "1", "schedule": {"task_id": "forged"}}),
        encoding="utf-8",
    )

    assert drive_rag.main(
        ["--state-root", str(state), "schedule", "show"]
    ) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["operation"] == "schedule.show"
    assert output["status"] == "error"
    assert output["error"]["code"] == "INVALID_SCHEDULE_RECORD"


def test_schedule_cli_records_and_clears_an_observed_task(tmp_path, capsys):
    state = tmp_path / "state"

    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "schedule",
            "record",
            "--task-id",
            "task-cli-123",
        ]
    ) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["operation"] == "schedule.record"
    assert recorded["status"] == "ok"
    assert recorded["task_id"] == "task-cli-123"

    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "schedule",
            "clear",
            "--task-id",
            "task-cli-123",
        ]
    ) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared == {
        "operation": "schedule.clear",
        "removed": True,
        "schema_version": "1",
        "status": "ok",
    }


def test_status_ignores_pending_staged_records_without_index_reads(
    sync_fixture, monkeypatch
):
    sync_fixture.engine.apply(
        sync_fixture.first_inventory, sync_fixture.first_artifacts
    )
    inventory, artifacts = sync_fixture.changed(
        text="A separately staged replacement policy", revision="2"
    )

    def interrupt(phase):
        if phase == "indexed":
            raise RuntimeError("leave candidate records staged")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=interrupt,
    )
    with pytest.raises(RuntimeError, match="candidate records staged"):
        engine.apply(inventory, artifacts)
    assert engine.index.collection.count() > 1

    def reject_get(*args, **kwargs):
        raise AssertionError("status must not read Chroma records")

    monkeypatch.setattr(engine.index.collection, "get", reject_get)

    status = engine.status().to_dict()

    assert status["indexed_files"] == 1
    assert status["indexed_chunks"] == 1
    assert status["pending_journal"] is True


def test_status_reports_empty_manifest_without_index_reads(sync_fixture, monkeypatch):
    def reject_get(*args, **kwargs):
        raise AssertionError("status must not read Chroma records")

    monkeypatch.setattr(sync_fixture.engine.index.collection, "get", reject_get)

    status = sync_fixture.engine.status().to_dict()

    assert status["indexed_files"] == 0
    assert status["indexed_chunks"] == 0
    assert status["pending_journal"] is False


def test_status_fails_closed_on_corrupt_manifest_without_index_reads(
    sync_fixture, monkeypatch
):
    sync_fixture.engine._manifest_path.write_text("{", encoding="utf-8")
    calls = []

    def reject_get(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("status must not read Chroma records")

    monkeypatch.setattr(sync_fixture.engine.index.collection, "get", reject_get)

    with pytest.raises(DriveRagError, match="could not read JSON"):
        sync_fixture.engine.status()

    assert calls == []


def test_manifest_is_committed_last(sync_fixture):
    observed = []

    def inject(phase):
        observed.append(phase)
        if phase == "deleted":
            raise RuntimeError("injected")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=inject,
    )
    with pytest.raises(RuntimeError):
        engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)

    assert observed == ["planned", "verified", "indexed", "promoted", "deleted"]
    assert engine.manifest().files == {}
    engine.recover()
    assert engine.manifest().files["file-a"].revision == "1"


def test_alias_rename_moves_mirror_and_index_metadata_without_embedding(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    passage_calls = sync_fixture.embedder.passage_calls
    Registry.load(engine.state_root).rename("root-a", "Governance")
    renamed = replace(
        sync_fixture.first_inventory,
        run_id="run-alias",
        generated_at="2026-07-18T11:00:00Z",
    )

    engine.apply(renamed, ArtifactSet("run-alias", ()))

    assert sync_fixture.embedder.passage_calls == passage_calls
    assert not (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()
    assert (
        engine.state_root / "mirrors" / "Governance" / "Policy.pdf"
    ).exists()
    metadata = engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["metadatas"]
    )["metadatas"][0]
    assert metadata["folder_alias"] == "Governance"


def test_sync_folder_snapshot_rejects_casefolded_alias_collision(sync_fixture):
    folders = (
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
    )

    with pytest.raises(DriveRagError, match="unique") as error:
        sync_fixture.engine._validate_folder_snapshot(folders, {"root-a", "root-b"})

    assert error.value.code == "INVALID_JOURNAL"


def test_alias_repath_rejects_unchanged_document_content_tampering(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    snapshot = _snapshot_index(engine.index.collection)
    metadata = dict(snapshot["metadatas"][0])
    engine.index.collection.upsert(
        ids=[snapshot["ids"][0]],
        documents=["EVIL TAMPERED CONTENT"],
        metadatas=[metadata],
        embeddings=[snapshot["embeddings"][0]],
    )
    Registry.load(engine.state_root).rename("root-a", "Governance")
    renamed = replace(
        sync_fixture.first_inventory,
        run_id="run-alias-content-check",
        generated_at="2026-07-18T13:30:00Z",
    )

    with pytest.raises(DriveRagError, match="index|content|record"):
        engine.apply(renamed, ArtifactSet(renamed.run_id, ()))

    assert engine.manifest().files["file-a"].revision == "1"
    assert engine.has_pending_journal()
    _replace_index_with_snapshot(engine.index.collection, snapshot)
    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()
    assert recovered.engine.manifest().files["file-a"].revision == "1"
    record = recovered.engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=["documents", "metadatas"]
    )
    assert record["documents"] == ["Retention policy"]
    assert record["metadatas"][0]["folder_alias"] == "Governance"


@pytest.mark.parametrize("interrupted_phase", [None, "promoted", "deleted"])
def test_native_pdf_export_collision_is_suffixed_recoverable_and_deletable(
    sync_fixture, interrupted_phase
):
    first = sync_fixture.first_artifacts.artifacts[0]
    binary_payload = Path(first.payload_path).with_name("file-b.pdf")
    binary_payload.write_bytes(Path(first.payload_path).read_bytes())
    binary = replace(
        sync_fixture.source_file,
        file_id="file-b",
        name="Policy.pdf",
        mime_type="application/pdf",
        drive_url="https://drive.google.com/file/d/file-b/view",
        size=binary_payload.stat().st_size,
        paths=(RemotePath("root-a", ("root-a",), ("Policy.pdf",)),),
        native_kind=None,
    )
    inventory = replace(
        sync_fixture.first_inventory,
        files=(sync_fixture.source_file, binary),
    )
    binary_artifact = Artifact(
        "file-b",
        "1",
        str(binary_payload),
        hashlib.sha256(binary_payload.read_bytes()).hexdigest(),
        None,
    )
    artifacts = ArtifactSet("run-1", (first, binary_artifact))

    def inject(phase):
        if phase == interrupted_phase:
            raise RuntimeError(f"injected-{phase}")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=inject if interrupted_phase else None,
    )
    if interrupted_phase:
        with pytest.raises(RuntimeError, match=f"injected-{interrupted_phase}"):
            engine.apply(inventory, artifacts)
        engine = SyncEngine.open(engine.state_root, engine.embedder)
        engine.recover()
    else:
        engine.apply(inventory, artifacts)

    mirrors = sorted((engine.state_root / "mirrors" / "Finance").glob("*.pdf"))
    expected_names = {
        f"Policy__{hashlib.sha256(b'file-a').hexdigest()[:8]}.pdf",
        f"Policy__{hashlib.sha256(b'file-b').hexdigest()[:8]}.pdf",
    }
    assert {path.name for path in mirrors} == expected_names
    records = engine.index.collection.get(include=["metadatas"])["metadatas"]
    drive_paths = {
        item["drive_file_id"]: item["drive_path"] for item in records
    }
    assert drive_paths == {"file-a": "Policy", "file-b": "Policy.pdf"}

    empty = replace(
        sync_fixture.empty_inventory,
        run_id="run-delete-collision",
        generated_at="2026-07-18T14:00:00Z",
    )
    engine.apply(empty, ArtifactSet(empty.run_id, ()))
    assert list((engine.state_root / "mirrors" / "Finance").glob("*.pdf")) == []
    assert engine.index.collection.count() == 0


def test_payload_hashing_is_streamed(sync_fixture, monkeypatch):
    from drive_rag_lib.models import RemoteFile

    state = sync_fixture.engine.state_root
    payload = state / "staging" / "run-bin" / "blob.bin"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b"binary payload")
    remote = RemoteFile(
        "file-bin",
        "blob.bin",
        "application/octet-stream",
        "1",
        "2026-07-18T11:00:00Z",
        "https://drive.google.com/file/d/file-bin/view",
        None,
        payload.stat().st_size,
        (RemotePath("root-a", ("root-a",), ("blob.bin",)),),
        None,
    )
    inventory = RemoteInventory(
        "run-bin",
        True,
        ("root-a",),
        (remote,),
        None,
        "2026-07-18T11:00:00Z",
    )
    artifact = Artifact(
        "file-bin",
        "1",
        str(payload),
        __import__("hashlib").sha256(payload.read_bytes()).hexdigest(),
        None,
    )
    original = Path.read_bytes

    def forbid_payload_read_bytes(path):
        if path == payload:
            raise AssertionError("payload must be hashed with bounded streaming reads")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_payload_read_bytes)

    result = sync_fixture.engine.apply(
        inventory, ArtifactSet("run-bin", (artifact,))
    )

    assert result.status == "SYNC_OK_CHANGED"
    assert (state / "mirrors" / "Finance" / "blob.bin").read_bytes() == b"binary payload"


@pytest.mark.parametrize("phase", ["indexed", "promoted", "deleted"])
def test_remote_deletion_recovers_exactly_after_phase_crash(sync_fixture, phase):
    sync_fixture.engine.apply(
        sync_fixture.first_inventory, sync_fixture.first_artifacts
    )

    def inject(observed):
        if observed == phase:
            raise RuntimeError(f"injected-{phase}")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=inject,
    )
    with pytest.raises(RuntimeError):
        engine.apply(sync_fixture.empty_inventory, sync_fixture.empty_artifacts)

    recovered = SyncEngine.open(engine.state_root, engine.embedder).recover()
    assert recovered.engine.index.count_file("file-a") == 0
    assert recovered.engine.manifest().files == {}
    assert not (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()


def test_identical_committed_inventory_is_an_idempotent_noop(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    before = engine.manifest()

    result = engine.apply(
        sync_fixture.first_inventory,
        ArtifactSet(sync_fixture.first_inventory.run_id, ()),
    )

    assert result.status == "SYNC_OK_NO_CHANGES"
    assert engine.manifest() == before


def test_same_run_pending_retry_must_match_journal_identity(sync_fixture):
    def inject(phase):
        if phase == "planned":
            raise RuntimeError("injected")

    engine = SyncEngine.open(
        sync_fixture.engine.state_root,
        sync_fixture.embedder,
        phase_callback=inject,
    )
    with pytest.raises(RuntimeError):
        engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    conflicting = replace(sync_fixture.first_inventory, files=())

    with pytest.raises(DriveRagError, match="pending|identity"):
        SyncEngine.open(engine.state_root, engine.embedder).apply(
            conflicting,
            ArtifactSet(conflicting.run_id, ()),
        )

    assert engine.has_pending_journal()


def test_corrupt_base_index_blocks_remote_deletion(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    committed = engine.manifest().files["file-a"]
    object_path = engine.object_path("file-a", committed.object_sha256)
    record_id = engine.index.collection.get(
        where={"drive_file_id": "file-a"}, include=[]
    )["ids"][0]
    engine.index.collection.delete(ids=[record_id])

    with pytest.raises(DriveRagError, match="index|record"):
        engine.apply(sync_fixture.empty_inventory, sync_fixture.empty_artifacts)

    assert object_path.exists()
    assert (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()
    assert "file-a" in engine.manifest().files


def test_cleanup_failure_retains_committed_journal_for_recovery(
    sync_fixture, monkeypatch
):
    import drive_rag_lib.sync as sync_module

    original = sync_module.shutil.rmtree
    attempts = 0

    def fail_once(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected cleanup failure")
        return original(path)

    monkeypatch.setattr(sync_module.shutil, "rmtree", fail_once)
    with pytest.raises(DriveRagError, match="staging"):
        sync_fixture.engine.apply(
            sync_fixture.first_inventory, sync_fixture.first_artifacts
        )

    assert sync_fixture.engine.has_pending_journal()
    recovered = SyncEngine.open(
        sync_fixture.engine.state_root, sync_fixture.embedder
    ).recover()
    assert recovered.status == "SYNC_OK_CHANGED"
    assert recovered.engine.manifest().files["file-a"].revision == "1"
    assert not recovered.engine.has_pending_journal()


def test_partial_cleanup_missing_structured_source_recovers_committed_journal(
    sync_fixture, monkeypatch
):
    import drive_rag_lib.sync as sync_module

    original = sync_module.shutil.rmtree
    attempts = 0

    def partially_remove_then_fail(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            structured = Path(
                sync_fixture.first_artifacts.artifacts[0].structured_path
            )
            structured.unlink()
            raise OSError("injected partial cleanup failure")
        return original(path)

    monkeypatch.setattr(sync_module.shutil, "rmtree", partially_remove_then_fail)
    with pytest.raises(DriveRagError, match="staging"):
        sync_fixture.engine.apply(
            sync_fixture.first_inventory, sync_fixture.first_artifacts
        )

    assert sync_fixture.engine.has_pending_journal()
    assert sync_fixture.engine.manifest().files["file-a"].revision == "1"
    recovered = SyncEngine.open(
        sync_fixture.engine.state_root, sync_fixture.embedder
    ).recover()
    assert recovered.status == "SYNC_OK_CHANGED"
    assert not recovered.engine.has_pending_journal()


def test_native_to_non_native_transition_deletes_old_pdf_mirror(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    payload = engine.state_root / "staging" / "run-text" / "file-a.txt"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text("plain retention policy", encoding="utf-8")
    changed = replace(
        sync_fixture.source_file,
        revision="2",
        modified_time="2026-07-18T11:00:00Z",
        mime_type="text/plain",
        drive_url="https://drive.google.com/file/d/file-a/view",
        size=payload.stat().st_size,
        native_kind=None,
    )
    inventory = RemoteInventory(
        "run-text",
        True,
        ("root-a",),
        (changed,),
        None,
        "2026-07-18T11:00:00Z",
    )
    artifact = Artifact(
        "file-a",
        "2",
        str(payload),
        __import__("hashlib").sha256(payload.read_bytes()).hexdigest(),
        None,
    )

    engine.apply(inventory, ArtifactSet("run-text", (artifact,)))

    assert not (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()
    assert (engine.state_root / "mirrors" / "Finance" / "Policy").exists()


def test_malformed_pending_journal_path_keeps_queries_stale(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    pending = engine.state_root / "journal" / "pending.json"
    pending.mkdir()

    assert engine.has_pending_journal()
    assert engine.status().pending_journal is True
    with pytest.raises(DriveRagError, match="pending"):
        engine.assert_query_ready()


def test_duplicate_name_suffix_does_not_change_original_drive_path(sync_fixture):
    engine = sync_fixture.engine
    first_artifact = sync_fixture.first_artifacts.artifacts[0]
    first_payload = Path(first_artifact.payload_path)
    first_structured = Path(first_artifact.structured_path)
    second_payload = first_payload.with_name("file-b.pdf")
    second_structured = first_structured.with_name("file-b.structured.json")
    second_payload.write_bytes(first_payload.read_bytes())
    second_structured.write_bytes(first_structured.read_bytes())
    second_file = replace(
        sync_fixture.source_file,
        file_id="file-b",
        drive_url="https://docs.google.com/document/d/file-b/edit",
    )
    inventory = replace(
        sync_fixture.first_inventory,
        files=(sync_fixture.source_file, second_file),
    )
    second_artifact = Artifact(
        "file-b",
        "1",
        str(second_payload),
        __import__("hashlib").sha256(second_payload.read_bytes()).hexdigest(),
        str(second_structured),
    )

    engine.apply(
        inventory,
        ArtifactSet("run-1", (first_artifact, second_artifact)),
    )

    metadatas = engine.index.collection.get(include=["metadatas"])["metadatas"]
    assert {item["drive_path"] for item in metadatas} == {"Policy"}
    assert all("__" in item["local_path"] for item in metadatas)


def test_later_phase_journal_rejects_unsafe_run_id_before_deletion(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)

    def inject(phase):
        if phase == "promoted":
            raise RuntimeError("injected")

    interrupted = SyncEngine.open(
        engine.state_root, engine.embedder, phase_callback=inject
    )
    with pytest.raises(RuntimeError):
        interrupted.apply(sync_fixture.empty_inventory, sync_fixture.empty_artifacts)
    pending = engine.state_root / "journal" / "pending.json"
    payload = json.loads(pending.read_text(encoding="utf-8"))
    journal = payload["journal"]
    journal["run_id"] = "/tmp/escape"
    journal["inventory"]["run_id"] = "/tmp/escape"
    journal["artifacts"]["run_id"] = "/tmp/escape"
    journal["plan"]["run_id"] = "/tmp/escape"
    pending.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DriveRagError, match="run ID"):
        SyncEngine.open(engine.state_root, engine.embedder).recover()

    assert engine.index.count_file("file-a") == 1
    assert (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()


def test_stale_copied_journal_cannot_delete_newer_committed_state(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)

    def inject(phase):
        if phase == "planned":
            raise RuntimeError("injected")

    interrupted = SyncEngine.open(
        engine.state_root, engine.embedder, phase_callback=inject
    )
    with pytest.raises(RuntimeError):
        interrupted.apply(sync_fixture.empty_inventory, sync_fixture.empty_artifacts)
    pending = engine.state_root / "journal" / "pending.json"
    stale_journal = pending.read_bytes()
    SyncEngine.open(engine.state_root, engine.embedder).recover()

    newer_inventory, newer_artifacts = sync_fixture.changed(revision="2")
    newer_inventory = replace(
        newer_inventory,
        run_id="run-newer",
        generated_at="2026-07-18T13:00:00Z",
    )
    newer_artifact = replace(
        newer_artifacts.artifacts[0],
        payload_path=newer_artifacts.artifacts[0].payload_path.replace(
            "run-2", "run-newer"
        ),
        structured_path=newer_artifacts.artifacts[0].structured_path.replace(
            "run-2", "run-newer"
        ),
    )
    old_payload = Path(newer_artifacts.artifacts[0].payload_path)
    old_structured = Path(newer_artifacts.artifacts[0].structured_path)
    new_payload = Path(newer_artifact.payload_path)
    new_structured = Path(newer_artifact.structured_path)
    new_payload.parent.mkdir(parents=True, exist_ok=True)
    old_payload.replace(new_payload)
    old_structured.replace(new_structured)
    engine.apply(
        newer_inventory,
        ArtifactSet("run-newer", (newer_artifact,)),
    )
    pending.write_bytes(stale_journal)

    with pytest.raises(DriveRagError, match="current manifest|journal"):
        SyncEngine.open(engine.state_root, engine.embedder).recover()

    assert engine.manifest().files["file-a"].revision == "2"
    assert engine.index.count_file("file-a") == 1
    assert (engine.state_root / "mirrors" / "Finance" / "Policy.pdf").exists()


def _write_cli_input(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "1", **payload}), encoding="utf-8")
    return path


def test_artifact_set_loader_requires_exact_schema(sync_fixture):
    path = sync_fixture.engine.state_root / "staging" / "artifacts.json"
    _write_cli_input(path, sync_fixture.first_artifacts.to_dict())
    assert load_artifact_set(path) == sync_fixture.first_artifacts
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                **sync_fixture.first_artifacts.to_dict(),
                "unexpected": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DriveRagError, match="exactly"):
        load_artifact_set(path)


def test_cli_sync_apply_and_content_free_status(sync_fixture, monkeypatch, capsys):
    state = sync_fixture.engine.state_root
    inventory_path = state / "staging" / "inventory.json"
    artifacts_path = state / "staging" / "artifacts.json"
    _write_cli_input(inventory_path, sync_fixture.first_inventory.to_dict())
    _write_cli_input(artifacts_path, sync_fixture.first_artifacts.to_dict())
    monkeypatch.setattr(drive_rag, "FastEmbedE5", lambda _state: sync_fixture.embedder)

    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "sync",
            "apply",
            "--inventory",
            str(inventory_path),
            "--artifacts",
            str(artifacts_path),
        ]
    ) == 0
    apply_result = json.loads(capsys.readouterr().out)
    assert apply_result["status"] == "SYNC_OK_CHANGED"
    assert apply_result["counts"] == {"indexed_chunks": 1, "indexed_files": 1}

    assert drive_rag.main(["--state-root", str(state), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["operation"] == "status"
    assert status["pending_journal"] is False
    assert "Retention policy" not in json.dumps(status)


def test_cli_status_requires_first_run_folder_configuration(tmp_path, capsys):
    state = ensure_state_root(tmp_path / "state")

    assert drive_rag.main(["--state-root", str(state), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["schema_version"] == "1"
    assert result["operation"] == "status"
    assert result["status"] == "error"
    assert result["error"]["code"] == "CONFIGURATION_REQUIRED"


def _reconcile_removed_last_folder(sync_fixture):
    engine = sync_fixture.engine
    engine.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    Registry.load(engine.state_root).remove("root-a")
    empty = RemoteInventory(
        "run-remove-last-folder",
        True,
        (),
        (),
        None,
        "2026-07-18T13:00:00Z",
    )
    result = engine.apply(empty, ArtifactSet(empty.run_id, ()))

    assert result.status == "SYNC_OK_CHANGED"
    assert engine.manifest().files == {}
    assert engine.manifest().root_ids == ()
    assert engine.index.collection.count() == 0
    return engine


def test_cli_status_onboards_after_successfully_reconciled_last_folder_removal(
    sync_fixture, capsys
):
    engine = _reconcile_removed_last_folder(sync_fixture)
    (engine.state_root / "mirrors" / "empty").mkdir()
    (engine.state_root / "objects" / "empty").mkdir()
    assert any((engine.state_root / "chroma").iterdir())

    assert drive_rag.main(["--state-root", str(engine.state_root), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == "CONFIGURATION_REQUIRED"


@pytest.mark.parametrize(
    ("corrupt", "expected_code"),
    [
        ("pending", "INDEX_STALE"),
        ("manifest", "STATE_READ_FAILED"),
        ("folder-snapshot", "INDEX_STALE"),
        ("failed-sync", "INDEX_STALE"),
        ("root-scope", "INDEX_STALE"),
        ("mirror", "INDEX_STALE"),
        ("index-record", "INDEX_STALE"),
    ],
)
def test_cli_status_reconciled_empty_state_still_fails_closed_on_corruption(
    sync_fixture, capsys, corrupt, expected_code
):
    engine = _reconcile_removed_last_folder(sync_fixture)
    state = engine.state_root
    if corrupt == "pending":
        (state / "journal" / "pending.json").symlink_to(state / "missing-journal")
    elif corrupt == "manifest":
        (state / "manifests" / "current.json").write_text("{", encoding="utf-8")
    elif corrupt == "folder-snapshot":
        _write_cli_input(
            state / "manifests" / "folders.json",
            {"folders": [sync_fixture.first_inventory.root_ids[0]]},
        )
    elif corrupt == "failed-sync":
        _write_cli_input(
            state / "manifests" / "current.json",
            replace(engine.manifest(), last_failure="failed").to_dict(),
        )
    elif corrupt == "root-scope":
        _write_cli_input(
            state / "manifests" / "current.json",
            replace(engine.manifest(), root_ids=("root-a",)).to_dict(),
        )
    elif corrupt == "mirror":
        orphan = state / "mirrors" / "orphan" / "payload.pdf"
        orphan.parent.mkdir()
        orphan.write_bytes(b"orphan")
    else:
        engine.index.collection.upsert(
            ids=["orphan-record"],
            documents=["orphan"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadatas=[{"drive_file_id": "orphan"}],
        )

    assert drive_rag.main(["--state-root", str(state), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == expected_code


def test_cli_status_reconciled_empty_state_rejects_missing_index_without_repair(
    sync_fixture, capsys
):
    engine = _reconcile_removed_last_folder(sync_fixture)
    engine.index.client.delete_collection(COLLECTION_NAME)
    assert engine.index.client.list_collections() == []

    assert drive_rag.main(["--state-root", str(engine.state_root), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == "INDEX_STALE"
    assert engine.index.client.list_collections() == []


@pytest.mark.parametrize("orphan_records", [0, 1])
def test_cli_status_reconciled_empty_state_rejects_orphan_index_collection(
    sync_fixture, capsys, orphan_records
):
    engine = _reconcile_removed_last_folder(sync_fixture)
    orphan = engine.index.client.create_collection("orphan")
    if orphan_records:
        orphan.add(
            ids=["orphan-record"],
            documents=["orphan"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadatas=[{"drive_file_id": "orphan"}],
        )
    assert sorted(
        (collection.name, collection.count())
        for collection in engine.index.client.list_collections()
    ) == [(COLLECTION_NAME, 0), ("orphan", orphan_records)]

    assert drive_rag.main(["--state-root", str(engine.state_root), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == "INDEX_STALE"


def test_cli_status_requires_recovery_for_empty_registry_pending_journal(
    tmp_path, capsys
):
    state = ensure_state_root(tmp_path / "state")
    (state / "journal" / "pending.json").symlink_to(tmp_path / "missing-journal")

    assert drive_rag.main(["--state-root", str(state), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == "INDEX_STALE"


def test_cli_status_rejects_committed_manifest_with_empty_registry(
    sync_fixture, capsys
):
    state = sync_fixture.engine.state_root
    sync_fixture.engine.apply(
        sync_fixture.first_inventory, sync_fixture.first_artifacts
    )
    Registry.load(state).remove("root-a")

    assert drive_rag.main(["--state-root", str(state), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == "INDEX_STALE"


def test_cli_status_validates_corrupt_manifest_before_configuration(
    tmp_path, capsys
):
    state = ensure_state_root(tmp_path / "state")
    (state / "manifests" / "current.json").write_text("{", encoding="utf-8")

    assert drive_rag.main(["--state-root", str(state), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == "STATE_READ_FAILED"


def test_cli_status_rejects_symlink_manifest_before_configuration(
    tmp_path, capsys
):
    state = ensure_state_root(tmp_path / "state")
    (state / "manifests" / "current.json").symlink_to(tmp_path / "missing-manifest")

    assert drive_rag.main(["--state-root", str(state), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == "INVALID_STATE"


def test_cli_status_accepts_persisted_default_manifest_as_pristine(
    tmp_path, capsys
):
    state = ensure_state_root(tmp_path / "state")
    _write_cli_input(state / "manifests" / "current.json", Manifest.empty().to_dict())

    assert drive_rag.main(["--state-root", str(state), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == "CONFIGURATION_REQUIRED"


@pytest.mark.parametrize("subtree", ["mirrors", "objects"])
def test_cli_status_rejects_orphan_committed_files_with_empty_registry(
    tmp_path, capsys, subtree
):
    state = ensure_state_root(tmp_path / "state")
    orphan = state / subtree / "orphan" / "payload"
    orphan.parent.mkdir()
    orphan.write_text("orphan", encoding="utf-8")

    assert drive_rag.main(["--state-root", str(state), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == "INDEX_STALE"


def test_cli_status_rejects_orphan_index_state_with_empty_registry(
    tmp_path, capsys
):
    state = ensure_state_root(tmp_path / "state")
    (state / "chroma" / "orphan.sqlite").write_bytes(b"orphan")

    assert drive_rag.main(["--state-root", str(state), "status"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error"]["code"] == "INDEX_STALE"


def test_cli_recover_and_query_fail_closed_on_pending_journal(
    sync_fixture, monkeypatch, capsys
):
    state = sync_fixture.engine.state_root

    def inject(phase):
        if phase == "planned":
            raise RuntimeError("injected")

    interrupted = SyncEngine.open(
        state, sync_fixture.embedder, phase_callback=inject
    )
    with pytest.raises(RuntimeError):
        interrupted.apply(sync_fixture.first_inventory, sync_fixture.first_artifacts)
    monkeypatch.setattr(drive_rag, "FastEmbedE5", lambda _state: sync_fixture.embedder)
    assert drive_rag.main(
        ["--state-root", str(state), "query", "--question", "policy"]
    ) == 2
    query_result = json.loads(capsys.readouterr().out)
    assert query_result["error"]["code"] == "INDEX_STALE"

    assert drive_rag.main(["--state-root", str(state), "sync", "recover"]) == 0
    recover_result = json.loads(capsys.readouterr().out)
    assert recover_result["status"] == "SYNC_OK_CHANGED"


def test_cli_sync_apply_rejects_inventory_outside_private_state(
    sync_fixture, tmp_path, monkeypatch, capsys
):
    state = sync_fixture.engine.state_root
    outside_inventory = tmp_path / "outside-inventory.json"
    artifacts_path = state / "staging" / "artifacts.json"
    _write_cli_input(outside_inventory, sync_fixture.first_inventory.to_dict())
    _write_cli_input(artifacts_path, sync_fixture.first_artifacts.to_dict())
    monkeypatch.setattr(drive_rag, "FastEmbedE5", lambda _state: sync_fixture.embedder)

    assert drive_rag.main(
        [
            "--state-root",
            str(state),
            "sync",
            "apply",
            "--inventory",
            str(outside_inventory),
            "--artifacts",
            str(artifacts_path),
        ]
    ) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "UNSAFE_PATH"
    assert sync_fixture.engine.manifest().files == {}
