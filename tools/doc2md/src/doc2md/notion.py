from __future__ import annotations

import hashlib
import html
import mimetypes
import posixpath
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import requests

from .core import Asset, Doc2mdError, Document, ProviderError, short_id, slugify
from .http import resilient_session


NOTION_VERSION = "2026-03-11"
MAX_ASSET_BYTES = 100 * 1024 * 1024
NOTION_ASSET_HOSTS = {
    "prod-files-secure.s3.us-west-2.amazonaws.com",
    "s3.us-west-2.amazonaws.com",
    "secure.notion-static.com",
}
MARKDOWN_MEDIA_RE = re.compile(r"(?P<prefix>!\[[^\]]*\]\()(?P<url>https://[^)\s]+)(?P<suffix>\))")
TAG_MEDIA_RE = re.compile(
    r'(?P<prefix><(?:file|video|audio|pdf)\b[^>]*?\bsrc=")(?P<url>https://[^"]+)(?P<suffix>")'
)
PAGE_TAG_RE = re.compile(r'(?P<prefix><page\s+url=")(?P<url>https://[^"]+)(?P<middle>">)(?P<title>.*?)</page>')
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[(?P<title>[^\]]+)\]\((?P<url>https://[^)\s]+)\)")
NOTION_PAGE_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{32}"
)
PROTECTED_MARKDOWN_RE = re.compile(
    r"(^```.*?^```[ \t]*$|^~~~.*?^~~~[ \t]*$|\$\$.*?\$\$|(?<!\\)\$(?:\\.|[^$\n])+(?<!\\)\$|`[^`\n]*`)",
    re.MULTILINE | re.DOTALL,
)


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.notion.com/v1",
        output_root: Path = Path("sources"),
        path_prefix: tuple[str, ...] = (),
        session: requests.Session | None = None,
    ) -> None:
        self.token = token.removeprefix("Bearer ").strip()
        self.base_url = base_url.rstrip("/")
        self.output_root = output_root
        self.path_prefix = path_prefix
        self.session = session or resilient_session()

    def documents(self) -> list[Document]:
        pages = self._list_pages()
        pages_by_id = {page["id"]: page for page in pages}
        page_paths = {
            _normalize_id(page["id"]): _relative_page_path(
                page,
                pages_by_id,
                output_root=self.output_root,
                path_prefix=self.path_prefix,
            )
            for page in pages
        }
        fetched: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        for page in pages:
            page_id = page["id"]
            markdown = self._get(f"/pages/{page_id}/markdown", params={"include_transcript": "true"})
            body = markdown.get("markdown", "")
            if markdown.get("truncated"):
                for block_id in markdown.get("unknown_block_ids", []):
                    try:
                        subtree = self._get(f"/pages/{block_id}/markdown")
                    except ProviderError as exc:
                        if exc.status_code == 404 and exc.code == "object_not_found":
                            continue
                        raise
                    body += "\n" + subtree.get("markdown", "")
            fetched.append((page, body, markdown))

        documents: list[Document] = []
        for page, raw_body, markdown in fetched:
            page_id = page["id"]
            title = _page_title(page)
            parent = page.get("parent", {})
            relative_path = page_paths[_normalize_id(page_id)]
            body = _rewrite_page_links(raw_body, relative_path, page_paths)
            body, assets = self._mirror_assets(body, page_id, relative_path)
            documents.append(
                Document(
                    source="notion",
                    source_id=page_id,
                    source_url=page.get("url", ""),
                    title=title,
                    body=body,
                    created_at=page.get("created_time", ""),
                    updated_at=page.get("last_edited_time", ""),
                    path_parts=_page_path(page, pages_by_id, self.path_prefix),
                    metadata={
                        "parent_type": parent.get("type", ""),
                        "parent_id": _parent_id(parent),
                        "created_by": page.get("created_by", {}).get("id", ""),
                        "last_edited_by": page.get("last_edited_by", {}).get("id", ""),
                        "notion_truncated": bool(markdown.get("truncated")),
                        "notion_unknown_block_ids": markdown.get("unknown_block_ids", []),
                    },
                    assets=assets,
                )
            )
        return documents

    def _mirror_assets(self, body: str, page_id: str, page_path: Path) -> tuple[str, tuple[Asset, ...]]:
        assets: dict[Path, Asset] = {}

        def replace(match: re.Match[str]) -> str:
            url = html.unescape(match.group("url"))
            parsed = urlparse(url)
            signed = "X-Amz-Signature=" in parsed.query
            notion_asset = parsed.hostname in NOTION_ASSET_HOSTS and (
                parsed.hostname != "s3.us-west-2.amazonaws.com"
                or parsed.path.startswith("/secure.notion-static.com/")
            )
            if not notion_asset:
                if signed:
                    raise Doc2mdError(f"unsupported Notion pre-signed asset host: {parsed.hostname}")
                return match.group(0)
            relative = _asset_path(page_id, url, output_root=self.output_root)
            if relative not in assets:
                assets[relative] = Asset(relative, self._download_asset(url))
            link = posixpath.relpath(relative.as_posix(), page_path.parent.as_posix())
            return f'{match.group("prefix")}{link}{match.group("suffix")}'

        body = _transform_unprotected(
            body,
            lambda segment: TAG_MEDIA_RE.sub(replace, MARKDOWN_MEDIA_RE.sub(replace, segment)),
        )
        return body, tuple(assets.values())

    def _download_asset(self, url: str) -> bytes:
        response = self.session.get(url, timeout=120)
        if not response.ok:
            raise Doc2mdError(f"Notion asset download returned HTTP {response.status_code}")
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_ASSET_BYTES:
            raise Doc2mdError(f"Notion asset exceeds {MAX_ASSET_BYTES} byte limit")
        content = response.content
        if len(content) > MAX_ASSET_BYTES:
            raise Doc2mdError(f"Notion asset exceeds {MAX_ASSET_BYTES} byte limit")
        return content

    def _list_pages(self) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
                "sort": {"direction": "ascending", "timestamp": "last_edited_time"},
            }
            if cursor:
                body["start_cursor"] = cursor
            response = self._post("/search", body)
            pages.extend(page for page in response.get("results", []) if not page.get("in_trash"))
            if not response.get("has_more"):
                return pages
            cursor = response.get("next_cursor")
            if not cursor:
                raise Doc2mdError("Notion search returned has_more without next_cursor")

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = self.session.get(
            self.base_url + path,
            headers=self._headers(),
            params=params,
            timeout=60,
        )
        return _json_or_raise(response, "Notion")

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(),
            json=body,
            timeout=60,
        )
        return _json_or_raise(response, "Notion")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }


