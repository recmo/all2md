from __future__ import annotations

import html
import re
import shutil
from pathlib import Path

from bs4 import BeautifulSoup

from .model import Block, Chapter, PageResult
from .util import atomic_text, slugify

LOCAL_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
IMAGE_LINK = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
HTML_TABLE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
TITLE_KINDS = {"title", "page_title", "heading", "section_header", "header"}
FIGURE_KINDS = {"embedded_figure", "figure", "image", "picture"}
BODY_BOUNDARY = re.compile(r"^(?:part|chapter)\b", re.IGNORECASE)
FRONT_MATTER_BOUNDARY = re.compile(
    r"^(?:praise|title page|copyright|contents|table of contents|dedication|foreword|preface)\b",
    re.IGNORECASE,
)
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
SMALL_TITLE_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "nor", "of", "on", "or", "over", "per", "the", "to", "up", "via", "with",
}
HEADING_ACRONYMS = {
    "ai": "AI", "aor": "AOR", "aors": "AORs", "api": "API", "apis": "APIs",
    "cbz": "CBZ", "ceo": "CEO", "ceos": "CEOs", "cfo": "CFO", "crm": "CRM",
    "djvu": "DjVu", "epub": "EPUB", "faq": "FAQ", "faqs": "FAQs", "gfm": "GFM",
    "hr": "HR", "ipo": "IPO", "kpi": "KPI", "kpis": "KPIs", "mlx": "MLX",
    "ocr": "OCR", "okr": "OKR", "okrs": "OKRs", "pdf": "PDF", "saas": "SaaS",
    "svg": "SVG", "ui": "UI", "ux": "UX", "vc": "VC",
}


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
    body = normalize_heading_case(body)
    if chapter:
        body = body.replace("](assets/", "](../assets/")
    return f"<!-- page: {page.number} -->\n\n{body}\n"


def strict_page_markdown(page: PageResult, outline: list[dict]) -> str:
    """Render OCR blocks as strict Markdown with document-level hierarchy."""
    entries = [item for item in outline if item.get("page") == page.number]
    boundary = max(entries, key=lambda item: item.get("level", 1), default=None)
    blocks = list(page.blocks)
    normalize_table_blocks(blocks)
    if boundary:
        blocks = _drop_visual_boundary_title(blocks, boundary["title"])

    body_pages = [
        item.get("page")
        for item in outline
        if BODY_BOUNDARY.match(str(item.get("title", "")).strip())
        and isinstance(item.get("page"), int)
    ]
    if not body_pages:
        body_pages = [
            item.get("page")
            for item in outline
            if not FRONT_MATTER_BOUNDARY.match(str(item.get("title", "")).strip())
            and isinstance(item.get("page"), int)
        ]
    cover_style = _is_cover_style_page(page, boundary)
    in_front_matter = (bool(body_pages) and page.number < min(body_pages)) or (
        page.number <= 3 and cover_style
    )

    current_level = 1
    preceding = [item for item in outline if item.get("page", 0) <= page.number]
    if preceding:
        current_level = preceding[-1].get("level", 1)
    pieces: list[str] = []
    suppressed_noise = False
    if boundary:
        title = title_case_heading(boundary["title"].strip())
        pieces.append(f"{'#' * min(6, max(1, boundary.get('level', 1)))} {title}")
    for block in blocks:
        content = block.markdown.strip()
        if not content:
            continue
        if in_front_matter and _is_counting_noise(content):
            suppressed_noise = True
            continue
        if block.kind in TITLE_KINDS:
            if in_front_matter and cover_style:
                continue
            if in_front_matter and boundary is None:
                content = HEADING.sub(lambda match: match.group(2), content)
            elif not HEADING.match(content):
                content = f"{'#' * min(6, current_level + 1)} {content}"
        content = normalize_heading_case(content)
        pieces.append(content)
    fallback = page.visual_markdown.strip()
    rendered = "\n\n".join(pieces).strip()
    if rendered or suppressed_noise:
        return rendered
    return normalize_heading_case(fallback)


def normalize_heading_case(markdown: str) -> str:
    """Title-case Markdown headings without touching link destinations."""
    return HEADING.sub(
        lambda match: f"{match.group(1)} {title_case_heading(match.group(2))}",
        markdown,
    )


def title_case_heading(value: str) -> str:
    visible = MARKDOWN_LINK.sub(lambda match: match.group(1), value)
    if not any(character.isalpha() for character in visible):
        return value

    def normalize_text(text: str) -> str:
        words = re.split(r"(\s+)", text)
        word_indexes = [index for index, word in enumerate(words) if word and not word.isspace()]
        last = word_indexes[-1] if word_indexes else -1
        normalized: list[str] = []
        capitalize_next = True
        for index, word in enumerate(words):
            if index not in word_indexes:
                normalized.append(word)
                continue
            rendered = _title_case_token(
                word,
                first=index == word_indexes[0] or capitalize_next,
                last=index == last,
            )
            normalized.append(rendered)
            capitalize_next = word.rstrip("*_`])}").endswith(":")
        return "".join(normalized)

    links: list[str] = []

    def protect_link(match: re.Match) -> str:
        links.append(f"[{normalize_text(match.group(1))}]({match.group(2)})")
        return f"§{len(links) - 1}§"

    protected = MARKDOWN_LINK.sub(protect_link, value)
    normalized = normalize_text(protected)
    for index, link in enumerate(links):
        normalized = normalized.replace(f"§{index}§", link)
    return normalized


