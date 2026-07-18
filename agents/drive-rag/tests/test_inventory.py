from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from drive_rag_lib.inventory import load_inventory, load_manifest, plan_sync, prove_complete
from drive_rag_lib.models import (
    Manifest,
    ManifestFile,
    RemoteFile,
    RemoteInventory as RemoteInventoryModel,
    RemotePath,
)
from drive_rag_lib.protocol import DriveRagError


def RemoteInventory(
    run_id,
    complete,
    root_ids,
    files,
    incomplete_reason,
    *,
    generated_at=None,
):
    if generated_at is None:
        generated_at = (
            "2026-07-18T10:00:00Z"
            if run_id == "old"
            else "2026-07-18T11:00:00Z"
        )
    return RemoteInventoryModel(
        run_id,
        complete,
        root_ids,
        files,
        incomplete_reason,
        generated_at,
    )


def remote(file_id, revision, roots):
    paths = tuple(RemotePath(root, (root,), ("Roadmap.pdf",)) for root in roots)
    return RemoteFile(
        file_id,
        "Roadmap.pdf",
        "application/pdf",
        revision,
        "2026-07-18T10:00:00Z",
        f"https://drive.google.com/file/d/{file_id}/view",
        None,
        10,
        paths,
        None,
    )


def chunk_id(file_id="file-a", character="a"):
    return f"{file_id}:{character * 64}"


def test_incomplete_inventory_cannot_plan_deletions():
    inventory = RemoteInventory(
        "run-1",
        False,
        ("root-a",),
        (remote("file-a", "2", ("root-a",)),),
        "truncated",
    )
    with pytest.raises(DriveRagError, match="complete"):
        prove_complete(inventory, {"root-a"})


def test_partial_inventory_plans_discovered_upserts_without_deletions():
    previous = RemoteInventory(
        "old",
        True,
        ("root-a",),
        (remote("file-old", "1", ("root-a",)),),
        None,
        generated_at="2026-07-18T10:00:00Z",
    )
    discovered = remote("file-new", "opaque-revision", ("root-a",))
    partial = RemoteInventory(
        "partial",
        False,
        ("root-a",),
        (discovered,),
        "connector omitted completeness marker",
        generated_at="2026-07-18T11:00:00Z",
    )

    plan = plan_sync(partial, Manifest.from_remote(previous), {"root-a"})

    assert plan.downloads == (discovered,)
    assert plan.deleted_file_ids == ()
    assert "file-old" not in plan.target_paths


def test_partial_inventory_never_deletes_unobserved_committed_file():
    previous = RemoteInventory(
        "old",
        True,
        ("root-a",),
        (remote("file-old", "1", ("root-a",)),),
        None,
        generated_at="2026-07-18T10:00:00Z",
    )
    partial = RemoteInventory(
        "partial",
        False,
        ("root-a",),
        (),
        "listing may be truncated",
        generated_at="2026-07-18T11:00:00Z",
    )

    plan = plan_sync(partial, Manifest.from_remote(previous), {"root-a"})

    assert plan.downloads == ()
    assert plan.deleted_file_ids == ()


def test_overlapping_roots_download_changed_file_once():
    inventory = RemoteInventory(
        "run-1",
        True,
        ("root-a", "root-b"),
        (remote("file-a", "2", ("root-a", "root-b")),),
        None,
    )
    plan = plan_sync(inventory, Manifest.empty())
    assert [item.file_id for item in plan.downloads] == ["file-a"]
    assert len(plan.target_paths["file-a"]) == 2


def test_missing_remote_file_plans_vector_deletion():
    previous = RemoteInventory(
        "old",
        True,
        ("root-a",),
        (remote("file-a", "1", ("root-a",)),),
        None,
    )
    plan = plan_sync(
        RemoteInventory("new", True, ("root-a",), (), None),
        Manifest.from_remote(previous),
    )
    assert plan.deleted_file_ids == ("file-a",)


