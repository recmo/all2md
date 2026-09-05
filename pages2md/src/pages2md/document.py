"""Document-wide normalization and evidence-backed link application."""
from __future__ import annotations

import re
from .model import FIGURE_KINDS, Block, PageResult
from .lists import normalize_lists, editable_leaves
from .markdown import merge_html_tables
from .embedded import bbox_coverage as _bbox_coverage
from .syntax import protected_ranges



def merge_continued_tables(pages: list[PageResult]) -> None:
    active: Block | None = None
    active_page: int | None = None
    for page in pages:
        retained: list[Block] = []
        for block in page.blocks:
            if block.kind == "table":
                if active is not None:
                    boundary_geometry = bool(
                        active.bbox
                        and block.bbox
                        and active.bbox[3] >= 800
                        and block.bbox[1] <= 250
                    )
                    merged = merge_html_tables(
                        active.markdown,
                        block.markdown,
                        adjacent=active_page is not None and page.number == active_page + 1,
                        boundary_geometry=boundary_geometry,
                    )
                    if merged is not None:
                        active.markdown = merged
                        active.source_pages = sorted(set([*active.source_pages, *block.source_pages, page.number]))
                        active.provenance.extend(block.provenance)
                        active.metadata["multi_page_table"] = True
                        active_page = page.number
                        continue
                active = block
                active_page = page.number
                retained.append(block)
            else:
                retained.append(block)
                if block.kind not in FIGURE_KINDS | {"footer"} and block.markdown.strip():
                    active = None
                    active_page = None
        page.blocks = retained


def normalize_document(pages: list[PageResult]) -> None:
    """Apply deterministic structural cleanup to normalized blocks, never raw OCR."""
    from collections import Counter
    from .compare import normalize

    repeated = Counter()
    repeated_top = Counter()
    for page in pages:
        for index, block in enumerate(page.blocks):
            if not block.markdown.strip() or not block.bbox:
                if index < 2:
                    key = normalize(block.markdown)
                    if key and len(key) <= 120:
                        repeated_top[key] += 1
                continue
            if block.kind in {"header", "footer"} or block.bbox[1] <= 80 or block.bbox[3] >= 940:
                key = normalize(block.markdown)
                if key:
                    repeated[key] += 1
    boilerplate = {key for key, count in repeated.items() if count >= 2}
    ungrounded_boilerplate = {key for key, count in repeated_top.items() if count >= 2}
    for page in pages:
        for block in page.blocks:
            if not block.source_pages:
                block.source_pages = [page.number]
        retained = []
        for index, block in enumerate(page.blocks):
            normalized = normalize(block.markdown)
            running_matter = bool(
                block.kind in {"page_number", "header", "footer"}
                or (
                    block.bbox
                    and normalized in boilerplate
                    and (block.bbox[1] <= 80 or block.bbox[3] >= 940)
                )
                or (
                    index < 2
                    and not block.bbox
                    and (normalized in ungrounded_boilerplate or bool(re.fullmatch(r"[ivxlcdm]+|\d+", normalized, re.I)))
                )
            )
            if not running_matter:
                retained.append(block)
        page.blocks = retained

    normalize_lists(pages)
    _trim_adjacent_duplicate_blocks(pages)

    for previous, current in zip(pages, pages[1:]):
        if current.number != previous.number + 1 or not previous.blocks or not current.blocks:
            continue
        body = [b for b in previous.blocks if not b.metadata.get("footnote")]
        if not body:
            continue
        left, right = body[-1], current.blocks[0]
        if (
            left.kind == right.kind == "paragraph"
            and left.markdown.rstrip()
            and right.markdown.lstrip()
            and left.markdown.rstrip()[-1] not in ".!?;:"
            and right.markdown.lstrip()[0].islower()
        ):
            left.markdown = f"{left.markdown.rstrip()} {right.markdown.lstrip()}"
            left.source_pages = sorted(set([*left.source_pages, *right.source_pages, previous.number, current.number]))
            left.provenance.extend(right.provenance)
            left.metadata["cross_page_paragraph"] = True
            current.blocks.pop(0)

    for page in pages:
        with editable_leaves(page.blocks) as leaves:
            for block in leaves:
                if block.kind == "paragraph":
                    block.markdown = _clean_prose(block.markdown)

        retained: list[Block] = []
        index = 0
        while index < len(page.blocks):
            block = page.blocks[index]
            if block.kind in FIGURE_KINDS and index + 1 < len(page.blocks):
                caption = page.blocks[index + 1]
                caption_text = caption.markdown.strip()
                close = bool(
                    block.bbox
                    and caption.bbox
                    and 0 <= caption.bbox[1] - block.bbox[3] <= 120
                )
                if caption.kind == "caption" and close:
                    block.markdown = f"{block.markdown.rstrip()}\n\n*{caption_text}*"
                    block.metadata["caption"] = caption_text
                    block.provenance.extend(caption.provenance)
                    retained.append(block)
                    index += 2
                    continue
            retained.append(block)
            index += 1
        page.blocks = retained


