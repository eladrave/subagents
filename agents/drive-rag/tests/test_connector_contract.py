import json
from pathlib import Path
import re

from drive_rag_lib.inventory import load_inventory


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def test_skill_blocks_deletion_on_incomplete_listing(asset_source: Path):
    skill = _read(asset_source / "skills" / "drive-rag" / "SKILL.md")
    normalized = _normalized(skill)

    assert "INVENTORY_INCOMPLETE" in skill
    assert "must not call `sync apply` with removals" in normalized
    for failure in (
        "partial response",
        "every subfolder",
        "every page",
        "continuation token",
    ):
        assert failure in normalized


def test_skill_defines_exact_inventory_schema(asset_source: Path):
    skill = _read(asset_source / "skills" / "drive-rag" / "SKILL.md")

    for field in (
        "file_id",
        "name",
        "mime_type",
        "revision",
        "modified_time",
        "drive_url",
        "checksum",
        "size",
        "native_kind",
        "root_id",
        "parent_ids",
        "parts",
    ):
        assert f"`{field}`" in skill


def test_skill_requires_local_connector_artifacts_or_preserves_previous_version(
    asset_source: Path,
):
    skill = _read(asset_source / "skills" / "drive-rag" / "SKILL.md")
    normalized = _normalized(skill)

    assert "absolute files beneath the private run staging root" in normalized
    assert "CONNECTOR_OUTPUT_UNSUPPORTED" in skill
    assert "opaque reference" in normalized
    assert "keep the previous committed version" in normalized
    assert (
        "never transcribe binary bytes through prompts or shell arguments" in normalized
    )


def test_skill_uses_the_actual_google_drive_connector_argument_shapes(
    asset_source: Path,
):
    skill = _normalized(_read(asset_source / "skills" / "drive-rag" / "SKILL.md"))

    assert "list_folder(url=<canonical folder URL>, top_k=<bounded limit>)" in skill
    assert (
        "fetch(url=<canonical Drive file URL>, download_raw_file=True)" in skill
    )
    assert "export_file(id=<file ID>, mime_type=\"application/pdf\")" in skill
    assert "only after the correct supported invocation" in skill


def test_skill_uses_authoritative_metadata_for_inventory_and_revision_fencing(
    asset_source: Path,
):
    skill = _normalized(_read(asset_source / "skills" / "drive-rag" / "SKILL.md"))

    assert (
        'get_file_metadata(fileId=<exact file ID>, fields="id,name,mimeType,'
        'modifiedTime,version,headRevisionId,md5Checksum,size,parents,webViewLink")'
        in skill
    )
    assert "`list_folder` only establishes child discovery and completeness" in skill
    assert "every listed item" in skill
    assert "pre-materialization metadata" in skill
    assert "post-materialization metadata" in skill
    assert "changed during materialization" in skill
    for mapping in (
        "id -> file_id",
        "name -> name",
        "mimeType -> mime_type",
        "modifiedTime -> modified_time",
        "version/headRevisionId -> revision",
        "md5Checksum -> checksum",
        "size -> size",
        "parents -> parent_ids",
        "webViewLink -> drive_url",
    ):
        assert mapping in skill


def test_skill_accepts_normalized_metadata_and_revision_history_fallback(
    asset_source: Path,
):
    skill = _normalized(_read(asset_source / "skills" / "drive-rag" / "SKILL.md"))

    assert "list_file_revisions(fileId=<exact file ID>)" in skill
    assert "currentRevisionId" in skill
    assert "opaque revision" in skill
    for mapping in (
        "title -> name",
        "mime_type -> mime_type",
        "modified_time -> modified_time",
        "parent_ids -> parent_ids",
        "url -> drive_url",
    ):
        assert mapping in skill
    assert "matching pre/post revision" in skill


def test_skill_defines_safe_partial_index_contract(asset_source: Path):
    skill = _normalized(_read(asset_source / "skills" / "drive-rag" / "SKILL.md"))
    agent = _normalized(_read(asset_source / "agents" / "drive_rag.toml"))

    for contract in (skill, agent):
        assert "PARTIAL_INDEX" in contract
        assert "partial coverage" in contract
        assert "never authorize deletion" in contract
    assert "preserve unobserved committed files" in skill
    assert "coverage warning" in skill


def test_sync_contract_has_bounded_progress_and_no_change_fast_path(
    asset_source: Path,
):
    skill = _normalized(_read(asset_source / "skills" / "drive-rag" / "SKILL.md"))
    agent = _normalized(_read(asset_source / "agents" / "drive_rag.toml"))

    for contract in (skill, agent):
        assert "no-change fast path" in contract
        assert "at least once every 60 seconds" in contract
        assert "must not inspect implementation source" in contract
    assert "concurrent batches" in skill
    assert "zero downloads" in skill
    assert "empty artifact set" in skill
    assert "immediately call `sync apply`" in skill