def test_direct_plan_rejects_inventory_that_omits_a_manifest_root():
    previous = RemoteInventory(
        "old",
        True,
        ("root-a", "root-b"),
        (remote("file-a", "1", ("root-a", "root-b")),),
        None,
    )
    incomplete_scope = RemoteInventory("new", True, ("root-a",), (), None)
    with pytest.raises(DriveRagError, match="root") as error:
        plan_sync(incomplete_scope, Manifest.from_remote(previous))
    assert error.value.code == "INVENTORY_INCOMPLETE"


def test_explicit_current_roots_allow_a_legitimate_root_change():
    previous = RemoteInventory(
        "old",
        True,
        ("root-a", "root-b"),
        (remote("file-a", "1", ("root-a", "root-b")),),
        None,
        generated_at="2026-07-18T10:00:00Z",
    )
    current = RemoteInventory(
        "new",
        True,
        ("root-a",),
        (remote("file-a", "1", ("root-a",)),),
        None,
        generated_at="2026-07-18T11:00:00Z",
    )
    plan = plan_sync(current, Manifest.from_remote(previous), {"root-a"})
    assert plan.downloads == ()
    assert plan.target_paths["file-a"] == current.files[0].paths


def test_manifest_without_root_scope_cannot_authorize_deletion():
    committed = Manifest.from_remote(
        RemoteInventory(
            "old",
            True,
            ("root-a",),
            (remote("file-a", "1", ("root-a",)),),
            None,
            generated_at="2026-07-18T10:00:00Z",
        )
    )
    malformed = replace(committed, root_ids=())
    current = RemoteInventory(
        "new",
        True,
        ("root-a",),
        (),
        None,
        generated_at="2026-07-18T11:00:00Z",
    )
    with pytest.raises(DriveRagError, match="manifest root"):
        plan_sync(current, malformed)


def test_manifest_without_freshness_proof_cannot_authorize_deletion():
    committed = Manifest.from_remote(
        RemoteInventory(
            "old",
            True,
            ("root-a",),
            (remote("file-a", "1", ("root-a",)),),
            None,
        )
    )
    current = RemoteInventory("new", True, ("root-a",), (), None)
    with pytest.raises(DriveRagError, match="timestamp"):
        plan_sync(current, replace(committed, last_inventory_generated_at=None))


@pytest.mark.parametrize(
    "generated_at",
    ("", "2026-07-18", "2026-07-18T10:00:00+02:00", "not-a-time"),
)
def test_inventory_generated_at_must_be_rfc3339_utc(generated_at):
    candidate = RemoteInventory(
        "run",
        True,
        (),
        (),
        None,
        generated_at=generated_at,
    )
    with pytest.raises(DriveRagError, match="generated_at"):
        plan_sync(candidate, Manifest.empty())


@pytest.mark.parametrize(
    "generated_at", ("2026-07-18T09:00:00Z", "2026-07-18T10:00:00Z")
)
def test_equal_or_older_inventory_cannot_authorize_reconciliation(generated_at):
    previous = RemoteInventory(
        "old",
        True,
        ("root-a",),
        (remote("file-a", "1", ("root-a",)),),
        None,
        generated_at="2026-07-18T10:00:00Z",
    )
    candidate = RemoteInventory(
        "candidate",
        True,
        ("root-a",),
        (),
        None,
        generated_at=generated_at,
    )
    with pytest.raises(DriveRagError, match="newer") as error:
        plan_sync(candidate, Manifest.from_remote(previous))
    assert error.value.code == "INVENTORY_INCOMPLETE"


def test_path_only_move_does_not_download_again():
    old = remote("file-a", "1", ("root-a",))
    moved = replace(
        old,
        paths=(RemotePath("root-a", ("root-a", "folder-b"), ("Moved.pdf",)),),
    )

    plan = plan_sync(
        RemoteInventory("new", True, ("root-a",), (moved,), None),
        Manifest.from_remote(RemoteInventory("old", True, ("root-a",), (old,), None)),
    )

    assert plan.downloads == ()
    assert plan.unchanged_file_ids == ("file-a",)
    assert plan.target_paths["file-a"] == moved.paths