def _title_case_token(token: str, *, first: bool, last: bool) -> str:
    match = re.match(r"^([^A-Za-z0-9]*)(.*?)([^A-Za-z0-9']*)$", token)
    if match is None or not match.group(2):
        return token
    prefix, core, suffix = match.groups()
    parts = re.split(r"(-)", core)
    normalized: list[str] = []
    word_parts = [index for index, part in enumerate(parts) if part != "-"]
    for index, part in enumerate(parts):
        if part == "-":
            normalized.append(part)
            continue
        folded = part.casefold()
        if folded in HEADING_ACRONYMS:
            normalized.append(HEADING_ACRONYMS[folded])
        elif re.fullmatch(r"[IVXLCDM]+", part):
            normalized.append(part)
        elif folded in SMALL_TITLE_WORDS and not (first and index == word_parts[0]) and not (last and index == word_parts[-1]):
            normalized.append(folded)
        elif any(character.islower() for character in part) and any(character.isupper() for character in part):
            normalized.append(part)
        else:
            normalized.append(folded[:1].upper() + folded[1:])
    return prefix + "".join(normalized) + suffix


def _is_cover_style_page(page: PageResult, boundary: dict | None) -> bool:
    if boundary and str(boundary.get("title", "")).casefold() == "title page":
        return True
    for block in page.blocks:
        if block.kind not in FIGURE_KINDS or block.bbox is None:
            continue
        left, top, right, bottom = block.bbox
        if max(block.bbox) <= 1100 and max(0, right - left) * max(0, bottom - top) >= 450_000:
            return True
    return False


def _is_counting_noise(value: str) -> bool:
    if re.search(r"[A-Za-z]", value):
        return False
    numbers = [int(number) for number in re.findall(r"\b(\d{1,3})\s*\.", value)]
    return len(numbers) >= 10 and numbers == list(range(numbers[0], numbers[0] + len(numbers)))


def html_tables_to_markdown(markdown: str) -> str:
    return HTML_TABLE.sub(lambda match: _table_to_markdown(match.group(0)), markdown)


def normalize_table_blocks(blocks: list[Block]) -> None:
    for block in blocks:
        if block.kind != "table" or "<table" not in block.markdown.casefold():
            continue
        reason = table_fallback_reason(block.markdown)
        if reason:
            block.metadata["render_format"] = "html"
            block.metadata["html_fallback_reason"] = reason
        else:
            block.markdown = _table_to_markdown(block.markdown)
            block.metadata["render_format"] = "gfm"


def table_fallback_reason(source: str) -> str | None:
    soup = BeautifulSoup(source, "html.parser")
    table = soup.find("table")
    if table is None or not table.find("tr"):
        return "malformed_table"
    if table.find("table") is not None:
        return "nested_table"
    for cell in table.find_all(["th", "td"]):
        if int(cell.get("colspan", 1)) > 1:
            return "column_span"
        if cell.find(["p", "ul", "ol", "pre", "blockquote", "br"]):
            return "multiline_or_nested_cell"
    return None


def merge_html_tables(
    first: str,
    continuation: str,
    *,
    adjacent: bool = False,
    boundary_geometry: bool = False,
) -> str | None:
    first_soup = BeautifulSoup(first, "html.parser")
    next_soup = BeautifulSoup(continuation, "html.parser")
    first_table, next_table = first_soup.find("table"), next_soup.find("table")
    if first_table is None or next_table is None:
        return None
    first_rows, next_rows = first_table.find_all("tr"), next_table.find_all("tr")
    if not first_rows or not next_rows:
        return None
    repeated_header = _row_signature(first_rows[0]) == _row_signature(next_rows[0])
    if not repeated_header and not (adjacent and boundary_geometry):
        return None
    if not repeated_header and _row_width(first_rows[0]) != _row_width(next_rows[0]):
        return None
    for row in next_rows[1 if repeated_header else 0 :]:
        first_table.append(row.extract())
    return str(first_table)


def _row_signature(row) -> tuple[str, ...]:
    values = (" ".join(cell.get_text(" ", strip=True).casefold().split()) for cell in row.find_all(["th", "td"]))
    return tuple(value for value in values if value)


def _row_width(row) -> int:
    return sum(max(1, int(cell.get("colspan", 1))) for cell in row.find_all(["th", "td"], recursive=False))


def _table_to_markdown(source: str) -> str:
    if table_fallback_reason(source):
        return source.strip()
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
    rendered_title = title_case_heading(title)
    if not split:
        shutil.rmtree(root / "chapters", ignore_errors=True)
        content = f"# {rendered_title}\n\n" + "\n".join(
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
    index_lines = [f"# {rendered_title}", "", "## Contents", ""]
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
        chapter_title = title_case_heading(chapter.title)
        content = f"# {chapter_title}\n\n" + "\n".join(
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
        index_lines.append(f"- [{chapter_title}](chapters/{filename})")
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
