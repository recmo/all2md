from __future__ import annotations

import html
import re
import shutil
from pathlib import Path

from bs4 import BeautifulSoup

from .model import Chapter, PageResult
from .util import atomic_text, slugify

LOCAL_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
IMAGE_LINK = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
HTML_TABLE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
TITLE_KINDS = {"title", "page_title", "heading", "section_header", "header"}


def page_markdown(
    page: PageResult,
    *,
    chapter: bool,
    suppress_title: str | None = None,
    heading_delta: int = 0,
) -> str:
    body = page.visual_markdown.strip()
    if suppress_title:
        body = _remove_duplicate_heading(body, suppress_title)
    if heading_delta:
        body = _relevel_headings(body, heading_delta)
    if chapter:
        body = body.replace("](assets/", "](../assets/")
    return f"<!-- page: {page.number} -->\n\n{body}\n"


def strict_page_markdown(page: PageResult, outline: list[dict]) -> str:
    """Render OCR blocks as strict Markdown with document-level hierarchy."""
    entries = [item for item in outline if item.get("page") == page.number]
    boundary = max(entries, key=lambda item: item.get("level", 1), default=None)
    blocks = list(page.blocks)
    if boundary:
        blocks = _drop_visual_boundary_title(blocks, boundary["title"])

    current_level = 1
    preceding = [item for item in outline if item.get("page", 0) <= page.number]
    if preceding:
        current_level = preceding[-1].get("level", 1)
    pieces: list[str] = []
    if boundary:
        pieces.append(f"{'#' * min(6, max(1, boundary.get('level', 1)))} {boundary['title'].strip()}")
    for block in blocks:
        content = html_tables_to_markdown(block.markdown.strip())
        if not content:
            continue
        if block.kind in TITLE_KINDS and not HEADING.match(content):
            content = f"{'#' * min(6, current_level + 1)} {content}"
        pieces.append(content)
    fallback = html_tables_to_markdown(page.visual_markdown.strip())
    return "\n\n".join(pieces).strip() or fallback


def html_tables_to_markdown(markdown: str) -> str:
    return HTML_TABLE.sub(lambda match: _table_to_markdown(match.group(0)), markdown)


def merge_html_tables(first: str, continuation: str) -> str | None:
    first_soup = BeautifulSoup(first, "html.parser")
    next_soup = BeautifulSoup(continuation, "html.parser")
    first_table, next_table = first_soup.find("table"), next_soup.find("table")
    if first_table is None or next_table is None:
        return None
    first_rows, next_rows = first_table.find_all("tr"), next_table.find_all("tr")
    if not first_rows or not next_rows or _row_signature(first_rows[0]) != _row_signature(next_rows[0]):
        return None
    for row in next_rows[1:]:
        first_table.append(row.extract())
    return str(first_table)


def _row_signature(row) -> tuple[str, ...]:
    values = (" ".join(cell.get_text(" ", strip=True).casefold().split()) for cell in row.find_all(["th", "td"]))
    return tuple(value for value in values if value)


def _table_to_markdown(source: str) -> str:
    soup = BeautifulSoup(source, "html.parser")
    table = soup.find("table")
    if table is None:
        return source
    source_rows = table.find_all("tr")
    if not source_rows:
        return ""

    def cells(row) -> list[str]:
        values: list[str] = []
        for cell in row.find_all(["th", "td"], recursive=False):
            value = " ".join(cell.get_text(" ", strip=True).split())
            value = html.unescape(value).replace("|", r"\|").replace("\n", "<br>")
            values.extend([value, *([""] * (max(1, int(cell.get("colspan", 1))) - 1))])
        return values

    header = [value for value in cells(source_rows[0]) if value]
    if not header:
        return ""
    width = len(header)
    rows: list[list[str]] = []
    current_group = ""
    group_rows_remaining = 0
    header_folded = [value.casefold() for value in header]
    for source_row in source_rows[1:]:
        cell_nodes = source_row.find_all(["th", "td"], recursive=False)
        row = cells(source_row)
        folded = [value.casefold() for value in row if value]
        if folded[:width] == header_folded:
            continue
        if (
            len(row) >= width
            and row[0].casefold().startswith("department ")
            and [value.casefold() for value in row[1:] if value] == header_folded[1:]
        ):
            current_group = row[0][len("Department ") :].strip()
            group_rows_remaining = max(0, int(cell_nodes[0].get("rowspan", 1)) - 1)
            continue

        first_span = int(cell_nodes[0].get("rowspan", 1)) if cell_nodes else 1
        if first_span > 1:
            if row and row[0]:
                current_group = row[0]
            payload = row[1:]
            group_rows_remaining = first_span - 1
        elif group_rows_remaining > 0:
            payload = row
            group_rows_remaining -= 1
        elif len(row) >= width:
            if row[0]:
                current_group = row[0]
            payload = row[1:]
        else:
            payload = row
        while len(payload) > width - 1 and payload and not payload[-1]:
            payload.pop()
        while len(payload) > width - 1 and payload and not payload[0]:
            payload.pop(0)
        payload = payload[: width - 1] + [""] * max(0, width - 1 - len(payload))
        normalized = [current_group, *payload]
        if any(payload):
            rows.append(normalized)
    separator = ["---"] * width
    return "\n".join(
        "| " + " | ".join(row) + " |" for row in [header, separator, *rows]
    )


