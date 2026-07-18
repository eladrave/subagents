from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import fitz


class FakeEmbedder:
    model_id = "test/fake"
    dimension = 3

    def __init__(self):
        self.passage_calls = 0
        self.query_calls = 0

    def embed_passages(self, texts):
        self.passage_calls += 1
        return [[1.0, float("budget" in text.lower()), 0.0] for text in texts]

    def embed_query(self, text):
        self.query_calls += 1
        return [1.0, float("budget" in text.lower()), 0.0]

    def count(self, text):
        return len(text.split())


@dataclass
class SyncFixture:
    engine: object
    first_inventory: object
    first_artifacts: object
    empty_inventory: object
    empty_artifacts: object
    source_file: object
    embedder: FakeEmbedder

    def changed(self, *, text="Updated retention policy", revision="2"):
        from drive_rag_lib.models import Artifact, ArtifactSet, RemoteInventory

        changed = replace(
            self.source_file,
            revision=revision,
            modified_time="2026-07-18T11:00:00Z",
        )
        inventory = RemoteInventory(
            f"run-{revision}",
            True,
            ("root-a",),
            (changed,),
            None,
            _next_timestamp(self.first_inventory.generated_at, int(revision)),
        )
        stage = self.engine.state_root / "staging" / inventory.run_id
        payload, structured = _write_native_document(stage, "file-a", text)
        artifact = Artifact(
            "file-a",
            revision,
            str(payload),
            hashlib.sha256(payload.read_bytes()).hexdigest(),
            str(structured),
        )
        return inventory, ArtifactSet(inventory.run_id, (artifact,))

    def changed_with_corrupt_payload(self):
        from drive_rag_lib.models import Artifact, ArtifactSet, RemoteInventory

        changed = replace(
            self.source_file,
            revision="2",
            modified_time="2026-07-18T11:00:00Z",
        )
        inventory = RemoteInventory(
            "run-2",
            True,
            ("root-a",),
            (changed,),
            None,
            "2026-07-18T11:00:00Z",
        )
        path = self.engine.state_root / "staging" / "run-2" / "file-a.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-a-pdf")
        structured = path.with_suffix(".structured.json")
        structured.write_text(
            json.dumps(
                {
                    "kind": "document",
                    "sections": [
                        {"locator": "section:Policy", "text": "changed"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        artifact = Artifact(
            "file-a",
            "2",
            str(path),
            "0" * 64,
            str(structured),
        )
        return inventory, ArtifactSet("run-2", (artifact,))


def _next_timestamp(value: str, minutes: int = 1) -> str:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return (parsed + timedelta(minutes=minutes)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _write_native_document(stage: Path, file_id: str, text: str):
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    pdf_path = stage / f"{file_id}.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), text)
    pdf.save(pdf_path)
    pdf.close()
    structured = stage / f"{file_id}.structured.json"
    structured.write_text(
        json.dumps(
            {
                "kind": "document",
                "sections": [{"locator": "section:Policy", "text": text}],
            }
        ),
        encoding="utf-8",
    )
    return pdf_path, structured


def build_sync_fixture(tmp_path):
    from drive_rag_lib.models import (
        Artifact,
        ArtifactSet,
        FolderConfig,
        RemoteFile,
        RemoteInventory,
        RemotePath,
    )
    from drive_rag_lib.paths import ensure_state_root
    from drive_rag_lib.registry import Registry
    from drive_rag_lib.sync import SyncEngine

    state = ensure_state_root(tmp_path / "state")
    Registry.load(state).add(
        FolderConfig(
            "root-a",
            "https://drive.google.com/drive/folders/root-a",
            "Finance",
            True,
        )
    )
    remote_path = RemotePath("root-a", ("root-a",), ("Policy",))
    source = RemoteFile(
        "file-a",
        "Policy",
        "application/vnd.google-apps.document",
        "1",
        "2026-07-18T10:00:00Z",
        "https://docs.google.com/document/d/file-a/edit",
        None,
        None,
        (remote_path,),
        "document",
    )
    inventory = RemoteInventory(
        "run-1",
        True,
        ("root-a",),
        (source,),
        None,
        "2026-07-18T10:00:00Z",
    )
    stage = state / "staging" / "run-1"
    pdf_path, structured = _write_native_document(stage, "file-a", "Retention policy")
    artifact = Artifact(
        "file-a",
        "1",
        str(pdf_path),
        hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        str(structured),
    )
    empty = RemoteInventory(
        "run-empty",
        True,
        ("root-a",),
        (),
        None,
        "2026-07-18T12:00:00Z",
    )
    embedder = FakeEmbedder()
    engine = SyncEngine.open(state, embedder)
    return SyncFixture(
        engine,
        inventory,
        ArtifactSet("run-1", (artifact,)),
        empty,
        ArtifactSet("run-empty", ()),
        source,
        embedder,
    )
