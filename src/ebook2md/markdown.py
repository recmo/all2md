from __future__ import annotations

import re
from pathlib import Path

from .model import Chapter, PageResult
from .util import atomic_text

LOCAL_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
IMAGE_LINK = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def page_markdown(page: PageResult, *, chapter: bool) -> str:
    body = page.visual_markdown.strip()
    if chapter:
        body = body.replace("](assets/", "](../assets/")
    return f"<a id=\"page-{page.number}\"></a>\n<!-- page: {page.number} -->\n\n{body}\n"


def write_markdown(
    root: Path,
    pages: list[PageResult],
    chapters: list[Chapter],
    *,
    split: bool,
    title: str,
) -> list[str]:
    written: list[str] = []
    if not split:
        content = f"# {title}\n\n" + "\n".join(page_markdown(page, chapter=False) for page in pages)
        atomic_text(root / "book.md", content.rstrip() + "\n")
        return ["book.md"]

    chapter_dir = root / "chapters"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    index_lines = [f"# {title}", "", "## Contents", ""]
    chapter_files = [f"{index:03d}-{chapter.slug}.md" for index, chapter in enumerate(chapters)]
    page_files = {
        page_number: chapter_files[index]
        for index, chapter in enumerate(chapters)
        for page_number in range(chapter.start_page, chapter.end_page + 1)
    }
    for index, chapter in enumerate(chapters):
        filename = chapter_files[index]
        selected = [page for page in pages if chapter.start_page <= page.number <= chapter.end_page]
        content = f"# {chapter.title}\n\n" + "\n".join(page_markdown(page, chapter=True) for page in selected)
        for target_page, target_file in page_files.items():
            content = content.replace(f"](#page-{target_page})", f"]({target_file}#page-{target_page})")
        atomic_text(chapter_dir / filename, content.rstrip() + "\n")
        index_lines.append(f"- [{chapter.title}](chapters/{filename})")
        written.append(f"chapters/{filename}")
    atomic_text(root / "book.md", "\n".join(index_lines) + "\n")
    return ["book.md", *written]


def local_links(markdown: str) -> list[str]:
    links = [target for _, target in IMAGE_LINK.findall(markdown)]
    links.extend(
        target for _, target in LOCAL_LINK.findall(markdown) if not target.startswith(("http://", "https://", "mailto:"))
    )
    return links