def test_checksum_change_downloads_even_when_revision_is_unchanged():
    old = replace(remote("file-a", "1", ("root-a",)), checksum="a" * 32)
    changed = replace(old, checksum="b" * 32)
    plan = plan_sync(
        RemoteInventory("new", True, ("root-a",), (changed,), None),
        Manifest.from_remote(RemoteInventory("old", True, ("root-a",), (old,), None)),
    )
    assert plan.downloads == (changed,)


def test_conflicting_duplicate_ids_are_rejected():
    inventory = RemoteInventory(
        "run",
        True,
        ("root-a",),
        (
            remote("file-a", "1", ("root-a",)),
            remote("file-a", "2", ("root-a",)),
        ),
        None,
    )
    with pytest.raises(DriveRagError, match="conflicting duplicate"):
        plan_sync(inventory, Manifest.empty())


def test_matching_duplicate_ids_merge_paths_and_download_once():
    first = remote("file-a", "1", ("root-a",))
    second = replace(
        first,
        paths=(RemotePath("root-b", ("root-b",), ("Roadmap.pdf",)),),
    )
    inventory = RemoteInventory("run", True, ("root-a", "root-b"), (first, second), None)
    plan = plan_sync(inventory, Manifest.empty())
    assert [item.file_id for item in plan.downloads] == ["file-a"]
    assert len(plan.target_paths["file-a"]) == 2


def test_manifest_from_remote_preserves_all_overlapping_root_paths():
    first = remote("file-a", "1", ("root-a",))
    second = replace(
        first,
        paths=(RemotePath("root-b", ("root-b",), ("Roadmap.pdf",)),),
    )
    manifest = Manifest.from_remote(
        RemoteInventory("run", True, ("root-a", "root-b"), (first, second), None)
    )
    assert len(manifest.files["file-a"].paths) == 2


@pytest.mark.parametrize(
    ("candidate", "message"),
    (
        (replace(remote("file-a", "1", ("root-a",)), revision=""), "revision"),
        (replace(remote("file-a", "1", ("root-a",)), drive_url="not a URL"), "URL"),
        (replace(remote("file-a", "1", ("root-a",)), size=-1), "size"),
        (
            replace(
                remote("file-a", "1", ("root-a",)),
                paths=(RemotePath("root-other", ("root-other",), ("Roadmap.pdf",)),),
            ),
            "unknown root",
        ),
    ),
)
def test_invalid_remote_file_is_rejected(candidate, message):
    inventory = RemoteInventory("run", True, ("root-a",), (candidate,), None)
    with pytest.raises(DriveRagError, match=message):
        plan_sync(inventory, Manifest.empty())


@pytest.mark.parametrize(
    "candidate",
    (
        replace(remote("file-a", "2", ("root-a",)), file_id=""),
        replace(remote("file-a", "2", ("root-a",)), name=""),
        replace(remote("file-a", "2", ("root-a",)), mime_type=""),
        replace(remote("file-a", "2", ("root-a",)), revision=""),
        replace(remote("file-a", "2", ("root-a",)), drive_url=""),
        replace(remote("file-a", "2", ("root-a",)), modified_time="2026-07-18"),
        replace(remote("file-a", "2", ("root-a",)), checksum="f" * 64),
        replace(remote("file-a", "2", ("root-a",)), native_kind="drawing"),
        replace(remote("file-a", "2", ("root-a",)), size=True),
        replace(remote("file-a", "2", ("root-a",)), paths=("not-a-path",)),
    ),
)
def test_malformed_complete_inventory_cannot_plan_deletion(candidate):
    previous = RemoteInventory(
        "old",
        True,
        ("root-a",),
        (
            remote("file-a", "1", ("root-a",)),
            remote("file-b", "1", ("root-a",)),
        ),
        None,
    )
    malformed = RemoteInventory(
        "new", True, ("root-a",), (candidate,), None
    )
    with pytest.raises(DriveRagError):
        plan_sync(malformed, Manifest.from_remote(previous))