def test_skill_keeps_recursive_folders_out_of_remote_inventory_files(
    asset_source: Path,
    tmp_path: Path,
):
    skill = _read(asset_source / "skills" / "drive-rag" / "SKILL.md")
    normalized = _normalized(skill)
    agent = _normalized(_read(asset_source / "agents" / "drive_rag.toml"))

    assert (
        "record subfolders only in the visited traversal/path graph and "
        "completeness proof"
    ) in normalized
    assert "recurse into each folder ID exactly once" in normalized
    assert "never emit a folder entry in `RemoteInventory.files`" in normalized
    assert (
        "Only supported native Docs, Sheets, and Slides and non-folder files "
        "enter `RemoteInventory.files`"
    ) in normalized
    assert "paths derived through the visited folder graph" in normalized
    assert "completeness still requires every discovered folder" in normalized

    example = re.search(
        r"Nested-folder inventory example:\s*```json\s*(\{.*?\})\s*```",
        skill,
        flags=re.DOTALL,
    )
    assert example is not None
    inventory = json.loads(example.group(1))
    example_path = tmp_path / "nested-inventory.json"
    example_path.write_text(example.group(1), encoding="utf-8")
    loaded = load_inventory(example_path)

    assert inventory["schema_version"] == "1"
    assert loaded.complete is True
    assert [item.file_id for item in loaded.files] == ["file-child"]
    assert inventory["complete"] is True
    assert inventory["root_ids"] == ["folder-root"]
    assert [item["file_id"] for item in inventory["files"]] == ["file-child"]
    assert inventory["files"][0]["paths"] == [
        {
            "root_id": "folder-root",
            "parent_ids": ["folder-root", "folder-nested"],
            "parts": ["Nested", "child.txt"],
        }
    ]
    assert "never emit folders in `RemoteInventory.files`" in agent


def test_skill_exports_native_types_as_pdf_and_indexes_structured_content(
    asset_source: Path,
):
    skill = _read(asset_source / "skills" / "drive-rag" / "SKILL.md")

    for kind in ("Google Docs", "Google Sheets", "Google Slides"):
        assert kind in skill
    assert "application/pdf" in skill
    assert "structured content" in skill


def test_skill_onboards_multiple_folders_and_reconciles_remove_or_disable(
    asset_source: Path,
):
    skill = _read(asset_source / "skills" / "drive-rag" / "SKILL.md")
    normalized = _normalized(skill)

    assert "one or more Google Drive folder URLs" in skill
    assert "registry enable" in skill
    assert "registry disable" in skill
    assert "registry rename" in skill
    assert "registry remove" in skill
    assert "Only report removal or disablement complete after" in normalized
    assert "queries are `INDEX_STALE` until reconciliation succeeds" in normalized


def test_schedule_is_hourly_local_and_all_folders(asset_source: Path):
    skill = _normalized(_read(asset_source / "skills" / "drive-rag" / "SKILL.md"))

    assert "FREQ=HOURLY;INTERVAL=1" in skill
    assert "local project mode" in skill
    assert "every enabled folder" in skill
    assert "NOT_SCHEDULED" in skill
    assert "one scheduled task" in skill
    assert "schedule record --task-id" in skill
    assert "schedule show" in skill
    assert "schedule clear" in skill
    assert "Do not create a schedule record" in skill
    assert "query task management by that exact task ID" in skill
    assert "missing, disabled, or mismatched" in skill
    assert "enumerate matching local-project scheduled tasks before creation" in skill


def test_skill_contains_the_exact_scheduled_prompt(asset_source: Path):
    skill = _read(asset_source / "skills" / "drive-rag" / "SKILL.md")
    expected = """Spawn the drive_rag subagent in sync mode. Invoke the $drive-rag Skill explicitly.
Synchronize every enabled configured Google Drive folder into the shared local
state. Prove recursive inventory completeness before any deletion, export native
Google Docs, Sheets, and Slides as PDF, update ChromaDB, and report only status,
counts, changed identities, and actionable failures. If no folder is configured,
return CONFIGURATION_REQUIRED. If connector enumeration is incomplete but every
discovered file has a stable revision, perform deletion-free partial upserts and
return PARTIAL_INDEX with a coverage warning. Never authorize deletion from a
partial inventory; preserve unobserved committed mirror and index records."""

    assert expected in skill


def test_agent_exposes_materialization_and_schedule_failures(asset_source: Path):
    agent = _read(asset_source / "agents" / "drive_rag.toml")

    assert "CONNECTOR_OUTPUT_UNSUPPORTED" in agent
    assert "FREQ=HOURLY;INTERVAL=1" in agent
    assert "one hourly" in agent
    assert "do not claim a schedule exists" in agent


def test_agent_and_skill_distinguish_interactive_query_onboarding_from_scheduled_sync(
    asset_source: Path,
):
    agent = _normalized(_read(asset_source / "agents" / "drive_rag.toml"))
    skill = _normalized(_read(asset_source / "skills" / "drive-rag" / "SKILL.md"))

    for contract in (agent, skill):
        assert "invocation context" in contract
        assert "interactive" in contract
        assert "unattended" in contract
        assert "ordinary interactive Drive-relevant query" in contract
        assert "ask the parent or user for one or more Google Drive folder URLs" in contract
        assert "enter onboarding" in contract
        assert "scheduled sync" in contract
        assert "return `CONFIGURATION_REQUIRED` without prompting" in contract


def test_managed_parent_guidance_routes_interactive_configuration_required_to_onboarding(
    asset_source: Path,
):
    installer = _read(asset_source / "install.py")
    normalized = _normalized(installer)

    assert "ordinary interactive Drive-relevant query" in normalized
    assert "CONFIGURATION_REQUIRED" in installer
    assert "ask the user for one or more Google Drive folder URLs" in normalized
    assert "resume the `drive_rag` subagent in onboarding mode" in normalized
    assert "scheduled or otherwise unattended sync" in normalized
    assert "must not prompt" in normalized


def test_skill_exposes_content_free_unsupported_index_status(
    asset_source: Path,
):
    skill = _normalized(_read(asset_source / "skills" / "drive-rag" / "SKILL.md"))

    assert "index status" in skill
    assert "UNSUPPORTED_FORMAT" in skill
    assert "unindexed" in skill
    assert "supported empty" in skill