def _page_title(page: dict[str, Any]) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") != "title":
            continue
        title = "".join(item.get("plain_text", "") for item in prop.get("title", []))
        if title.strip():
            return title.strip()
    return "Untitled Notion page"


def _parent_id(parent: dict[str, Any]) -> str:
    parent_type = parent.get("type", "")
    value = parent.get(parent_type)
    return value if isinstance(value, str) else ""


def _page_path(
    page: dict[str, Any],
    pages_by_id: dict[str, dict[str, Any]],
    path_prefix: tuple[str, ...] = (),
) -> tuple[str, ...]:
    ancestors: list[str] = []
    seen = {page["id"]}
    parent_id = _parent_id(page.get("parent", {}))
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = pages_by_id.get(parent_id)
        if parent is None:
            break
        ancestors.append(_page_title(parent))
        parent_id = _parent_id(parent.get("parent", {}))
    return (*path_prefix, *reversed(ancestors))


def _relative_page_path(
    page: dict[str, Any],
    pages_by_id: dict[str, dict[str, Any]],
    *,
    output_root: Path = Path("sources"),
    path_prefix: tuple[str, ...] = (),
) -> Path:
    return Document(
        source="notion",
        source_id=page["id"],
        title=_page_title(page),
        body="",
        updated_at=page.get("last_edited_time", ""),
        path_parts=_page_path(page, pages_by_id, path_prefix),
    ).relative_path(output_root)


def _rewrite_page_links(body: str, current_path: Path, page_paths: dict[str, Path]) -> str:
    def relative_link(url: str) -> str | None:
        page_id = _page_id_from_url(url)
        target = page_paths.get(page_id) if page_id else None
        if target is None:
            return None
        return posixpath.relpath(target.as_posix(), current_path.parent.as_posix())

    def replace_page(match: re.Match[str]) -> str:
        target = relative_link(match.group("url"))
        return f'[{match.group("title")}]({target})' if target else match.group(0)

    def replace_link(match: re.Match[str]) -> str:
        target = relative_link(match.group("url"))
        return f'[{match.group("title")}]({target})' if target else match.group(0)

    return _transform_unprotected(
        body,
        lambda segment: MARKDOWN_LINK_RE.sub(replace_link, PAGE_TAG_RE.sub(replace_page, segment)),
    )


def _transform_unprotected(body: str, transform: Callable[[str], str]) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in PROTECTED_MARKDOWN_RE.finditer(body):
        pieces.append(transform(body[cursor : match.start()]))
        pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(transform(body[cursor:]))
    return "".join(pieces)


def _page_id_from_url(url: str) -> str:
    location = unquote(urlparse(url).path + "#" + urlparse(url).fragment)
    candidates = NOTION_PAGE_ID_RE.findall(location)
    return _normalize_id(candidates[-1]) if candidates else ""


def _normalize_id(value: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", value).lower()


def _asset_path(page_id: str, url: str, *, output_root: Path = Path("sources")) -> Path:
    parsed = urlparse(url)
    filename = unquote(PurePosixPath(parsed.path).name)
    suffix = Path(filename).suffix.lower()
    if not suffix:
        suffix = mimetypes.guess_extension("application/octet-stream") or ".bin"
    suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"
    stem = Path(filename).stem or "asset"
    stable_id = hashlib.sha256(parsed.path.encode()).hexdigest()[:12]
    return Path(
        *output_root.parts,
        "notion",
        "assets",
        short_id(page_id),
        f"{slugify(stem)}--{stable_id}{suffix}",
    )


def _json_or_raise(response: requests.Response, provider: str) -> dict[str, Any]:
    if response.ok:
        return response.json()
    request_id = response.headers.get("x-request-id", "")
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code = payload.get("code", "") if isinstance(payload, dict) else ""
    raise ProviderError(
        f"{provider} API returned HTTP {response.status_code} code {code or 'unknown'} (request {request_id})",
        status_code=response.status_code,
        code=code if isinstance(code, str) else "",
    )
