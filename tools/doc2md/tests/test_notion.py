from pathlib import Path

import pytest

from doc2md.core import ProviderError
from doc2md.notion import NOTION_VERSION, NotionClient, _page_path, _rewrite_page_links


class Response:
    def __init__(self, payload, *, content=b"", ok=True, status_code=200):
        self.payload = payload
        self.content = content
        self.ok = ok
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self.payload


class Session:
    def __init__(self, markdown):
        self.markdown = markdown
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response(
            {
                "results": [
                    {
                        "id": "page-123",
                        "url": "https://notion.so/page-123",
                        "created_time": "2026-07-01T10:00:00Z",
                        "last_edited_time": "2026-07-14T10:00:00Z",
                        "created_by": {"id": "user-1"},
                        "last_edited_by": {"id": "user-2"},
                        "parent": {"type": "workspace", "workspace": True},
                        "properties": {
                            "Name": {
                                "type": "title",
                                "title": [{"plain_text": "Math notes"}],
                            }
                        },
                    }
                ],
                "has_more": False,
            }
        )

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return Response(
            {
                "object": "page_markdown",
                "id": "page-123",
                "markdown": self.markdown,
                "truncated": False,
                "unknown_block_ids": [],
            }
        )


def test_notion_markdown_and_latex_are_preserved_exactly() -> None:
    latex = (
        "Inline $\\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}$ stays inline.\n\n"
        "$$\n\\begin{aligned}\nA &= B \\\\\n+C &= \\{x \\mid x > 0\\}\n\\end{aligned}\n$$\n\n"
        "```latex\n\\newcommand{\\R}{\\mathbb{R}}\n```"
    )
    session = Session(latex)

    documents = NotionClient("placeholder", session=session).documents()

    assert len(documents) == 1
    assert documents[0].body == latex
    markdown_call = next(call for call in session.calls if call[0] == "GET")
    assert markdown_call[2]["params"] == {"include_transcript": "true"}
    assert markdown_call[2]["headers"]["Notion-Version"] == NOTION_VERSION


def test_truncated_notion_page_appends_retrievable_subtrees() -> None:
    class TruncatedSession(Session):
        def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            if url.endswith("/page-123/markdown"):
                return Response(
                    {
                        "markdown": "Before\n<unknown/>",
                        "truncated": True,
                        "unknown_block_ids": ["block-456"],
                    }
                )
            return Response({"markdown": "Recovered", "truncated": False, "unknown_block_ids": []})

    documents = NotionClient("placeholder", session=TruncatedSession("")).documents()

    assert documents[0].body.endswith("\nRecovered")


def test_transient_subtree_failure_aborts_sync() -> None:
    class FailingSession(Session):
        def get(self, url, **kwargs):
            if url.endswith("/page-123/markdown"):
                return Response(
                    {"markdown": "Before", "truncated": True, "unknown_block_ids": ["block-456"]}
                )
            return Response({"code": "internal_server_error"}, ok=False, status_code=503)

    with pytest.raises(ProviderError, match="HTTP 503"):
        NotionClient("placeholder", session=FailingSession("")).documents()


def test_inaccessible_subtree_is_the_only_ignored_failure() -> None:
    class InaccessibleSession(Session):
        def get(self, url, **kwargs):
            if url.endswith("/page-123/markdown"):
                return Response(
                    {"markdown": "Before", "truncated": True, "unknown_block_ids": ["block-456"]}
                )
            return Response({"code": "object_not_found"}, ok=False, status_code=404)

    documents = NotionClient("placeholder", session=InaccessibleSession("")).documents()

    assert documents[0].body == "Before"


def test_notion_page_hierarchy_becomes_directories() -> None:
    parent = {
        "id": "parent",
        "parent": {"type": "workspace", "workspace": True},
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "Projects"}]}},
    }
    child = {
        "id": "child",
        "parent": {"type": "page_id", "page_id": "parent"},
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "Launch"}]}},
    }

    assert _page_path(
        child,
        {"parent": parent, "child": child},
        ("workspace",),
    ) == ("workspace", "Projects")


def test_internal_notion_links_become_relative_markdown_links() -> None:
    target_id = "11111111-2222-3333-4444-555555555555"
    body = (
        f'<page url="https://www.notion.so/Target-{target_id}">Target page</page>\n'
        f'[also target](https://www.notion.so/{target_id})'
    )

    rewritten = _rewrite_page_links(
        body,
        Path("sources/notion/workspace/current.md"),
        {target_id.replace("-", ""): Path("sources/notion/workspace/projects/target.md")},
    )

    assert rewritten == "[Target page](projects/target.md)\n[also target](projects/target.md)"


def test_link_rewrites_do_not_touch_latex_or_code() -> None:
    target_id = "11111111-2222-3333-4444-555555555555"
    url = f"https://www.notion.so/{target_id}"
    body = f"$f([x]({url}))$\n\n```latex\n\\text{{[x]({url})}}\n```\n\n[outside]({url})"

    rewritten = _rewrite_page_links(
        body,
        Path("sources/notion/workspace/current.md"),
        {target_id.replace("-", ""): Path("sources/notion/workspace/target.md")},
    )

    assert rewritten == f"$f([x]({url}))$\n\n```latex\n\\text{{[x]({url})}}\n```\n\n[outside](target.md)"


def test_notion_assets_are_mirrored_and_signed_url_is_removed() -> None:
    signed_url = (
        "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/space/diagram.png"
        "?X-Amz-Signature=temporary"
    )

    class AssetSession(Session):
        def get(self, url, **kwargs):
            if url == signed_url:
                return Response({}, content=b"image bytes")
            return super().get(url, **kwargs)

    documents = NotionClient(
        "placeholder",
        session=AssetSession(f"![Diagram]({signed_url})"),
    ).documents()

    assert signed_url not in documents[0].body
    assert documents[0].body.startswith("![Diagram](assets/")
    assert len(documents[0].assets) == 1
    assert documents[0].assets[0].content == b"image bytes"


def test_custom_output_root_and_path_prefix_are_used() -> None:
    documents = NotionClient(
        "test-token",
        output_root=Path("generated"),
        path_prefix=("team",),
        session=Session("Body"),
    ).documents()

    assert documents[0].path_parts == ("team",)
    assert documents[0].relative_path(Path("generated")).parts[:3] == (
        "generated",
        "notion",
        "team",
    )
