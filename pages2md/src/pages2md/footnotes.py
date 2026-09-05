"""Match OCR-owned footnote bodies to references; never invent note text."""
from __future__ import annotations

import re
from collections import Counter
from statistics import median
from markdown_it import MarkdownIt

from .alignment import Projection, _baseline, _em, align_glyphs
from .embedded import assess_embedded, embedded_characters_for_bbox
from .model import Block, PageResult
from .semantics import _math_letter, protected_ranges


MARK = r"(?:\d{1,3}|\*{1,3}|†|‡|§|¶|\\dagger|\\ddagger)"
SUPER = rf"(?:\\\(\s*\^\s*\{{\s*({MARK})\s*\}}\s*\\\)|\$\s*\^\s*\{{\s*({MARK})\s*\}}\s*\$)"
LEADING = re.compile(rf"^\s*(?:{SUPER}|({MARK}))[ \t]*")
REFERENCE = re.compile(rf"{SUPER}|(?<=[\w.)])([*†‡§¶]{{1,3}})|(?<=[A-Za-z.)])(\d{{1,3}})(?!\d)")
NOTE_KINDS = {"footnote", "page_footnote"}


def _label(match: re.Match) -> str:
    return next(value for value in match.groups() if value).replace(r"\ddagger", "‡").replace(r"\dagger", "†")


def _glyphs(page: PageResult, block: Block) -> list[dict]:
    return embedded_characters_for_bbox(page.embedded, block.bbox) if block.bbox else []


def _body_size(glyphs: list[dict]) -> float:
    sizes = [float(g["size"]) for g in glyphs if g.get("size") and g["text"].isalpha()]
    return median(sizes) if sizes else 0


def _raised_reference(page: PageResult, block: Block, match: re.Match, project: Projection) -> bool:
    """Require a raised marker beside prose, not an equation's exponent."""
    aligned = align_glyphs(block.markdown, page.embedded, block.bbox, project)
    preceding = [i for i, (_, end) in enumerate(aligned.spans) if end <= match.start()]
    if not preceding:
        return False
    native = aligned.matches.get(preceding[-1])
    if native is None:
        return False
    base = aligned.glyphs[native]
    if _math_letter(base) or not base.get("origin"):
        return False
    ex, ey = _em(base)
    label = _label(match)
    candidates = []
    for glyph in _glyphs(page, block):
        if glyph["text"].replace("∗", "*") not in label or not glyph.get("origin"):
            continue
        dx = glyph["bbox"][0] - base["bbox"][2]
        dy = _baseline(glyph) - _baseline(base)
        if -.2 * ex <= dx <= 1.5 * ex and -.9 * ey <= dy <= -.12 * ey:
            candidates.append(glyph)
    return "".join(g["text"].replace("∗", "*") for g in sorted(candidates, key=lambda g: g["bbox"][0])) == label