def _trim_adjacent_duplicate_blocks(pages: list[PageResult]) -> None:
    """Keep duplicated OCR content on the physical page supported by evidence."""
    from difflib import SequenceMatcher
    from .compare import normalize

    for previous, current in zip(pages, pages[1:]):
        if current.number != previous.number + 1 or not previous.blocks or not current.blocks:
            continue
        left, right = previous.blocks[-1], current.blocks[0]
        left_text, right_text = left.markdown.strip(), right.markdown.strip()
        left_norm, right_norm = normalize(left_text), normalize(right_text)
        if min(len(left_norm), len(right_norm)) < 120:
            continue
        exact = left_text.rfind(right_text)
        if exact >= 0 and exact >= len(left_text) * 0.25:
            left.markdown = left_text[:exact].rstrip()
            left.metadata["trimmed_adjacent_page_overlap"] = current.number
            previous.warnings.append("visual_adjacent_page_overlap_repaired")
            continue
        longest = SequenceMatcher(None, left_text, right_text, autojunk=False).find_longest_match()
        if (
            longest.size >= 0.75 * min(len(left_text), len(right_text))
            and longest.a + longest.size >= len(left_text) - 80
            and longest.b <= 80
        ):
            left.markdown = left_text[: longest.a].rstrip()
            left.metadata["trimmed_adjacent_page_overlap"] = current.number
            previous.warnings.append("visual_adjacent_page_overlap_repaired")
            continue
        similarity = SequenceMatcher(None, left_norm, right_norm, autojunk=False).ratio()
        if similarity < 0.92:
            continue
        left_support = SequenceMatcher(
            None, left_norm, normalize(previous.embedded.text), autojunk=False
        ).ratio()
        right_support = SequenceMatcher(
            None, right_norm, normalize(current.embedded.text), autojunk=False
        ).ratio()
        if right_support > left_support + 0.05:
            previous.blocks.pop()
            previous.warnings.append("visual_adjacent_page_overlap_repaired")
        elif left_support > right_support + 0.05:
            current.blocks.pop(0)
            current.warnings.append("visual_adjacent_page_overlap_repaired")


def _clean_prose(value: str) -> str:
    lines = [re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in value.splitlines()]
    value = "\n".join(lines).strip()
    value = re.sub(r"\\\)\s+([,.;:!?])", lambda match: r"\)" + match.group(1), value)
    return value




def apply_links_to_blocks(
    blocks: list[Block],
    links,
    *,
    page_number: int | None = None,
) -> None:
    with editable_leaves(blocks) as blocks:
        for link in links:
            label = " ".join(link.text.split())
            if not label or not link.target:
                continue
            # PDF GoTo annotations identify a destination page, not the exact
            # target block. Turning them into heading anchors can silently link a
            # theorem or equation reference to an unrelated heading on that page.
            if not link.external:
                continue
            if page_number is not None and link.target == f"#page-{page_number}":
                continue
            if any(f"]({link.target})" in block.markdown for block in blocks):
                continue
            eligible = [
                block
                for block in blocks
                if block.kind not in {"table", "figure"}
            ]
            if link.bbox:
                eligible = [
                    block
                    for block in eligible
                    if block.bbox and _bbox_coverage(link.bbox, block.bbox) >= 0.35
                ]
                eligible.sort(
                    key=lambda block: _bbox_coverage(link.bbox, block.bbox),
                    reverse=True,
                )
            candidates = [
                block for block in eligible if _link_label_pattern(label).search(block.markdown)
            ]
            for block in candidates:
                updated, count = _replace_plain_text_once(block.markdown, label, link.target)
                if count:
                    block.markdown = updated
                    block.metadata.setdefault("links", []).append(
                        {"target": link.target, "source": "embedded_link_geometry"}
                    )
                    break
            else:
                if link.target.casefold().startswith("https://doi.org/"):
                    for block in eligible:
                        updated = _replace_doi_tail(block.markdown, link.target)
                        if updated == block.markdown:
                            continue
                        block.markdown = updated
                        block.metadata.setdefault("links", []).append(
                            {"target": link.target, "source": "embedded_doi_target_geometry"}
                        )
                        break


def _replace_doi_tail(markdown: str, target: str) -> str:
    doi = target[len("https://doi.org/") :].strip().rstrip(".")
    if not doi.startswith("10."):
        return markdown
    pattern = re.compile(r"(?is)(\bdoi\s*:\s*)(?:10\..*)$")
    match = pattern.search(markdown)
    if not match:
        return markdown
    suffix = "." if markdown.rstrip().endswith(".") else ""
    replacement = f"{match.group(1)}[{doi}]({target}){suffix}"
    return markdown[: match.start()] + replacement


def _replace_plain_text_once(markdown: str, label: str, target: str) -> tuple[str, int]:
    """Link one visible occurrence without entering existing Markdown constructs."""
    pattern = _link_label_pattern(label)
    cursor = 0
    for start, end in protected_ranges(markdown):
        match = pattern.search(markdown, cursor, start)
        if match:
            replacement = f"[{match.group(0)}]({target})"
            return markdown[: match.start()] + replacement + markdown[match.end() :], 1
        cursor = end
    match = pattern.search(markdown, cursor)
    if not match:
        return markdown, 0
    replacement = f"[{match.group(0)}]({target})"
    return markdown[: match.start()] + replacement + markdown[match.end() :], 1


def _link_label_pattern(label: str) -> re.Pattern[str]:
    prefix = r"(?<!\w)" if label[:1].isalnum() else ""
    suffix = r"(?!\w)" if label[-1:].isalnum() else ""
    return re.compile(prefix + re.escape(label) + suffix, re.IGNORECASE)
