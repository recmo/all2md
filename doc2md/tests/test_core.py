import json
from pathlib import Path

import pytest

from doc2md.core import Asset, Doc2mdError, Document, Repository


def document(source_id: str, *, body: str = "Body", updated_at: str = "2026-07-14T10:00:00Z") -> Document:
    return Document(
        source="notion",
        source_id=source_id,
        source_url=f"https://notion.so/{source_id}",
        title=f"Page {source_id}",
        body=body,
        created_at="2026-07-01T10:00:00Z",
        updated_at=updated_at,
        path_parts=("workspace",),
    )


def test_apply_is_idempotent_and_preserves_ingested_at(tmp_path: Path) -> None:
    repo = Repository(tmp_path)
    first = repo.apply("notion", [document("abc")])
    path = next((tmp_path / "sources/notion").rglob("*.md"))
    before = path.read_text()

    second = Repository(tmp_path).apply("notion", [document("abc")])

    assert first["created"] == 1
    assert second == {"created": 0, "updated": 0, "unchanged": 1, "deleted": 0}
    assert path.read_text() == before


def test_move_removes_old_generated_path(tmp_path: Path) -> None:
    repo = Repository(tmp_path)
    repo.apply("notion", [document("abc")])
    old_path = next((tmp_path / "sources/notion").rglob("*.md"))
    moved = Document(**{**document("abc").__dict__, "path_parts": ("workspace", "new")})

    Repository(tmp_path).apply("notion", [moved])

    assert not old_path.exists()
    assert len(list((tmp_path / "sources/notion").rglob("*.md"))) == 1


def test_bulk_delete_is_refused_without_explicit_override(tmp_path: Path) -> None:
    repo = Repository(tmp_path)
    repo.apply("notion", [document(str(index)) for index in range(12)])

    with pytest.raises(Doc2mdError, match="refusing to delete"):
        Repository(tmp_path).apply("notion", [])

    assert len(list((tmp_path / "sources/notion").rglob("*.md"))) == 12


def test_bulk_delete_refusal_happens_before_moves_or_writes(tmp_path: Path) -> None:
    Repository(tmp_path).apply("notion", [document(str(index)) for index in range(12)])
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    changed = Document(
        **{
            **document("0", body="changed").__dict__,
            "path_parts": ("workspace", "moved"),
        }
    )

    with pytest.raises(Doc2mdError, match="refusing to delete"):
        Repository(tmp_path).apply("notion", [changed])

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_complete_deletion_of_small_source_is_refused(tmp_path: Path) -> None:
    Repository(tmp_path).apply("notion", [document("abc")])

    with pytest.raises(Doc2mdError, match="refusing to delete 1 of 1"):
        Repository(tmp_path).apply("notion", [])


def test_assets_are_written_and_removed_with_document(tmp_path: Path) -> None:
    asset_path = Path("sources/notion/assets/abc/diagram.png")
    with_asset = Document(**{**document("abc").__dict__, "assets": (Asset(asset_path, b"png"),)})
    Repository(tmp_path).apply("notion", [with_asset])

    assert (tmp_path / asset_path).read_bytes() == b"png"

    without_asset = Document(**{**document("abc").__dict__, "updated_at": "2026-07-15T10:00:00Z"})
    Repository(tmp_path).apply("notion", [without_asset])

    assert not (tmp_path / asset_path).exists()


def test_manifest_contains_only_operational_metadata(tmp_path: Path) -> None:
    Repository(tmp_path).apply("notion", [document("abc", body="private body")])
    manifest = json.loads((tmp_path / "sources/.doc2md/manifest.json").read_text())

    assert "private body" not in json.dumps(manifest)
    assert manifest["sources"]["notion"]["abc"]["revision"] == "2026-07-14T10:00:00Z"


def test_custom_output_root_contains_documents_assets_and_manifest(tmp_path: Path) -> None:
    asset_path = Path("generated/notion/assets/abc/diagram.png")
    item = Document(**{**document("abc").__dict__, "assets": (Asset(asset_path, b"png"),)})

    Repository(tmp_path, output_root=Path("generated")).apply("notion", [item])

    assert list((tmp_path / "generated/notion").rglob("*.md"))
    assert (tmp_path / asset_path).read_bytes() == b"png"
    assert (tmp_path / "generated/.doc2md/manifest.json").exists()


def test_asset_outside_output_root_is_rejected(tmp_path: Path) -> None:
    item = Document(
        **{**document("abc").__dict__, "assets": (Asset(Path("elsewhere/file.bin"), b"x"),)}
    )

    with pytest.raises(Doc2mdError, match="unsafe generated path"):
        Repository(tmp_path).apply("notion", [item])


def test_symlink_inside_output_root_cannot_escape_destination(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources/notion").symlink_to(outside, target_is_directory=True)

    with pytest.raises(Doc2mdError, match="symlink"):
        Repository(tmp_path).apply("notion", [document("abc")])

    assert list(outside.iterdir()) == []


def test_incomplete_sync_keeps_unseen_documents(tmp_path: Path) -> None:
    Repository(tmp_path).apply("notion", [document("a"), document("b")])

    counts = Repository(tmp_path).apply("notion", [document("a")], complete=False)

    assert counts["deleted"] == 0
    assert len(Repository(tmp_path).manifest["sources"]["notion"]) == 2
