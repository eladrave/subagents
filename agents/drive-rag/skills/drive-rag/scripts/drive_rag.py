#!/usr/bin/env python3
"""Command-line entry point for deterministic Drive RAG operations."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from drive_rag_lib.inventory import (  # noqa: E402
    load_inventory,
    load_manifest,
    plan_sync,
    validate_inventory_scope,
)
from drive_rag_lib.aliases import alias_key, canonical_alias  # noqa: E402
from drive_rag_lib.extract import (  # noqa: E402
    extract_file,
    extract_native_structured,
    require_bounded_file,
)
from drive_rag_lib.embed import E5_DIMENSION, E5_MODEL_ID, FastEmbedE5  # noqa: E402
from drive_rag_lib.index import ChromaIndex  # noqa: E402
from drive_rag_lib.models import Chunk, FolderConfig, Manifest  # noqa: E402
from drive_rag_lib.paths import ensure_state_root, resolve_below  # noqa: E402
from drive_rag_lib.protocol import (  # noqa: E402
    PARTIAL_INDEX,
    SCHEMA_VERSION,
    DriveRagError,
    atomic_write_json,
    emit_result,
    read_json,
)
from drive_rag_lib.registry import Registry  # noqa: E402
from drive_rag_lib.query import QueryService  # noqa: E402
from drive_rag_lib.schedule import (  # noqa: E402
    clear_schedule,
    content_free_schedule,
    load_schedule,
    record_schedule,
)
from drive_rag_lib.sync import SyncEngine, load_artifact_set  # noqa: E402


class DriveRagArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DriveRagError(message, code="INVALID_ARGUMENTS")


def build_parser() -> argparse.ArgumentParser:
    parser = DriveRagArgumentParser(description="Manage deterministic Drive RAG state")
    parser.add_argument("--state-root", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    registry = commands.add_parser("registry", help="manage configured Drive folders")
    actions = registry.add_subparsers(dest="registry_action", required=True)
    actions.add_parser("list", help="list configured folders")

    add = actions.add_parser("add", help="add a Drive folder")
    add.add_argument("--folder-id", required=True)
    add.add_argument("--url", required=True)
    add.add_argument("--alias", required=True)
    add.add_argument("--disabled", action="store_true")

    for action in ("enable", "disable", "remove"):
        action_parser = actions.add_parser(action)
        action_parser.add_argument("identifier")

    rename = actions.add_parser("rename")
    rename.add_argument("identifier")
    rename.add_argument("alias")

    inventory = commands.add_parser("inventory", help="validate connector inventory")
    inventory_actions = inventory.add_subparsers(dest="inventory_action", required=True)
    validate = inventory_actions.add_parser("validate")
    validate.add_argument("--input", required=True, type=Path)

    sync = commands.add_parser("sync", help="reconcile Drive mirror and index")
    sync_actions = sync.add_subparsers(dest="sync_action", required=True)
    plan = sync_actions.add_parser("plan")
    plan.add_argument("--inventory", required=True, type=Path)
    plan.add_argument("--output", required=True, type=Path)
    apply = sync_actions.add_parser("apply")
    apply.add_argument("--inventory", required=True, type=Path)
    apply.add_argument("--artifacts", required=True, type=Path)
    sync_actions.add_parser("recover")

    schedule = commands.add_parser("schedule", help="record an observed scheduled task")
    schedule_actions = schedule.add_subparsers(dest="schedule_action", required=True)
    record = schedule_actions.add_parser("record")
    record.add_argument("--task-id", required=True)
    schedule_actions.add_parser("show")
    clear = schedule_actions.add_parser("clear")
    clear.add_argument("--task-id", required=True)

    commands.add_parser("status", help="report content-free Drive RAG state")

    extract = commands.add_parser("extract", help="extract a staged Drive artifact")
    extract.add_argument("--descriptor", required=True, type=Path)
    extract.add_argument("--payload", required=True, type=Path)
    extract.add_argument("--structured", type=Path)
    extract.add_argument("--output", required=True, type=Path)
    extract.add_argument("--ocr-languages", default="eng")

    index = commands.add_parser("index", help="manage the persistent vector index")
    index_actions = index.add_subparsers(dest="index_action", required=True)
    upsert = index_actions.add_parser("upsert")
    upsert.add_argument("--input", required=True, type=Path)
    delete_file = index_actions.add_parser("delete-file")
    delete_file.add_argument("--file-id", required=True)
    index_actions.add_parser("rebuild")

    query = commands.add_parser("query", help="retrieve cited Drive evidence")
    query.add_argument("--question", required=True)
    query.add_argument("--folder-alias", action="append", default=[])
    query.add_argument("--limit", type=int, default=8)
    return parser


def run_registry(args: argparse.Namespace) -> None:
    registry = Registry.load(ensure_state_root(args.state_root))
    action = args.registry_action
    if action == "list":
        emit_result(
            "registry.list",
            "ok",
            folders=[folder.to_dict() for folder in registry.list()],
        )
        return
    if action == "add":
        folder = registry.add(
            FolderConfig(args.folder_id, args.url, args.alias, not args.disabled)
        )
    elif action == "enable":
        folder = registry.set_enabled(args.identifier, True)
    elif action == "disable":
        folder = registry.set_enabled(args.identifier, False)
    elif action == "rename":
        folder = registry.rename(args.identifier, args.alias)
    elif action == "remove":
        folder = registry.remove(args.identifier)
    else:
        raise DriveRagError(f"unsupported registry action: {action}")
    emit_result(f"registry.{action}", "ok", folder=folder.to_dict())


def _enabled_root_ids(registry: Registry) -> set[str]:
    return {folder.folder_id for folder in registry.list() if folder.enabled}


def run_inventory(args: argparse.Namespace) -> None:
    state_root = ensure_state_root(args.state_root)
    inventory = load_inventory(args.input)
    validate_inventory_scope(inventory, _enabled_root_ids(Registry.load(state_root)))
    emit_result(
        "inventory.validate",
        "ok" if inventory.complete else PARTIAL_INDEX,
        run_id=inventory.run_id,
        root_count=len(inventory.root_ids),
        file_count=len({remote.file_id for remote in inventory.files}),
        coverage="complete" if inventory.complete else "partial",
        coverage_reason=inventory.incomplete_reason,
    )


def run_sync(args: argparse.Namespace) -> None:
    state_root = ensure_state_root(args.state_root)
    if args.sync_action == "recover":
        embedder = FastEmbedE5(state_root)
        result = SyncEngine.open(state_root, embedder).recover()
        emit_result(
            "sync.recover",
            result.status,
            pending_journal=result.engine.has_pending_journal(),
        )
        return
    if args.sync_action == "apply":
        inventory_path = resolve_below(state_root, args.inventory)
        inventory = load_inventory(inventory_path)
        artifact_path = resolve_below(state_root, args.artifacts)
        artifacts = load_artifact_set(artifact_path)
        embedder = FastEmbedE5(state_root)
        result = SyncEngine.open(state_root, embedder).apply(inventory, artifacts)
        status = result.engine.status()
        emit_result(
            "sync.apply",
            result.status,
            run_id=inventory.run_id,
            counts={
                "indexed_files": status.indexed_files,
                "indexed_chunks": status.indexed_chunks,
            },
            pending_journal=status.pending_journal,
            coverage=status.coverage,
            coverage_reason=status.coverage_reason,
        )
        return
    if args.sync_action != "plan":
        raise DriveRagError(
            f"unsupported sync action: {args.sync_action}",
            code="INVALID_ARGUMENTS",
        )
    inventory = load_inventory(args.inventory)
    enabled_root_ids = _enabled_root_ids(Registry.load(state_root))
    validate_inventory_scope(inventory, enabled_root_ids)
    manifest = load_manifest(state_root / "manifests" / "current.json")
    sync_plan = plan_sync(inventory, manifest, enabled_root_ids)
    staging_root = resolve_below(state_root, state_root / "staging")
    output = resolve_below(staging_root, args.output)
    atomic_write_json(output, {"schema_version": SCHEMA_VERSION, **sync_plan.to_dict()})
    output_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
    emit_result(
        "sync.plan",
        "ok" if inventory.complete else PARTIAL_INDEX,
        run_id=sync_plan.run_id,
        counts={
            "downloads": len(sync_plan.downloads),
            "unchanged": len(sync_plan.unchanged_file_ids),
            "deleted": len(sync_plan.deleted_file_ids),
        },
        output=str(output),
        output_sha256=output_sha256,
        coverage="complete" if inventory.complete else "partial",
        coverage_reason=inventory.incomplete_reason,
    )


class _ModelIdentityOnly:
    def __init__(self, model_id: str, dimension: int) -> None:
        self.model_id = model_id
        self.dimension = dimension


def _contains_live_file_artifacts(*roots: Path) -> bool:
    try:
        return any(
            entry.is_symlink() or not entry.is_dir()
            for root in roots
            for entry in root.rglob("*")
        )
    except OSError as exc:
        raise DriveRagError(
            "could not inspect committed file artifacts",
            code="STATE_READ_FAILED",
        ) from exc


def run_status(args: argparse.Namespace) -> None:
    state_root = ensure_state_root(args.state_root)
    configured_folders = Registry.load(state_root).list()
    model_id = E5_MODEL_ID
    dimension = E5_DIMENSION
    manifest_path = state_root / "manifests" / "current.json"
    if manifest_path.is_symlink():
        raise DriveRagError(
            "committed manifest must not be a symlink",
            code="INVALID_STATE",
        )
    manifest = load_manifest(manifest_path)
    if manifest.model_identity is not None:
        try:
            identity = json.loads(manifest.model_identity)
        except json.JSONDecodeError as exc:
            raise DriveRagError(
                "committed model identity is malformed", code="INVALID_STATE"
            ) from exc
        if (
            not isinstance(identity, dict)
            or set(identity)
            != {"model_id", "dimension", "distance", "schema_version"}
            or not isinstance(identity["model_id"], str)
            or not identity["model_id"].strip()
            or not isinstance(identity["dimension"], int)
            or isinstance(identity["dimension"], bool)
            or identity["dimension"] <= 0
            or identity["distance"] != "cosine"
            or identity["schema_version"] != SCHEMA_VERSION
        ):
            raise DriveRagError(
                "committed model identity is malformed", code="INVALID_STATE"
            )
        model_id = identity["model_id"]
        dimension = identity["dimension"]
    if not configured_folders:
        pending = state_root / "journal" / "pending.json"
        committed_folders = state_root / "manifests" / "folders.json"
        committed_subtrees = (state_root / "mirrors", state_root / "objects")
        chroma_root = state_root / "chroma"
        if pending.exists() or pending.is_symlink():
            raise DriveRagError(
                "pending synchronization requires recovery before status",
                code="INDEX_STALE",
            )
        live_file_artifacts = _contains_live_file_artifacts(*committed_subtrees)
        chroma_exists = any(chroma_root.iterdir())
        pristine = (
            manifest == Manifest.empty()
            and not committed_folders.exists()
            and not committed_folders.is_symlink()
            and not live_file_artifacts
            and not chroma_exists
        )
        if pristine:
            raise DriveRagError(
                "no Drive folders are configured",
                code="CONFIGURATION_REQUIRED",
            )
        if (
            manifest.files
            or manifest.root_ids
            or not manifest.last_success
            or manifest.last_success != manifest.last_inventory_generated_at
            or manifest.last_failure is not None
            or manifest.model_identity is None
            or not committed_folders.exists()
            or committed_folders.is_symlink()
            or live_file_artifacts
            or not chroma_exists
        ):
            raise DriveRagError(
                "committed Drive RAG state exists without configured folders",
                code="INDEX_STALE",
            )
        committed_folders_payload = read_json(committed_folders)
        if committed_folders_payload != {
            "schema_version": SCHEMA_VERSION,
            "folders": [],
        }:
            raise DriveRagError(
                "committed folder state differs from the empty registry",
                code="INDEX_STALE",
            )
        engine = SyncEngine.open(
            state_root,
            _ModelIdentityOnly(model_id, dimension),
            create_index_if_missing=False,
        )
        engine.assert_query_ready()
        raise DriveRagError(
            "no Drive folders are configured",
            code="CONFIGURATION_REQUIRED",
        )
    status = SyncEngine.open(
        state_root, _ModelIdentityOnly(model_id, dimension)
    ).status()
    emit_result("status", "ok", **status.to_dict())


def run_schedule(args: argparse.Namespace) -> None:
    state_root = ensure_state_root(args.state_root)
    if args.schedule_action == "record":
        record = record_schedule(state_root, args.task_id)
        emit_result(
            "schedule.record",
            "ok",
            task_id=record.task_id,
            rrule=record.rrule,
            project_mode=record.project_mode,
            project_path=record.project_path,
            enabled=record.enabled,
        )
        return
    if args.schedule_action == "show":
        record = load_schedule(state_root)
        if record is None:
            emit_result("schedule.show", "ok", configured=False)
        else:
            emit_result(
                "schedule.show",
                "ok",
                configured=True,
                **content_free_schedule(record),
            )
        return
    if args.schedule_action == "clear":
        emit_result(
            "schedule.clear",
            "ok",
            removed=clear_schedule(state_root, args.task_id),
        )
        return
    raise DriveRagError(
        f"unsupported schedule action: {args.schedule_action}",
        code="INVALID_ARGUMENTS",
    )


def run_extract(args: argparse.Namespace) -> None:
    state_root = ensure_state_root(args.state_root)
    descriptor_path = resolve_below(state_root, args.descriptor)
    payload_path = resolve_below(state_root, args.payload)
    output_path = resolve_below(state_root, args.output)
    structured_path = (
        resolve_below(state_root, args.structured) if args.structured is not None else None
    )
    protected_inputs = {descriptor_path, payload_path}
    if structured_path is not None:
        protected_inputs.add(structured_path)
    if output_path in protected_inputs:
        raise DriveRagError(
            "extraction output must not overwrite an input", code="UNSAFE_PATH"
        )

    require_bounded_file(descriptor_path, "descriptor")
    require_bounded_file(payload_path, "payload")
    if structured_path is not None:
        require_bounded_file(structured_path, "structured content")

    descriptor = read_json(descriptor_path)
    expected = {"schema_version", "file_id", "revision", "mime_type", "native_kind"}
    if set(descriptor) != expected or descriptor["schema_version"] != SCHEMA_VERSION:
        raise DriveRagError(
            "descriptor must contain exactly the schema-1 extraction fields",
            code="INVALID_DESCRIPTOR",
        )
    file_id = descriptor["file_id"]
    revision = descriptor["revision"]
    mime_type = descriptor["mime_type"]
    native_kind = descriptor["native_kind"]
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (file_id, revision, mime_type)
    ):
        raise DriveRagError(
            "descriptor identity fields must be non-empty strings",
            code="INVALID_DESCRIPTOR",
        )
    if native_kind not in {None, "document", "spreadsheet", "presentation"}:
        raise DriveRagError("descriptor has invalid native_kind", code="INVALID_DESCRIPTOR")
    native_mime_types = {
        "document": "application/vnd.google-apps.document",
        "spreadsheet": "application/vnd.google-apps.spreadsheet",
        "presentation": "application/vnd.google-apps.presentation",
    }
    if native_kind is not None and mime_type != native_mime_types[native_kind]:
        raise DriveRagError(
            "descriptor MIME type does not match native_kind",
            code="INVALID_DESCRIPTOR",
        )
    if native_kind is None and structured_path is not None:
        raise DriveRagError(
            "structured content requires a native descriptor",
            code="INVALID_DESCRIPTOR",
        )
    if native_kind is not None and structured_path is None:
        raise DriveRagError(
            "native content requires a structured artifact",
            code="INVALID_DESCRIPTOR",
        )

    if structured_path is not None:
        structured = read_json(structured_path)
        if structured.get("kind") != native_kind:
            raise DriveRagError(
                "structured kind does not match native_kind",
                code="INVALID_DESCRIPTOR",
            )
        document = extract_native_structured(file_id, revision, structured)
    else:
        document = extract_file(
            file_id,
            revision,
            payload_path,
            mime_type,
            ocr_languages=args.ocr_languages,
        )
    atomic_write_json(
        output_path,
        {"schema_version": SCHEMA_VERSION, **document.to_dict()},
    )
    emit_result(
        "extract",
        "ok",
        file_id=file_id,
        revision=revision,
        counts={"blocks": len(document.blocks)},
        output=str(output_path),
        output_sha256=hashlib.sha256(output_path.read_bytes()).hexdigest(),
    )


def _open_index(state_root: Path, *, rebuild: bool = False) -> ChromaIndex:
    chroma_root = resolve_below(state_root, state_root / "chroma")
    return ChromaIndex(
        chroma_root,
        E5_MODEL_ID,
        E5_DIMENSION,
        rebuild=rebuild,
    )


def _parse_index_payload(payload: dict[str, object]):
    expected = {
        "schema_version",
        "file_id",
        "revision",
        "mime_type",
        "chunks",
        "embeddings",
        "roots",
    }
    if set(payload) != expected or payload.get("schema_version") != SCHEMA_VERSION:
        raise DriveRagError(
            "index payload must contain exactly the schema-1 fields",
            code="INVALID_INDEX_INPUT",
        )
    file_id = payload["file_id"]
    revision = payload["revision"]
    mime_type = payload["mime_type"]
    raw_chunks = payload["chunks"]
    embeddings = payload["embeddings"]
    roots = payload["roots"]
    if (
        not isinstance(file_id, str)
        or not isinstance(revision, str)
        or not isinstance(mime_type, str)
    ):
        raise DriveRagError(
            "index identity fields must be strings", code="INVALID_INDEX_INPUT"
        )
    if not isinstance(raw_chunks, list) or any(
        not isinstance(item, dict) for item in raw_chunks
    ):
        raise DriveRagError(
            "chunks must be a list of objects", code="INVALID_INDEX_INPUT"
        )
    if not isinstance(embeddings, list) or any(
        not isinstance(item, list) for item in embeddings
    ):
        raise DriveRagError(
            "embeddings must be a list of vectors", code="INVALID_INDEX_INPUT"
        )
    if not isinstance(roots, dict) or any(
        not isinstance(root_id, str) or not isinstance(metadata, dict)
        for root_id, metadata in roots.items()
    ):
        raise DriveRagError(
            "roots must be an object of metadata objects", code="INVALID_INDEX_INPUT"
        )
    chunks = tuple(Chunk.from_dict(item) for item in raw_chunks)
    return file_id, revision, mime_type, chunks, embeddings, roots


def _invalidate_manifest_for_index_mutation(state_root: Path) -> None:
    manifest_path = resolve_below(
        state_root, state_root / "manifests" / "current.json"
    )
    if not manifest_path.exists():
        return
    manifest = load_manifest(manifest_path)
    stale = Manifest(
        manifest.files,
        manifest.model_identity,
        None,
        "INDEX_STALE: index mutation requires manifest reconciliation",
        manifest.root_ids,
        manifest.last_inventory_generated_at,
        manifest.coverage,
        manifest.coverage_reason,
    )
    atomic_write_json(
        manifest_path,
        {"schema_version": SCHEMA_VERSION, **stale.to_dict()},
    )


def run_index(args: argparse.Namespace) -> None:
    state_root = ensure_state_root(args.state_root)
    action = args.index_action
    if action == "rebuild":
        _invalidate_manifest_for_index_mutation(state_root)
        index = _open_index(state_root, rebuild=True)
        emit_result("index.rebuild", "ok", model_identity=index.model_identity)
        return
    if action == "delete-file":
        _invalidate_manifest_for_index_mutation(state_root)
        index = _open_index(state_root)
        index.delete_file(args.file_id)
        emit_result("index.delete-file", "ok", file_id=args.file_id, records=0)
        return
    if action == "upsert":
        input_path = resolve_below(state_root, args.input)
        require_bounded_file(input_path, "index input")
        file_id, revision, mime_type, chunks, embeddings, roots = _parse_index_payload(
            read_json(input_path)
        )
        _invalidate_manifest_for_index_mutation(state_root)
        index = _open_index(state_root)
        index.upsert(
            file_id,
            revision,
            chunks,
            embeddings,
            roots,
            mime_type=mime_type,
        )
        emit_result(
            "index.upsert",
            "ok",
            file_id=file_id,
            revision=revision,
            records=index.count_file(file_id),
        )
        return
    raise DriveRagError(f"unsupported index action: {action}")


def _model_identity_string() -> str:
    return json.dumps(
        {
            "model_id": E5_MODEL_ID,
            "dimension": E5_DIMENSION,
            "distance": "cosine",
            "schema_version": SCHEMA_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def select_query_folders(
    enabled: list[FolderConfig], requested_aliases: list[str]
) -> list[FolderConfig]:
    by_alias: dict[str, FolderConfig] = {}
    for folder in enabled:
        key = alias_key(
            folder.alias, code="INVALID_STATE", require_canonical=True
        )
        if key in by_alias:
            raise DriveRagError(
                "enabled folder aliases are ambiguous", code="INVALID_STATE"
            )
        by_alias[key] = folder
    if not requested_aliases:
        return enabled
    selected: list[FolderConfig] = []
    for requested in requested_aliases:
        key = alias_key(canonical_alias(requested, code="INVALID_REQUEST"))
        folder = by_alias.get(key)
        if folder is None:
            raise DriveRagError(
                f"unknown enabled folder alias: {requested}", code="INVALID_REQUEST"
            )
        selected.append(folder)
    return selected


def run_query(args: argparse.Namespace) -> None:
    state_root = ensure_state_root(args.state_root)
    pending_journal = state_root / "journal" / "pending.json"
    if pending_journal.exists() or pending_journal.is_symlink():
        raise DriveRagError(
            "a pending synchronization journal makes the index stale",
            code="INDEX_STALE",
        )
    enabled = [folder for folder in Registry.load(state_root).list() if folder.enabled]
    if not enabled:
        raise DriveRagError(
            "no enabled Drive folders are configured",
            code="CONFIGURATION_REQUIRED",
        )
    requested = select_query_folders(enabled, args.folder_alias)

    manifest_path = state_root / "manifests" / "current.json"
    if not manifest_path.is_file():
        raise DriveRagError(
            "no successful synchronized index is available", code="INDEX_STALE"
        )
    manifest = load_manifest(manifest_path)
    enabled_ids = {folder.folder_id for folder in enabled}
    if (
        not manifest.last_success
        or manifest.last_failure is not None
        or manifest.model_identity != _model_identity_string()
        or set(manifest.root_ids) != enabled_ids
    ):
        raise DriveRagError(
            "the persistent index is not synchronized with current configuration",
            code="INDEX_STALE",
        )

    index = _open_index(state_root)
    index.assert_manifest_consistent(manifest)
    embedder = FastEmbedE5(state_root)
    evidence = QueryService(index, embedder).query(
        args.question,
        tuple(folder.folder_id for folder in requested),
        args.limit,
    )
    emit_result(
        "query",
        "ok" if manifest.coverage == "complete" else PARTIAL_INDEX,
        counts={"evidence": len(evidence)},
        coverage=manifest.coverage,
        coverage_reason=manifest.coverage_reason,
        warnings=(
            []
            if manifest.coverage == "complete"
            else ["Results come from a partial Drive inventory; coverage is incomplete."]
        ),
        evidence=[item.to_dict() for item in evidence],
    )


def main(argv: list[str] | None = None) -> int:
    operation = "command"
    try:
        args = build_parser().parse_args(argv)
        if args.command == "registry":
            operation = f"registry.{args.registry_action}"
        elif args.command in {"inventory", "sync", "index", "schedule"}:
            operation = f"{args.command}.{getattr(args, f'{args.command}_action')}"
        else:
            operation = args.command
        if args.command == "registry":
            run_registry(args)
        elif args.command == "inventory":
            run_inventory(args)
        elif args.command == "sync":
            run_sync(args)
        elif args.command == "extract":
            run_extract(args)
        elif args.command == "index":
            run_index(args)
        elif args.command == "query":
            run_query(args)
        elif args.command == "status":
            run_status(args)
        elif args.command == "schedule":
            run_schedule(args)
        return 0
    except DriveRagError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        emit_result(operation, "error", error={"code": exc.code, "message": str(exc)})
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(f"UNEXPECTED_ERROR: {exc}", file=sys.stderr)
        emit_result(
            operation,
            "error",
            error={"code": "UNEXPECTED_ERROR", "message": str(exc)},
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
