from __future__ import annotations

import json
from pathlib import Path
import re
from difflib import SequenceMatcher

from .model import Chapter, PageResult, SourceDocument
from .util import slugify


def chapters_from_map(path: Path, page_numbers: list[int]) -> list[Chapter]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("chapter map must be a non-empty JSON array")
    starts = []
    for item in data:
        starts.append((str(item["title"]), int(item["start_page"])))
    return _boundaries_to_chapters(starts, page_numbers, "manual", 1.0)


def detect_chapters(source: SourceDocument, pages: list[PageResult]) -> list[Chapter]:
    page_numbers = [page.number for page in pages]
    if source.outline:
        minimum_level = min(item["level"] for item in source.outline)
        chapter_levels = [
            item["level"]
            for item in source.outline
            if re.match(r"^\s*chapter\b", item["title"], re.IGNORECASE)
        ]
        boundary_level = min(chapter_levels) if chapter_levels else minimum_level
        starts = [
            (item["title"], item["page"])
            for item in source.outline
            if item["level"] <= boundary_level and item["page"] in page_numbers
        ]
        if len(starts) >= 2:
            return _boundaries_to_chapters(starts, page_numbers, "document_outline", 1.0)

    toc_starts = _toc_boundaries(pages)
    if len(toc_starts) >= 2:
        return _boundaries_to_chapters(toc_starts, page_numbers, "visual_toc", 0.85)

    starts: list[tuple[str, int]] = []
    for page in pages:
        title_blocks = [
            block for block in page.blocks if block.kind in {"title", "page_title", "heading", "section_header"}
        ]
        if not title_blocks:
            continue
        title = title_blocks[0].markdown.strip().splitlines()[0][:160]
        if title and (title.lower().startswith(("chapter ", "part ", "appendix ")) or len(title) < 80):
            starts.append((title, page.number))
    if len(starts) >= 2:
        return _boundaries_to_chapters(starts, page_numbers, "visual_headings", 0.8)
    return []


def _toc_boundaries(pages: list[PageResult]) -> list[tuple[str, int]]:
    entries: list[tuple[str, int]] = []
    entry_pattern = re.compile(r"^\s*(.+?)\s*(?:\.{2,}|\s{2,})\s*(\d+)\s*$")
    for page in pages[: min(20, len(pages))]:
        for line in page.visual_markdown.splitlines():
            match = entry_pattern.match(line)
            if match:
                entries.append((match.group(1).strip(" ."), int(match.group(2))))
    headings = [
        (block.markdown.strip().splitlines()[0], page.number)
        for page in pages
        for block in page.blocks
        if block.kind in {"title", "page_title", "heading", "section_header"} and block.markdown.strip()
    ]
    matched: list[tuple[str, int]] = []
    last_page = 0
    for title, printed_page in entries:
        candidates = [
            (SequenceMatcher(None, title.casefold(), heading.casefold()).ratio(), heading, page_number)
            for heading, page_number in headings
            if page_number > last_page
        ]
        if not candidates:
            continue
        score, heading, page_number = max(candidates)
        if score >= 0.82:
            matched.append((heading, page_number))
            last_page = page_number
    return matched


def _boundaries_to_chapters(
    starts: list[tuple[str, int]], page_numbers: list[int], evidence: str, confidence: float
) -> list[Chapter]:
    starts = sorted(dict((page, title) for title, page in starts).items())
    first_page, last_page = min(page_numbers), max(page_numbers)
    chapters: list[Chapter] = []
    if starts and starts[0][0] > first_page:
        chapters.append(Chapter("Front matter", first_page, starts[0][0] - 1, "front-matter", confidence, evidence))
    for index, (start_page, title) in enumerate(starts):
        end_page = starts[index + 1][0] - 1 if index + 1 < len(starts) else last_page
        if end_page < first_page or start_page > last_page:
            continue
        chapters.append(
            Chapter(
                title=title,
                start_page=max(first_page, start_page),
                end_page=min(last_page, end_page),
                slug=slugify(title, f"chapter-{index + 1}"),
                confidence=confidence,
                evidence=evidence,
            )
        )
    return chapters
