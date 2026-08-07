from __future__ import annotations

from collections import deque
from typing import Any
from urllib.parse import quote

import requests

from .core import Doc2mdError, Document
from .http import resilient_session


FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FILE_FIELDS = (
    "id,name,mimeType,createdTime,modifiedTime,webViewLink,driveId,"
    "owners(displayName,emailAddress),shortcutDetails(targetId,targetMimeType)"
)


class GoogleDriveClient:
    def __init__(
        self,
        bearer: str,
        root_ids: list[str],
        *,
        base_url: str = "https://www.googleapis.com/drive/v3",
        path_prefix: tuple[str, ...] = (),
        session: requests.Session | None = None,
    ) -> None:
        self.bearer = bearer.removeprefix("Bearer ").strip()
        self.root_ids = [root for root in root_ids if root]
        self.base_url = base_url.rstrip("/")
        self.path_prefix = path_prefix
        self.session = session or resilient_session()

    def documents(self) -> list[Document]:
        documents: list[Document] = []
        queue: deque[tuple[str, tuple[str, ...]]] = deque(
            (root, self.path_prefix) for root in self.root_ids
        )
        seen_folders: set[str] = set()
        seen_docs: set[str] = set()

        while queue:
            folder_id, path = queue.popleft()
            if folder_id in seen_folders:
                continue
            seen_folders.add(folder_id)
            for item in self._list_children(folder_id):
                if item["mimeType"] == SHORTCUT_MIME:
                    details = item.get("shortcutDetails", {})
                    target_id = details.get("targetId")
                    target_mime = details.get("targetMimeType")
                    if not target_id or target_mime not in {FOLDER_MIME, DOC_MIME}:
                        raise Doc2mdError(f"unsupported Google Drive shortcut {item.get('id', '')}")
                    if target_mime == FOLDER_MIME:
                        queue.append((target_id, (*path, item["name"])))
                        continue
                    item = self._get_file(target_id)
                if item["mimeType"] == FOLDER_MIME:
                    queue.append((item["id"], (*path, item["name"])))
                elif item["mimeType"] == DOC_MIME and item["id"] not in seen_docs:
                    seen_docs.add(item["id"])
                    documents.append(self._document(item, path))
        return documents

    def _document(self, item: dict[str, Any], path: tuple[str, ...]) -> Document:
        file_id = item["id"]
        response = self.session.get(
            f"{self.base_url}/files/{quote(file_id, safe='')}/export",
            headers=self._headers(),
            params={"mimeType": "text/markdown"},
            timeout=120,
        )
        if not response.ok:
            raise Doc2mdError(f"Google Drive export returned HTTP {response.status_code} for {file_id}")
        owners = [owner.get("emailAddress") or owner.get("displayName") for owner in item.get("owners", [])]
        return Document(
            source="google-docs",
            source_id=file_id,
            source_url=item.get("webViewLink", ""),
            title=item["name"],
            body=response.text,
            created_at=item.get("createdTime", ""),
            updated_at=item.get("modifiedTime", ""),
            path_parts=path,
            metadata={
                "drive_id": item.get("driveId", ""),
                "owners": [owner for owner in owners if owner],
            },
        )

    def _get_file(self, file_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/files/{quote(file_id, safe='')}",
            headers=self._headers(),
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
            timeout=60,
        )
        if not response.ok:
            raise Doc2mdError(f"Google Drive metadata returned HTTP {response.status_code} for {file_id}")
        item = response.json()
        if item.get("mimeType") != DOC_MIME:
            raise Doc2mdError(f"Google Drive shortcut target {file_id} is no longer a Doc")
        return item

    def _list_children(self, folder_id: str) -> list[dict[str, Any]]:
        fields = f"nextPageToken,incompleteSearch,files({FILE_FIELDS})"
        files: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": fields,
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            response = self.session.get(
                f"{self.base_url}/files",
                headers=self._headers(),
                params=params,
                timeout=60,
            )
            if not response.ok:
                raise Doc2mdError(
                    f"Google Drive list returned HTTP {response.status_code} for folder {folder_id}"
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise Doc2mdError(f"Google Drive list returned an invalid response for folder {folder_id}")
            if payload.get("incompleteSearch"):
                raise Doc2mdError(f"Google Drive search was incomplete for folder {folder_id}")
            page_files = payload.get("files", [])
            if not isinstance(page_files, list) or not all(isinstance(item, dict) for item in page_files):
                raise Doc2mdError(f"Google Drive list returned invalid files for folder {folder_id}")
            files.extend(page_files)
            page_token = payload.get("nextPageToken")
            if not page_token:
                return sorted(
                    files,
                    key=lambda item: (
                        str(item.get("name", "")).casefold(),
                        str(item.get("name", "")),
                        str(item.get("id", "")),
                    ),
                )
            if not isinstance(page_token, str) or page_token in seen_tokens:
                raise Doc2mdError(f"Google Drive returned an invalid page token for folder {folder_id}")
            seen_tokens.add(page_token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bearer}", "Accept": "application/json"}