@pytest.mark.parametrize(
    "candidate",
    (
        replace(
            remote("file-a", "2", ("root-a",)),
            native_kind="document",
        ),
        replace(
            remote("file-a", "2", ("root-a",)),
            mime_type="application/vnd.google-apps.document",
            drive_url="https://docs.google.com/document/d/file-a/edit",
        ),
        replace(
            remote("file-a", "2", ("root-a",)),
            mime_type="application/vnd.google-apps.document",
            drive_url="https://docs.google.com/document/d/file-a/edit",
            native_kind="spreadsheet",
        ),
        replace(
            remote("file-a", "2", ("root-a",)),
            mime_type="application/vnd.google-apps.document",
            drive_url="https://docs.google.com/spreadsheets/d/file-a/edit",
            native_kind="document",
        ),
        replace(remote("file-a", "2", ("root-a",)), mime_type="not a mime"),
    ),
)
def test_mime_native_kind_and_url_must_describe_the_same_resource(candidate):
    with pytest.raises(DriveRagError):
        plan_sync(
            RemoteInventory("run", True, ("root-a",), (candidate,), None),
            Manifest.empty(),
        )


@pytest.mark.parametrize(
    ("native_kind", "mime_type", "resource"),
    (
        ("document", "application/vnd.google-apps.document", "document"),
        ("spreadsheet", "application/vnd.google-apps.spreadsheet", "spreadsheets"),
        ("presentation", "application/vnd.google-apps.presentation", "presentation"),
    ),
)
def test_supported_native_identity_mappings_are_valid(
    native_kind, mime_type, resource
):
    candidate = replace(
        remote("file-a", "2", ("root-a",)),
        mime_type=mime_type,
        drive_url=f"https://docs.google.com/{resource}/d/file-a/edit",
        native_kind=native_kind,
        size=None,
    )
    plan = plan_sync(
        RemoteInventory("run", True, ("root-a",), (candidate,), None),
        Manifest.empty(),
    )
    assert plan.downloads == (candidate,)


def test_remote_file_without_a_reachable_path_is_rejected():
    candidate = replace(remote("file-a", "1", ("root-a",)), paths=())
    inventory = RemoteInventory("run", True, ("root-a",), (candidate,), None)
    with pytest.raises(DriveRagError, match="path"):
        plan_sync(inventory, Manifest.empty())


def test_inventory_complete_flag_must_be_boolean():
    inventory = RemoteInventory("run", "yes", (), (), None)  # type: ignore[arg-type]
    with pytest.raises(DriveRagError, match="complete"):
        plan_sync(inventory, Manifest.empty())


def test_inventory_root_identity_must_match_expected_roots():
    inventory = RemoteInventory("run", True, ("root-a",), (), None)
    with pytest.raises(DriveRagError, match="root") as error:
        prove_complete(inventory, {"root-a", "root-b"})
    assert error.value.code == "INVENTORY_INCOMPLETE"


def test_colliding_names_receive_stable_file_id_suffixes():
    first = remote("file-a", "1", ("root-a",))
    second = remote("file-b", "1", ("root-a",))
    inventory = RemoteInventory("run", True, ("root-a",), (first, second), None)

    plan = plan_sync(inventory, Manifest.empty())

    first_hash = hashlib.sha256(b"file-a").hexdigest()[:8]
    second_hash = hashlib.sha256(b"file-b").hexdigest()[:8]
    assert plan.target_paths["file-a"][0].parts[-1] == f"Roadmap__{first_hash}.pdf"
    assert plan.target_paths["file-b"][0].parts[-1] == f"Roadmap__{second_hash}.pdf"


def test_collisions_use_local_path_even_when_drive_parent_ids_differ():
    first = remote("file-a", "1", ("root-a",))
    second = replace(
        remote("file-b", "1", ("root-a",)),
        paths=(
            RemotePath("root-a", ("root-a", "different-parent"), ("Roadmap.pdf",)),
        ),
    )
    plan = plan_sync(
        RemoteInventory("run", True, ("root-a",), (first, second), None),
        Manifest.empty(),
    )
    assert plan.target_paths["file-a"][0].parts[-1] != "Roadmap.pdf"
    assert plan.target_paths["file-b"][0].parts[-1] != "Roadmap.pdf"


