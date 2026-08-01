from __future__ import annotations

import json
from pathlib import Path
import re
import statistics
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
        coarse = chapter_levels and _prefer_coarse_file_units(source.outline, boundary_level, pages)
        if coarse:
            starts = _coarse_boundaries(source.outline, boundary_level, page_numbers)
        else:
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


def _prefer_coarse_file_units(outline: list[dict], chapter_level: int, pages: list[PageResult]) -> bool:
    page_numbers = [page.number for page in pages]
    parts = [
        item for item in outline
        if item["level"] < chapter_level and re.match(r"^\s*part\b", item["title"], re.IGNORECASE)
    ]
    chapters = [item for item in outline if item["level"] == chapter_level and item["page"] in page_numbers]
    if len(parts) < 2 or len(chapters) < 2:
        return False
    starts = sorted(item["page"] for item in chapters)
    byte_sizes: list[int] = []
    page_bytes = {page.number: len(page.visual_markdown.encode("utf-8")) for page in pages}
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else max(page_numbers) + 1
        byte_sizes.append(sum(size for number, size in page_bytes.items() if start <= number < end))
    short_share = sum(size < 16 * 1024 for size in byte_sizes) / len(byte_sizes)
    part_starts = sorted(item["page"] for item in parts if item["page"] in page_numbers)
    part_sizes = []
    for index, start in enumerate(part_starts):
        end = part_starts[index + 1] if index + 1 < len(part_starts) else max(page_numbers) + 1
        part_sizes.append(sum(size for number, size in page_bytes.items() if start <= number < end))
    parts_are_reasonable = bool(part_sizes) and max(part_sizes) <= 512 * 1024
    short_chapters = len(chapters) > 32 or statistics.median(byte_sizes) < 16 * 1024 or short_share >= 0.6
    return parts_are_reasonable and short_chapters


def _coarse_boundaries(
    outline: list[dict], chapter_level: int, page_numbers: list[int]
) -> list[tuple[str, int]]:
    parts = [
        item
        for item in outline
        if item["level"] < chapter_level
        and re.match(r"^\s*part\b", item["title"], re.IGNORECASE)
        and item["page"] in page_numbers
    ]
    if not parts:
        return []
    starts = [(item["title"], item["page"]) for item in parts]
    after_parts = [
        item
        for item in outline
        if item["level"] < chapter_level
        and item["page"] > parts[-1]["page"]
        and item["page"] in page_numbers
        and not re.match(r"^\s*chapter\b", item["title"], re.IGNORECASE)
    ]
    if after_parts:
        starts.append(("Back matter", after_parts[0]["page"]))
    return starts


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
