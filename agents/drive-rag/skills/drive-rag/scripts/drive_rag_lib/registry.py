"""Persistent, non-secret configuration for multiple Google Drive folders."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, unquote, urlparse

from .aliases import alias_key, canonical_alias
from .models import FolderConfig
from .paths import ensure_state_root, resolve_below
from .protocol import SCHEMA_VERSION, DriveRagError, atomic_write_json, read_json


class Registry:
    def __init__(self, state_root: Path, folders: Iterable[FolderConfig] = ()) -> None:
        self.state_root = state_root
        self.path = resolve_below(state_root, state_root / "config" / "folders.json")
        self._folders = list(folders)
        self._validate_collection(self._folders)

    @classmethod
    def load(cls, state_root: Path) -> "Registry":
        state = ensure_state_root(state_root)
        path = resolve_below(state, state / "config" / "folders.json")
        if not path.exists():
            return cls(state)
        payload = read_json(path)
        if set(payload) != {"schema_version", "folders"}:
            raise DriveRagError(
                "folder registry must contain only schema_version and folders",
                code="INVALID_STATE",
            )
        if payload["schema_version"] != SCHEMA_VERSION:
            raise DriveRagError(
                f"unsupported folder registry schema: {payload['schema_version']!r}",
                code="UNSUPPORTED_SCHEMA",
            )
        raw_folders = payload["folders"]
        if not isinstance(raw_folders, list) or any(
            not isinstance(item, dict) for item in raw_folders
        ):
            raise DriveRagError(
                "folder registry folders must be a list of objects",
                code="INVALID_STATE",
            )
        return cls(state, (FolderConfig.from_dict(item) for item in raw_folders))

    def add(self, folder: FolderConfig) -> FolderConfig:
        normalized = self._normalize(folder)
        if any(item.folder_id == normalized.folder_id for item in self._folders):
            raise DriveRagError(
                f"folder ID already exists: {normalized.folder_id}",
                code="DUPLICATE_FOLDER_ID",
            )
        normalized_key = alias_key(normalized.alias)
        if any(alias_key(item.alias) == normalized_key for item in self._folders):
            raise DriveRagError(
                f"folder alias already exists: {normalized.alias}",
                code="DUPLICATE_ALIAS",
            )
        self._folders.append(normalized)
        self._save()
        return normalized

    def list(self) -> list[FolderConfig]:
        return sorted(self._folders, key=lambda item: (item.alias.casefold(), item.alias))

    def set_enabled(self, identifier: str, enabled: bool) -> FolderConfig:
        index = self._find_index(identifier)
        updated = replace(self._folders[index], enabled=enabled)
        self._folders[index] = updated
        self._save()
        return updated

    def rename(self, identifier: str, alias: str) -> FolderConfig:
        index = self._find_index(identifier)
        candidate = canonical_alias(alias)
        candidate_key = alias_key(candidate)
        if any(
            item_index != index and alias_key(item.alias) == candidate_key
            for item_index, item in enumerate(self._folders)
        ):
            raise DriveRagError(
                f"folder alias already exists: {candidate}",
                code="DUPLICATE_ALIAS",
            )
        updated = replace(self._folders[index], alias=candidate)
        self._folders[index] = updated
        self._save()
        return updated

    def remove(self, identifier: str) -> FolderConfig:
        index = self._find_index(identifier)
        removed = self._folders.pop(index)
        self._save()
        return removed

    def _find_index(self, identifier: str) -> int:
        try:
            identifier_alias_key = alias_key(identifier)
        except DriveRagError:
            identifier_alias_key = None
        matches = [
            index
            for index, item in enumerate(self._folders)
            if identifier == item.folder_id
            or (
                identifier_alias_key is not None
                and alias_key(item.alias) == identifier_alias_key
            )
        ]
        if not matches:
            raise DriveRagError(
                f"folder not found: {identifier}",
                code="FOLDER_NOT_FOUND",
            )
        if len(matches) > 1:
            raise DriveRagError(
                f"folder identifier is ambiguous: {identifier}",
                code="AMBIGUOUS_FOLDER",
            )
        return matches[0]

    @classmethod
    def _validate_collection(cls, folders: Iterable[FolderConfig]) -> None:
        seen_ids: set[str] = set()
        seen_aliases: set[str] = set()
        for folder in folders:
            normalized = cls._normalize(folder)
            if folder != normalized:
                raise DriveRagError(
                    f"folder registry contains a non-canonical URL for {folder.folder_id}",
                    code="INVALID_STATE",
                )
            if folder.folder_id in seen_ids:
                raise DriveRagError(
                    f"duplicate folder ID in registry: {folder.folder_id}",
                    code="DUPLICATE_FOLDER_ID",
                )
            folder_alias_key = alias_key(
                folder.alias, code="INVALID_STATE", require_canonical=True
            )
            if folder_alias_key in seen_aliases:
                raise DriveRagError(
                    f"duplicate folder alias in registry: {folder.alias}",
                    code="DUPLICATE_ALIAS",
                )
            seen_ids.add(folder.folder_id)
            seen_aliases.add(folder_alias_key)

    @staticmethod
    def _normalize(folder: FolderConfig) -> FolderConfig:
        folder_id = folder.folder_id.strip()
        alias = canonical_alias(folder.alias)
        if not folder_id:
            raise DriveRagError("folder ID must not be empty", code="INVALID_FOLDER_ID")
        try:
            parsed = urlparse(folder.url)
            valid_origin = (
                parsed.scheme == "https"
                and parsed.hostname == "drive.google.com"
                and parsed.username is None
                and parsed.password is None
            )
        except (TypeError, ValueError) as exc:
            raise DriveRagError(
                "folder URL is malformed",
                code="INVALID_FOLDER_URL",
            ) from exc
        if not valid_origin:
            raise DriveRagError(
                "folder URL must be an HTTPS drive.google.com folder URL",
                code="INVALID_FOLDER_URL",
            )
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        try:
            folders_index = parts.index("folders")
            url_folder_id = parts[folders_index + 1]
        except (ValueError, IndexError) as exc:
            raise DriveRagError(
                "folder URL must contain /folders/<folder-id>",
                code="INVALID_FOLDER_URL",
            ) from exc
        if folders_index < 1 or parts[0] != "drive" or folders_index + 2 != len(parts):
            raise DriveRagError(
                "folder URL must identify exactly one Google Drive folder",
                code="INVALID_FOLDER_URL",
            )
        if url_folder_id != folder_id:
            raise DriveRagError(
                "folder URL ID does not match folder_id",
                code="INVALID_FOLDER_URL",
            )
        canonical_url = f"https://drive.google.com/drive/folders/{quote(folder_id, safe='-_')}"
        return replace(folder, folder_id=folder_id, alias=alias, url=canonical_url)

    def _save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "folders": [folder.to_dict() for folder in self.list()],
            },
        )