def test_collision_suffix_cannot_collide_with_an_existing_remote_name():
    first = remote("file-a", "1", ("root-a",))
    second = remote("file-b", "1", ("root-a",))
    first_suffix = hashlib.sha256(b"file-a").hexdigest()[:8]
    occupied_name = f"Roadmap__{first_suffix}.pdf"
    third = replace(
        remote("file-c", "1", ("root-a",)),
        name=occupied_name,
        paths=(RemotePath("root-a", ("root-a",), (occupied_name,)),),
    )
    plan = plan_sync(
        RemoteInventory("run", True, ("root-a",), (first, second, third), None),
        Manifest.empty(),
    )
    final_names = [
        path.parts[-1] for paths in plan.target_paths.values() for path in paths
    ]
    assert len(final_names) == len(set(final_names))


def test_suffixing_cannot_create_duplicate_targets_for_the_same_file():
    digest = hashlib.sha256(b"file-a").hexdigest()[:8]
    suffixed_name = f"A__{digest}.pdf"
    first = replace(
        remote("file-a", "1", ("root-a",)),
        name="A.pdf",
        paths=(
            RemotePath("root-a", ("root-a",), ("A.pdf",)),
            RemotePath("root-a", ("root-a",), (suffixed_name,)),
        ),
    )
    second = replace(
        remote("file-b", "1", ("root-a",)),
        name="A.pdf",
        paths=(RemotePath("root-a", ("root-a",), ("A.pdf",)),),
    )
    with pytest.raises(DriveRagError, match="collision|duplicate"):
        plan_sync(
            RemoteInventory("run", True, ("root-a",), (first, second), None),
            Manifest.empty(),
        )


@pytest.mark.parametrize(
    "path",
    (
        RemotePath("root-a", (), ("Roadmap.pdf",)),
        RemotePath("root-a", ("different-root",), ("Roadmap.pdf",)),
        RemotePath("root-a", ("root-a",), ("",)),
        RemotePath("root-a", ("root-a",), (".",)),
        RemotePath("root-a", ("root-a",), ("..",)),
        RemotePath("root-a", ("root-a",), ("folder/name.pdf",)),
        RemotePath("root-a", ("root-a",), ("folder\\name.pdf",)),
    ),
)
def test_malformed_remote_paths_are_rejected(path):
    candidate = replace(remote("file-a", "1", ("root-a",)), paths=(path,))
    with pytest.raises(DriveRagError, match="path"):
        plan_sync(
            RemoteInventory("run", True, ("root-a",), (candidate,), None),
            Manifest.empty(),
        )


@pytest.mark.parametrize(
    "path",
    (
        RemotePath("root-a", ["root-a"], ("Roadmap.pdf",)),  # type: ignore[arg-type]
        RemotePath("root-a", ("root-a", 7), ("Roadmap.pdf",)),  # type: ignore[arg-type]
        RemotePath("root-a", ("root-a",), ["Roadmap.pdf"]),  # type: ignore[arg-type]
        RemotePath("root-a", ("root-a",), (7,)),  # type: ignore[arg-type]
        RemotePath("root-a", ("root-a",), ("Road\x00map.pdf",)),
        RemotePath("root-a", ("root-a",), ("Road\x1fmap.pdf",)),
        RemotePath("root-a", ("root-a",), ("Road\u0085map.pdf",)),
        RemotePath("root-a", ("root-a\u0085",), ("Roadmap.pdf",)),
    ),
)
def test_malformed_direct_path_cannot_authorize_deletion(path):
    previous = RemoteInventory(
        "old",
        True,
        ("root-a",),
        (
            remote("file-a", "1", ("root-a",)),
            remote("file-b", "1", ("root-a",)),
        ),
        None,
    )
    candidate = replace(remote("file-a", "2", ("root-a",)), paths=(path,))
    malformed = RemoteInventory("new", True, ("root-a",), (candidate,), None)
    with pytest.raises(DriveRagError):
        plan_sync(malformed, Manifest.from_remote(previous))


