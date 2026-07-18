import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from drive_rag_lib.models import FolderConfig
from drive_rag_lib.paths import ensure_state_root, resolve_below
from drive_rag_lib.protocol import DriveRagError, read_json
from drive_rag_lib.registry import Registry


def test_registry_supports_multiple_folders(tmp_path: Path):
    state = ensure_state_root(tmp_path / "state")
    registry = Registry.load(state)
    registry.add(
        FolderConfig(
            "folder-a",
            "https://drive.google.com/drive/folders/folder-a",
            "Alpha",
            True,
        )
    )
    registry.add(
        FolderConfig(
            "folder-b",
            "https://drive.google.com/drive/folders/folder-b",
            "Beta",
            True,
        )
    )
    assert [item.alias for item in Registry.load(state).list()] == ["Alpha", "Beta"]


def test_registry_mutations_persist(tmp_path: Path):
    state = ensure_state_root(tmp_path / "state")
    registry = Registry.load(state)
    registry.add(
        FolderConfig(
            "folder-a",
            "https://drive.google.com/drive/folders/folder-a",
            "Alpha",
            True,
        )
    )
    registry.add(
        FolderConfig(
            "folder-b",
            "https://drive.google.com/drive/folders/folder-b",
            "Beta",
            True,
        )
    )

    registry.set_enabled("folder-a", False)
    disabled = Registry.load(state).list()
    assert next(item for item in disabled if item.folder_id == "folder-a").enabled is False

    registry = Registry.load(state)
    registry.set_enabled("Alpha", True)
    registry.rename("folder-a", "Gamma")
    registry.remove("folder-b")
    assert Registry.load(state).list() == [
        FolderConfig(
            "folder-a",
            "https://drive.google.com/drive/folders/folder-a",
            "Gamma",
            True,
        )
    ]


def test_registry_rejects_duplicate_alias(tmp_path: Path):
    registry = Registry.load(ensure_state_root(tmp_path / "state"))
    registry.add(
        FolderConfig(
            "folder-a",
            "https://drive.google.com/drive/folders/folder-a",
            "Shared",
            True,
        )
    )
    with pytest.raises(DriveRagError, match="alias"):
        registry.add(
            FolderConfig(
                "folder-b",
                "https://drive.google.com/drive/folders/folder-b",
                "Shared",
                True,
            )
        )


def test_registry_rejects_casefolded_duplicate_aliases(tmp_path: Path):
    registry = Registry.load(ensure_state_root(tmp_path / "state"))
    registry.add(
        FolderConfig(
            "folder-a",
            "https://drive.google.com/drive/folders/folder-a",
            "Finance",
            True,
        )
    )

    with pytest.raises(DriveRagError, match="alias") as error:
        registry.add(
            FolderConfig(
                "folder-b",
                "https://drive.google.com/drive/folders/folder-b",
                "finance",
                True,
            )
        )

    assert error.value.code == "DUPLICATE_ALIAS"


@pytest.mark.parametrize(
    "alias",
    (".", "..", "Finance/2026", r"Finance\2026", "Finance\n2026", "Finance\u200b"),
)
def test_registry_rejects_path_unsafe_or_control_aliases(
    tmp_path: Path, alias: str
):
    registry = Registry.load(ensure_state_root(tmp_path / "state"))

    with pytest.raises(DriveRagError, match="alias") as error:
        registry.add(
            FolderConfig(
                "folder-a",
                "https://drive.google.com/drive/folders/folder-a",
                alias,
                True,
            )
        )

    assert error.value.code == "INVALID_ALIAS"


def test_registry_rename_rejects_casefolded_conflict_without_mutation(tmp_path: Path):
    registry = Registry.load(ensure_state_root(tmp_path / "state"))
    for folder_id, alias in (("folder-a", "Finance"), ("folder-b", "Legal")):
        registry.add(
            FolderConfig(
                folder_id,
                f"https://drive.google.com/drive/folders/{folder_id}",
                alias,
                True,
            )
        )
    original = registry.path.read_bytes()

    with pytest.raises(DriveRagError, match="alias") as error:
        registry.rename("folder-b", "finance")

    assert error.value.code == "DUPLICATE_ALIAS"
    assert registry.path.read_bytes() == original