def normalize_footnotes(pages: list[PageResult], project: Projection) -> None:
    for page in pages:
        trusted = assess_embedded(page.embedded, page.visual_markdown).geometric
        sizes = [_body_size(_glyphs(page, b)) for b in page.blocks
                 if b.kind == "paragraph" and b.bbox and b.bbox[1] < 650]
        body_size = median([s for s in sizes if s]) if any(sizes) else 0
        candidates = []
        for block in page.blocks:
            if block.metadata.get("footnote") or block.kind not in NOTE_KINDS | {"paragraph"}:
                continue
            lead = LEADING.match(block.markdown)
            if not lead or len(block.markdown[lead.end():].strip()) < 12:
                continue
            labelled = block.kind in NOTE_KINDS
            size = _body_size(_glyphs(page, block)) if trusted else 0
            inferred = bool(trusted and body_size and size and size <= .9 * body_size
                            and block.bbox and block.bbox[1] >= 650)
            if labelled or inferred:
                candidates.append((block, lead, labelled))
        counts = Counter(_label(lead) for _, lead, _ in candidates)
        body_ids = {id(b) for b, _, _ in candidates}
        accepted = []
        for note, lead, labelled in candidates:
            label = _label(lead)
            if counts[label] != 1:
                continue
            references = []
            for block in page.blocks:
                if id(block) in body_ids or block.kind not in {"paragraph", "heading", "title"}:
                    continue
                protected = protected_ranges(block.markdown)
                for match in REFERENCE.finditer(block.markdown):
                    if _label(match) != label:
                        continue
                    # A standalone math superscript is eligible; code, links,
                    # or a superscript inside a larger expression are not.
                    if any(a <= match.start() < b and (a, b) != match.span()
                           for a, b in protected):
                        continue
                    if trusted:
                        valid = bool(block.bbox and _raised_reference(page, block, match, project))
                    else:
                        # OCR-only fallback: explicit note label plus a standalone
                        # superscript, or an attached symbolic author reference.
                        valid = labelled and not match[4] and not (match[3] and label in block.markdown[:match.start()])
                    if valid:
                        references.append((block, match.span()))
            if references:
                accepted.append((note, lead, references))
        edits: dict[int, list] = {}
        occupied = {identifier for block in page.blocks
                    for identifier in re.findall(r"\[\^([^\]]+)\]", block.markdown)}
        occupied.update(b.metadata["footnote"]["id"] for b in page.blocks if b.metadata.get("footnote"))
        for index, (note, lead, references) in enumerate(accepted, 1):
            identifier = f"p{page.number}-note-{index}"
            while identifier in occupied:
                index += 1
                identifier = f"p{page.number}-note-{index}"
            occupied.add(identifier)
            note.metadata["footnote"] = {"id": identifier, "marker": _label(lead),
                                         "original_markdown": note.markdown}
            note.markdown = note.markdown[lead.end():].strip()
            note.kind = "footnote"
            for block, (start, end) in references:
                # Consume separating spaces: the reference belongs to the word.
                while start and block.markdown[start - 1] in " \t":
                    start -= 1
                edits.setdefault(id(block), []).append((start, end, f"[^{identifier}]"))
        for block in page.blocks:
            for start, end, replacement in sorted(edits.get(id(block), []), reverse=True):
                block.markdown = block.markdown[:start] + replacement + block.markdown[end:]


def footnote_definitions(pages: list[PageResult], *, references: str | None = None) -> str:
    wanted = set(re.findall(r"\[\^([^\]]+)\]", references)) if references is not None else None
    definitions = []
    for page in pages:
        for block in page.blocks:
            note = block.metadata.get("footnote")
            if not note:
                continue
            if wanted is not None and note["id"] not in wanted:
                continue
            lines = block.markdown.splitlines()
            definitions.append(f"[^{note['id']}]: {lines[0]}" + "".join(
                "\n    " + line if line else "\n" for line in lines[1:]))
    return "\n\n".join(definitions)


def place_footnotes(text: str, pages: list[PageResult], *, chapter: bool = False) -> str:
    """Place each definition after its first referencing paragraph in this file.

    Use parser source maps, not blank-line splitting: wrapped paragraphs and
    fenced code can both contain blank lines or footnote-looking text.
    """
    definitions = {}
    for page in pages:
        for block in page.blocks:
            note = block.metadata.get("footnote")
            if note:
                definitions[note["id"]] = footnote_definitions(
                    [page], references=f"[^{note['id']}]",
                )
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    seen = set()
    insertions = {}
    for token in MarkdownIt("commonmark").parse(text):
        if token.type != "inline" or token.map is None:
            continue
        references = [identifier for child in token.children or [] if child.type == "text"
                      for identifier in re.findall(r"\[\^([^\]]+)\]", child.content)]
        notes = []
        for identifier in references:
            if identifier in definitions and identifier not in seen:
                notes.append(definitions[identifier])
                seen.add(identifier)
        if not notes:
            continue
        content = "\n\n".join(notes)
        if chapter:
            content = content.replace("](assets/", "](../assets/")
        first_line = lines[token.map[0]]
        # Retain quote containers and replace list markers with their content
        # indentation, including combinations such as '> -' and '- >'.
        prefix = re.match(r"[ \t]*(?:(?:>[ \t]?|(?:[-+*]|\d+[.)])[ \t]+)[ \t]*)*", first_line)[0]
        indent = re.sub(r"(?:[-+*]|\d+[.)])[ \t]+", lambda m: " " * len(m.group()), prefix)
        if indent:
            content = "\n".join(indent + line if line else indent.rstrip() for line in content.splitlines())
        end = offsets[token.map[1]]
        blank = indent.rstrip() if ">" in indent else ""
        insertions[end] = (content, blank)
    for end, (content, blank) in sorted(insertions.items(), reverse=True):
        separator = "" if end and text[end - 1] == "\n" else "\n"
        text = text[:end] + separator + blank + "\n" + content + "\n" + blank + "\n" + text[end:].lstrip("\n")
    return text