def write_markdown(
    root: Path,
    pages: list[PageResult],
    chapters: list[Chapter],
    *,
    split: bool,
    title: str,
    outline: list[dict] | None = None,
) -> list[str]:
    outline = outline or []
    written: list[str] = []
    if not split:
        shutil.rmtree(root / "chapters", ignore_errors=True)
        content = f"# {title}\n\n" + "\n".join(
            page_markdown(page, chapter=False, heading_delta=1) for page in pages
        )
        content = _rewrite_page_links(
            content,
            _page_targets(pages, chapters, ["book.md"] * len(chapters)),
            "book.md",
        )
        atomic_text(root / "book.md", content.rstrip() + "\n")
        return ["book.md"]

    chapter_dir = root / "chapters"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    for stale_markdown in chapter_dir.glob("*.md"):
        stale_markdown.unlink()
    index_lines = [f"# {title}", "", "## Contents", ""]
    chapter_files = [f"{index:03d}-{chapter.slug}.md" for index, chapter in enumerate(chapters)]
    targets = _page_targets(pages, chapters, chapter_files)
    for index, chapter in enumerate(chapters):
        filename = chapter_files[index]
        selected = [page for page in pages if chapter.start_page <= page.number <= chapter.end_page]
        if chapter.title.casefold() in {"front matter", "back matter"}:
            heading_delta = 1
        elif re.match(r"^(chapter|introduction|conclusion|appendix)\b", chapter.title, re.I):
            heading_delta = -1
        else:
            heading_delta = 0
        content = f"# {chapter.title}\n\n" + "\n".join(
            page_markdown(
                page,
                chapter=True,
                suppress_title=chapter.title if page.number == chapter.start_page else None,
                heading_delta=heading_delta,
            )
            for page in selected
        )
        content = _rewrite_page_links(content, targets, filename)
        atomic_text(chapter_dir / filename, content.rstrip() + "\n")
        index_lines.append(f"- [{chapter.title}](chapters/{filename})")
        written.append(f"chapters/{filename}")
    atomic_text(root / "book.md", "\n".join(index_lines) + "\n")
    return ["book.md", *written]


def markdown_anchors(markdown: str) -> set[str]:
    anchors = set(re.findall(r'<a\s+id=["\']([^"\']+)["\']\s*></a>', markdown))
    counts: dict[str, int] = {}
    for _, heading in HEADING.findall(markdown):
        base = slugify(re.sub(r"[*_`]", "", heading))
        count = counts.get(base, 0)
        anchors.add(base if count == 0 else f"{base}-{count}")
        counts[base] = count + 1
    return anchors


def _page_targets(
    pages: list[PageResult], chapters: list[Chapter], chapter_files: list[str]
) -> dict[int, tuple[str, str | None]]:
    targets: dict[int, tuple[str, str | None]] = {}
    for index, chapter in enumerate(chapters):
        filename = chapter_files[index]
        for number in range(chapter.start_page, chapter.end_page + 1):
            targets[number] = (filename, slugify(chapter.title) if number == chapter.start_page else None)
    for page in pages:
        match = HEADING.search(page.visual_markdown)
        if match and page.number in targets:
            filename, existing = targets[page.number]
            targets[page.number] = (filename, existing or slugify(match.group(2)))
    return targets


def _rewrite_page_links(markdown: str, targets: dict[int, tuple[str, str | None]], current_file: str) -> str:
    pattern = re.compile(r"\[([^\]]+)\]\(#page-(\d+)\)")

    def replace(match: re.Match) -> str:
        target = targets.get(int(match.group(2)))
        if target is None:
            return match.group(1)
        filename, anchor = target
        if filename == current_file:
            destination = f"#{anchor}" if anchor else ""
        else:
            destination = filename + (f"#{anchor}" if anchor else "")
        return f"[{match.group(1)}]({destination})" if destination else match.group(1)

    return pattern.sub(replace, markdown)


def _remove_duplicate_heading(markdown: str, title: str) -> str:
    for match in HEADING.finditer(markdown):
        if _same_heading(match.group(2), title):
            return (markdown[: match.start()] + markdown[match.end() :]).strip()
        break
    return markdown


def _relevel_headings(markdown: str, delta: int) -> str:
    return HEADING.sub(
        lambda match: f"{'#' * min(6, max(1, len(match.group(1)) + delta))} {match.group(2)}",
        markdown,
    )


def _drop_visual_boundary_title(blocks, title: str):
    limit = min(3, len(blocks))
    for count in range(1, limit + 1):
        candidate = " ".join(block.markdown for block in blocks[:count])
        if _same_heading(candidate, title):
            return blocks[count:]
        if blocks[count - 1].bbox and blocks[count - 1].bbox[1] > 320:
            break
    return blocks


def _same_heading(left: str, right: str) -> bool:
    from difflib import SequenceMatcher

    def canonical(value: str) -> str:
        value = re.sub(r"^#{1,6}\s+", "", value.strip())
        value = re.sub(r"\bCHAPTER\s+I\b", "CHAPTER 1", value, flags=re.I)
        value = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
        return value

    a, b = canonical(left), canonical(right)
    return bool(a and b) and (a == b or SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.82)


def local_links(markdown: str) -> list[str]:
    links = [target for _, target in IMAGE_LINK.findall(markdown)]
    links.extend(
        target for _, target in LOCAL_LINK.findall(markdown) if not target.startswith(("http://", "https://", "mailto:"))
    )
    return links