@pytest.mark.parametrize(
    "aliases",
    (("Finance", "finance"), ("Finance/2026", "Legal"), ("Finance\u200b", "Legal")),
)
def test_registry_load_rejects_corrupt_or_ambiguous_aliases(
    tmp_path: Path, aliases: tuple[str, str]
):
    state = ensure_state_root(tmp_path / "state")
    payload = {
        "schema_version": "1",
        "folders": [
            {
                "folder_id": folder_id,
                "url": f"https://drive.google.com/drive/folders/{folder_id}",
                "alias": alias,
                "enabled": True,
            }
            for folder_id, alias in zip(("folder-a", "folder-b"), aliases, strict=True)
        ],
    }
    (state / "config" / "folders.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(DriveRagError, match="alias"):
        Registry.load(state)


def test_registry_rejects_malformed_url_as_typed_error(tmp_path: Path):
    registry = Registry.load(ensure_state_root(tmp_path / "state"))
    with pytest.raises(DriveRagError, match="folder URL") as error:
        registry.add(
            FolderConfig(
                "folder-a",
                "https://[drive.google.com/drive/folders/folder-a",
                "Alpha",
                True,
            )
        )
    assert error.value.code == "INVALID_FOLDER_URL"


def test_registry_cli_maps_malformed_url_to_exit_two(
    tmp_path: Path, asset_source: Path
):
    command = [
        sys.executable,
        str(asset_source / "skills" / "drive-rag" / "scripts" / "drive_rag.py"),
        "--state-root",
        str(tmp_path / "state"),
        "registry",
        "add",
        "--folder-id",
        "folder-a",
        "--url",
        "https://[drive.google.com/drive/folders/folder-a",
        "--alias",
        "Alpha",
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["error"]["code"] == "INVALID_FOLDER_URL"


def test_registry_cli_rejects_casefolded_alias_conflict(
    tmp_path: Path, asset_source: Path
):
    script = asset_source / "skills" / "drive-rag" / "scripts" / "drive_rag.py"
    state = tmp_path / "state"
    common = [sys.executable, str(script), "--state-root", str(state), "registry"]
    first = subprocess.run(
        [
            *common,
            "add",
            "--folder-id",
            "folder-a",
            "--url",
            "https://drive.google.com/drive/folders/folder-a",
            "--alias",
            "Finance",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    conflict = subprocess.run(
        [
            *common,
            "add",
            "--folder-id",
            "folder-b",
            "--url",
            "https://drive.google.com/drive/folders/folder-b",
            "--alias",
            "finance",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode == 0
    assert conflict.returncode == 2
    assert json.loads(conflict.stdout)["error"]["code"] == "DUPLICATE_ALIAS"


def test_resolve_below_rejects_traversal_and_symlink_escape(tmp_path: Path):
    state = ensure_state_root(tmp_path / "state")
    with pytest.raises(DriveRagError, match="outside"):
        resolve_below(state, state / ".." / "escape")
    outside = tmp_path / "outside"
    outside.mkdir()
    (state / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DriveRagError, match="outside"):
        resolve_below(state, state / "link" / "payload")


def test_resolve_below_rejects_internal_traversal(tmp_path: Path):
    state = ensure_state_root(tmp_path / "state")
    with pytest.raises(DriveRagError, match="traversal"):
        resolve_below(state, state / "nested" / ".." / "payload")


def test_resolve_below_rejects_symlinks_that_stay_inside_state(tmp_path: Path):
    state = ensure_state_root(tmp_path / "state")
    (state / "link").symlink_to(state / "objects", target_is_directory=True)
    with pytest.raises(DriveRagError, match="symlink"):
        resolve_below(state, state / "link" / "payload")


def test_ensure_state_root_rejects_symlinked_required_directory(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "redirect").mkdir()
    (state / "config").symlink_to(state / "redirect", target_is_directory=True)
    with pytest.raises(DriveRagError, match="symlink"):
        ensure_state_root(state)


def test_state_root_has_exact_private_directory_layout(tmp_path: Path):
    state = ensure_state_root(tmp_path / "state")
    expected = {
        "config",
        "manifests",
        "mirrors",
        "objects",
        "chroma",
        "models",
        "journal",
        "logs",
        "staging",
    }
    assert {path.name for path in state.iterdir()} == expected
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert all(
        path.is_dir()
        and not path.is_symlink()
        and stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in state.iterdir()
    )


def test_registry_is_schema_one_and_private(tmp_path: Path):
    state = ensure_state_root(tmp_path / "state")
    Registry.load(state).add(
        FolderConfig(
            "folder-a",
            "https://drive.google.com/drive/folders/folder-a",
            "Alpha",
            True,
        )
    )
    path = state / "config" / "folders.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert read_json(path)["schema_version"] == "1"