@pytest.mark.parametrize("root_id", ("root\x00-a", "root\u0085-a"))
def test_control_characters_in_root_scope_cannot_authorize_deletion(root_id):
    previous = RemoteInventory(
        "old",
        True,
        (root_id,),
        (remote("file-a", "1", (root_id,)),),
        None,
    )
    current = RemoteInventory("new", True, (root_id,), (), None)
    with pytest.raises(DriveRagError, match="root"):
        plan_sync(current, Manifest.from_remote(previous))


def test_drive_url_must_reference_the_matching_file_id():
    candidate = replace(
        remote("file-a", "1", ("root-a",)),
        drive_url="https://drive.google.com/file/d/different-file/view",
    )
    with pytest.raises(DriveRagError, match="URL"):
        plan_sync(
            RemoteInventory("run", True, ("root-a",), (candidate,), None),
            Manifest.empty(),
        )


def inventory_payload(inventory):
    return {
        "schema_version": "1",
        "run_id": inventory.run_id,
        "complete": inventory.complete,
        "root_ids": list(inventory.root_ids),
        "files": [item.to_dict() for item in inventory.files],
        "incomplete_reason": inventory.incomplete_reason,
        "generated_at": inventory.generated_at,
    }


def test_load_inventory_rejects_an_unsupported_schema(tmp_path: Path):
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps(
            {
                **inventory_payload(RemoteInventory("run", True, (), (), None)),
                "schema_version": "2",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DriveRagError, match="schema"):
        load_inventory(path)


def test_load_inventory_requires_generated_at(tmp_path: Path):
    path = tmp_path / "inventory.json"
    payload = inventory_payload(RemoteInventory("run", True, (), (), None))
    payload.pop("generated_at", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DriveRagError, match="generated_at"):
        load_inventory(path)


@pytest.mark.parametrize(
    "part", ("Road\x00map.pdf", "Road\x7fmap.pdf", "Road\u0085map.pdf")
)
def test_load_inventory_rejects_control_characters_in_path_parts(
    tmp_path: Path, part
):
    candidate = replace(
        remote("file-a", "1", ("root-a",)),
        paths=(RemotePath("root-a", ("root-a",), (part,)),),
    )
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps(
            inventory_payload(
                RemoteInventory("run", True, ("root-a",), (candidate,), None)
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(DriveRagError, match="path"):
        load_inventory(path)


def test_manifest_round_trip_preserves_committed_state():
    path = RemotePath("root-a", ("root-a",), ("Roadmap.pdf",))
    manifest = Manifest(
        {
            "file-a": ManifestFile(
                "file-a", "7", "checksum", "object-hash", (path,), (chunk_id(),)
            )
        },
        "multilingual-e5-small@1",
        "2026-07-18T10:00:00Z",
        "previous failure",
        ("root-a",),
    )
    assert Manifest.from_dict(manifest.to_dict()) == manifest


def test_manifest_requires_explicit_index_status_and_reason_fields():
    path = RemotePath("root-a", ("root-a",), ("Roadmap.pdf",))
    committed = ManifestFile(
        "file-a", "7", None, "a" * 64, (path,), (chunk_id(),)
    )
    payload = committed.to_dict()

    assert payload["index_status"] == "indexed"
    assert payload["index_reason"] is None
    legacy = dict(payload)
    legacy.pop("index_status")
    legacy.pop("index_reason")
    with pytest.raises(DriveRagError, match="missing keys"):
        ManifestFile.from_dict(legacy)


@pytest.mark.parametrize(
    ("index_status", "index_reason", "active_chunks"),
    (
        ("unindexed", None, ()),
        ("unindexed", "UNSUPPORTED_FORMAT", (chunk_id(),)),
        ("indexed", "UNSUPPORTED_FORMAT", ()),
    ),
)
def test_manifest_rejects_inconsistent_index_status(
    index_status, index_reason, active_chunks
):
    committed = ManifestFile(
        "file-a",
        "7",
        None,
        "a" * 64,
        (RemotePath("root-a", ("root-a",), ("file.bin",)),),
        active_chunks,
    )
    object.__setattr__(committed, "index_status", index_status)
    object.__setattr__(committed, "index_reason", index_reason)
    manifest = Manifest(
        {"file-a": committed},
        "model",
        "2026-07-18T10:00:00Z",
        None,
        ("root-a",),
        "2026-07-18T10:00:00Z",
    )

    with pytest.raises(DriveRagError, match="index"):
        plan_sync(
            RemoteInventory(
                "new",
                True,
                ("root-a",),
                (),
                None,
                generated_at="2026-07-18T11:00:00Z",
            ),
            manifest,
        )


def test_load_manifest_rejects_committed_paths_outside_root_scope(tmp_path: Path):
    path = RemotePath("root-a", ("root-a",), ("Roadmap.pdf",))
    malformed = Manifest(
        {
            "file-a": ManifestFile(
                "file-a", "7", None, "a" * 64, (path,), (chunk_id(),)
            )
        },
        "model",
        "run",
        None,
        (),
        "2026-07-18T10:00:00Z",
    )
    source = tmp_path / "manifest.json"
    source.write_text(
        json.dumps({"schema_version": "1", **malformed.to_dict()}), encoding="utf-8"
    )
    with pytest.raises(DriveRagError, match="manifest root"):
        load_manifest(source)


@pytest.mark.parametrize(
    "manifest_file",
    (
        ManifestFile(
            "file-a",
            "7",
            None,
            "a" * 64,
            (RemotePath("root-a", ("root-a",), ("..",)),),
            (chunk_id(),),
        ),
        ManifestFile(
            "file-a",
            "7",
            None,
            "not-a-sha256",
            (RemotePath("root-a", ("root-a",), ("Roadmap.pdf",)),),
            (chunk_id(),),
        ),
    ),
)
def test_load_manifest_rejects_unsafe_paths_and_malformed_hashes(
    tmp_path: Path, manifest_file
):
    malformed = Manifest(
        {"file-a": manifest_file},
        "model",
        "run",
        None,
        ("root-a",),
        "2026-07-18T10:00:00Z",
    )
    source = tmp_path / "manifest.json"
    source.write_text(
        json.dumps({"schema_version": "1", **malformed.to_dict()}), encoding="utf-8"
    )
    with pytest.raises(DriveRagError):
        load_manifest(source)


@pytest.mark.parametrize(
    ("manifest_key", "committed", "model_identity"),
    (
        (
            "",
            ManifestFile("", "7", None, "a" * 64, (), ()),
            "model",
        ),
        (
            "file-a",
            ManifestFile("file-b", "7", None, "a" * 64, (), ()),
            "model",
        ),
        (
            "file-a",
            ManifestFile("file-a", "", None, "a" * 64, (), ()),
            "model",
        ),
        (
            "file-a",
            ManifestFile("file-a", "7", None, "a" * 64, (), ("",)),
            "model",
        ),
        (
            "file-a",
            ManifestFile(
                "file-a", "7", None, "a" * 64, (), (chunk_id(), chunk_id())
            ),
            "model",
        ),
        (
            "file-a",
            ManifestFile(
                "file-a", "7", None, "a" * 64, (), (chunk_id("file-b"),)
            ),
            "model",
        ),
        (
            "file-a",
            ManifestFile("file-a", "7", None, "a" * 64, (), ("file-a:short",)),
            "model",
        ),
        (
            "file-a",
            ManifestFile("file-a", "7", None, "a" * 64, (), (chunk_id(),)),
            None,
        ),
        (
            "file-a",
            ManifestFile("file-a", "7", None, "a" * 64, (), (chunk_id(),)),
            "",
        ),
        (
            "file-a",
            ManifestFile("file-a", "7", None, 42, (), ()),  # type: ignore[arg-type]
            "model",
        ),
        (
            "file-a",
            ManifestFile(
                "file-a", "7", None, "a" * 64, (), ([],)  # type: ignore[arg-type]
            ),
            "model",
        ),
    ),
)
def test_malformed_manifest_identity_cannot_emit_deletions(
    manifest_key, committed, model_identity
):
    manifest = Manifest(
        {manifest_key: committed},
        model_identity,
        "old",
        None,
        ("root-a",),
        "2026-07-18T10:00:00Z",
    )
    current = RemoteInventory("new", True, ("root-a",), (), None)
    with pytest.raises(DriveRagError):
        plan_sync(current, manifest)


def test_non_manifest_entry_cannot_authorize_deletion():
    malformed = Manifest(
        {"file-a": object()},  # type: ignore[dict-item]
        "model",
        "old",
        None,
        ("root-a",),
        "2026-07-18T10:00:00Z",
    )
    with pytest.raises(DriveRagError):
        plan_sync(RemoteInventory("new", True, ("root-a",), (), None), malformed)


def test_pathless_committed_file_cannot_authorize_deletion():
    manifest = Manifest(
        {"file-a": ManifestFile("file-a", "7", None, "a" * 64, (), ())},
        None,
        "old",
        None,
        ("root-a",),
        "2026-07-18T10:00:00Z",
    )
    with pytest.raises(DriveRagError, match="path"):
        plan_sync(RemoteInventory("new", True, ("root-a",), (), None), manifest)


def test_duplicate_remote_paths_produce_one_unique_target():
    path = RemotePath("root-a", ("root-a",), ("Roadmap.pdf",))
    source = replace(
        remote("file-a", "1", ("root-a",)),
        paths=(path, path),
    )
    plan = plan_sync(
        RemoteInventory("run", True, ("root-a",), (source,), None),
        Manifest.empty(),
    )
    assert plan.target_paths["file-a"] == (path,)


def test_same_file_local_targets_are_unique_across_distinct_parent_ids():
    source = replace(
        remote("file-a", "1", ("root-a",)),
        paths=(
            RemotePath("root-a", ("root-a", "parent-a"), ("Roadmap.pdf",)),
            RemotePath("root-a", ("root-a", "parent-b"), ("Roadmap.pdf",)),
        ),
    )
    plan = plan_sync(
        RemoteInventory("run", True, ("root-a",), (source,), None),
        Manifest.empty(),
    )
    local_targets = [
        (path.root_id, path.parts) for path in plan.target_paths["file-a"]
    ]
    assert len(local_targets) == len(set(local_targets)) == 1


def run_cli(asset_source: Path, state: Path, *arguments: str):
    return subprocess.run(
        [
            sys.executable,
            str(asset_source / "skills" / "drive-rag" / "scripts" / "drive_rag.py"),
            "--state-root",
            str(state),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_inventory_validate_accepts_partial_input_without_deletion_authority(
    tmp_path: Path, asset_source: Path
):
    source = tmp_path / "partial.json"
    source.write_text(
        json.dumps(
            inventory_payload(
                RemoteInventory("partial", False, (), (), "connector truncated")
            )
        ),
        encoding="utf-8",
    )
    completed = run_cli(
        asset_source, tmp_path / "state", "inventory", "validate", "--input", str(source)
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["status"] == "PARTIAL_INDEX"
    assert result["coverage"] == "partial"
    assert result["coverage_reason"] == "connector truncated"


def test_sync_plan_writes_private_atomic_json_and_reports_sha256(
    tmp_path: Path, asset_source: Path
):
    source = tmp_path / "inventory.json"
    source.write_text(
        json.dumps(inventory_payload(RemoteInventory("run", True, (), (), None))),
        encoding="utf-8",
    )
    output = tmp_path / "state" / "staging" / "plan.json"
    completed = run_cli(
        asset_source,
        tmp_path / "state",
        "sync",
        "plan",
        "--inventory",
        str(source),
        "--output",
        str(output),
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["counts"] == {"deleted": 0, "downloads": 0, "unchanged": 0}
    assert result["output_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == "1"


def test_sync_plan_cannot_overwrite_authoritative_manifest(
    tmp_path: Path, asset_source: Path
):
    source = tmp_path / "inventory.json"
    source.write_text(
        json.dumps(inventory_payload(RemoteInventory("run", True, (), (), None))),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    output = state / "manifests" / "current.json"
    completed = run_cli(
        asset_source,
        state,
        "sync",
        "plan",
        "--inventory",
        str(source),
        "--output",
        str(output),
    )
    assert completed.returncode == 2
    assert not output.exists()
