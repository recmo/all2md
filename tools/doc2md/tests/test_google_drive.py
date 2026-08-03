import pytest

from doc2md.core import Doc2mdError
from doc2md.google_drive import DOC_MIME, FOLDER_MIME, SHORTCUT_MIME, GoogleDriveClient


class Response:
    def __init__(self, payload=None, text="", *, ok=True, status_code=200):
        self.payload = payload
        self.text = text
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self.payload


class Session:
    def __init__(self):
        self.list_calls = 0

    def get(self, url, **kwargs):
        if url.endswith("/files"):
            self.list_calls += 1
            if self.list_calls == 1:
                return Response(
                    {
                        "files": [
                            {"id": "folder-2", "name": "Plans", "mimeType": FOLDER_MIME},
                            {
                                "id": "doc-1",
                                "name": "Root doc",
                                "mimeType": DOC_MIME,
                                "createdTime": "2026-07-01T10:00:00Z",
                                "modifiedTime": "2026-07-14T10:00:00Z",
                                "webViewLink": "https://docs.google.com/document/d/doc-1",
                                "owners": [{"emailAddress": "owner@example.com"}],
                            },
                        ]
                    }
                )
            return Response(
                {
                    "files": [
                        {
                            "id": "doc-2",
                            "name": "Nested doc",
                            "mimeType": DOC_MIME,
                            "createdTime": "2026-07-02T10:00:00Z",
                            "modifiedTime": "2026-07-14T11:00:00Z",
                            "webViewLink": "https://docs.google.com/document/d/doc-2",
                            "owners": [],
                        }
                    ]
                }
            )
        return Response(text="# Exported markdown")


def test_google_drive_walks_allowlisted_folder_tree() -> None:
    documents = GoogleDriveClient(
        "test-token", ["root"], path_prefix=("workspace",), session=Session()
    ).documents()

    assert [document.source_id for document in documents] == ["doc-1", "doc-2"]
    assert documents[1].path_parts == ("workspace", "Plans")
    assert documents[0].body == "# Exported markdown"


def test_google_drive_resolves_doc_and_folder_shortcuts() -> None:
    class ShortcutSession:
        def __init__(self):
            self.list_calls = 0

        def get(self, url, **kwargs):
            if url.endswith("/files"):
                self.list_calls += 1
                if self.list_calls == 1:
                    return Response(
                        {
                            "files": [
                                {
                                    "id": "shortcut-doc",
                                    "name": "Doc shortcut",
                                    "mimeType": SHORTCUT_MIME,
                                    "shortcutDetails": {"targetId": "doc-target", "targetMimeType": DOC_MIME},
                                },
                                {
                                    "id": "shortcut-folder",
                                    "name": "Shared plans",
                                    "mimeType": SHORTCUT_MIME,
                                    "shortcutDetails": {
                                        "targetId": "folder-target",
                                        "targetMimeType": FOLDER_MIME,
                                    },
                                },
                            ]
                        }
                    )
                return Response(
                    {
                        "files": [
                            {
                                "id": "nested-doc",
                                "name": "Nested doc",
                                "mimeType": DOC_MIME,
                                "modifiedTime": "2026-07-16T10:00:00Z",
                            }
                        ]
                    }
                )
            if url.endswith("/files/doc-target"):
                return Response(
                    {
                        "id": "doc-target",
                        "name": "Target doc",
                        "mimeType": DOC_MIME,
                        "modifiedTime": "2026-07-16T09:00:00Z",
                    }
                )
            return Response(text="# Exported")

    documents = GoogleDriveClient(
        "test-token",
        ["root"],
        path_prefix=("workspace",),
        session=ShortcutSession(),
    ).documents()

    assert [document.source_id for document in documents] == ["doc-target", "nested-doc"]
    assert documents[1].path_parts == ("workspace", "Shared plans")


def test_google_drive_paginates_and_deduplicates_documents() -> None:
    class PaginatedSession:
        def get(self, url, **kwargs):
            if url.endswith("/files"):
                if kwargs["params"].get("pageToken") == "next":
                    return Response(
                        {
                            "files": [
                                {
                                    "id": "doc-1",
                                    "name": "Duplicate",
                                    "mimeType": DOC_MIME,
                                    "modifiedTime": "2026-01-01T00:00:00Z",
                                },
                                {
                                    "id": "doc-2",
                                    "name": "Second",
                                    "mimeType": DOC_MIME,
                                    "modifiedTime": "2026-01-01T00:00:00Z",
                                },
                            ]
                        }
                    )
                return Response(
                    {
                        "files": [
                            {
                                "id": "doc-1",
                                "name": "First",
                                "mimeType": DOC_MIME,
                                "modifiedTime": "2026-01-01T00:00:00Z",
                            }
                        ],
                        "nextPageToken": "next",
                    }
                )
            return Response(text="# Exported")

    documents = GoogleDriveClient("test-token", ["root"], session=PaginatedSession()).documents()

    assert [document.source_id for document in documents] == ["doc-1", "doc-2"]


def test_google_drive_export_failure_aborts_enumeration() -> None:
    class FailingExportSession:
        def get(self, url, **kwargs):
            if url.endswith("/files"):
                return Response(
                    {
                        "files": [
                            {
                                "id": "doc-1",
                                "name": "Document",
                                "mimeType": DOC_MIME,
                                "modifiedTime": "2026-01-01T00:00:00Z",
                            }
                        ]
                    }
                )
            return Response(ok=False, status_code=503)

    with pytest.raises(Doc2mdError, match="HTTP 503"):
        GoogleDriveClient("test-token", ["root"], session=FailingExportSession()).documents()
