"""Conservative glyph attachments and inline math recovery from native evidence."""
from __future__ import annotations

import re
import unicodedata

from .alignment import Projection, _baseline, _em, align_glyphs, math_font_role
from .embedded import iter_embedded_characters
from .mathlint import math_spans
from .model import Block, EmbeddedEvidence
from .lists import render_list


ACCENTS = {"\u20d7": "vec", "\u0302": "hat", "\u0303": "tilde",
           "\u0304": "bar", "\u0307": "dot", "\u0308": "ddot"}
ACCENT_TEX = re.compile(r"\\(vec|hat|tilde|bar|dot|ddot|widehat|widetilde)\s*\{\s*([A-Za-z])\s*\}")
NON_PROSE = {"figure", "image", "table", "code", "formula", "equation", "display_formula"}


def accent_identity(glyph: dict) -> str | None:
    value = glyph["text"]
    if value in ACCENTS:
        return ACCENTS[value]
    # The txsys Type1 encoding names slot 174 /vec. Other fonts use the
    # same extracted character for delimiter pieces or a registered mark.
    font = re.sub(r"^[A-Z]{6}\+", "", glyph.get("font", "")).casefold()
    if font == "txsys" and value == "®":
        return "vec"
    return None


def accent_attachments(glyphs: list[dict]) -> dict[tuple, str]:
    """Attach only uniquely overlapping, same-scale marks to a single letter.

    Font boxes need not describe the ink: TeX accent glyphs can share the
    base's origin and have a box *below* it. Baselines plus horizontal overlap
    are used instead of assuming that every accent box is above its base.
    """
    attached: dict[tuple, list[str]] = {}
    for accent in glyphs:
        identity = accent_identity(accent)
        if not identity or not accent.get("origin"):
            continue
        if tuple(accent.get("direction", (1, 0))) != (1, 0):
            continue
        candidates = []
        for base in glyphs:
            if not base.get("origin") or not re.fullmatch(r"[A-Za-z]", unicodedata.normalize("NFKC", base["text"])):
                continue
            if tuple(base.get("direction", (1, 0))) != (1, 0):
                continue
            ex, ey = _em(base)
            if not .8 <= _em(accent)[1] / ey <= 1.2:
                continue
            a, b = accent["bbox"], base["bbox"]
            overlap = min(a[2], b[2]) - max(a[0], b[0])
            if overlap < .5 * max(.01, b[2] - b[0]):
                continue
            if abs((a[0] + a[2] - b[0] - b[2]) / 2) > .35 * ex:
                continue
            if not -.8 * ey <= _baseline(accent) - _baseline(base) <= .1 * ey:
                continue
            candidates.append(base)
        if len(candidates) == 1:
            attached.setdefault(candidates[0]["order"], []).append(identity)
    # Stacked / duplicated accents need an explicit cluster model, not guessing.
    return {key: marks[0] for key, marks in attached.items() if len(marks) == 1}


def repair_accents(blocks: list[Block], embedded: EmbeddedEvidence, project: Projection) -> list[str]:
    attachments = accent_attachments(list(iter_embedded_characters(embedded)))
    changed = False
    for block in blocks:
        if not block.bbox or not attachments or not ACCENT_TEX.search(block.markdown):
            continue
        spans, _ = math_spans(block.markdown)
        aligned = align_glyphs(block.markdown, embedded, block.bbox, project)
        by_start = {start: i for i, (start, _) in enumerate(aligned.spans)}
        edits = []
        for match in ACCENT_TEX.finditer(block.markdown):
            if not any(s.content_start <= match.start() and match.end() <= s.content_end for s in spans):
                continue
            native = aligned.matches.get(by_start.get(match.start(2)))
            if native is None or aligned.native[native] != match[2]:
                continue
            identity = attachments.get(aligned.glyphs[native]["order"])
            if identity and match[1].removeprefix("wide") != identity:
                edits.append((match.start(1), match.end(1), identity))
        changed |= _apply_edits(block, edits, "accent")
    return ["visual_embedded_accent_repair"] if changed else []


def _math_letter(glyph: dict) -> bool:
    value = glyph["text"]
    if len(value) != 1 or not re.fullmatch("[A-Za-z]", unicodedata.normalize("NFKC", value)):
        return False
    if unicodedata.name(value, "").startswith("MATHEMATICAL "):
        return True
    if math_font_role(glyph) in {"mathbb", "mathcal"}:
        return True
    font = re.sub(r"^[A-Z]{6}\+", "", glyph.get("font", "")).casefold()
    return bool(re.fullmatch(r"(?:newtxmi|cmmi|stixmathitalic)\d*", font))


def protected_ranges(text: str) -> list[tuple[int, int]]:
    spans, _ = math_spans(text)
    ranges = [(s.start, s.end) for s in spans]
    # Do not rewrite code, links (including their labels), tags, or references.
    ranges.extend(m.span() for m in re.finditer(
        r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|`+[^`]*`+|!?\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])?|<[^>]*>|\\[A-Za-z]+",
        text, re.S))
    return ranges


def restore_inline_math(blocks: list[Block], embedded: EmbeddedEvidence, project: Projection) -> list[str]:
    changed = False
    for block in blocks:
        if isinstance(block.metadata.get("list"), dict):
            list_changed = False
            def visit(node):
                nonlocal changed, list_changed
                for item in node.get("items", []):
                    for child in item.get("blocks", []):
                        leaf = Block(child.get("kind", "paragraph"), child.get("markdown", ""),
                                     bbox=child.get("bbox") or block.bbox)
                        if restore_inline_math([leaf], embedded, project):
                            child["markdown"] = leaf.markdown
                            block.metadata.setdefault("embedded_text_repairs", []).extend(
                                leaf.metadata.get("embedded_text_repairs", []))
                            changed = True
                            list_changed = True
                    for child in item.get("children", []):
                        visit(child)
            visit(block.metadata["list"])
            if list_changed:
                block.markdown = render_list(block.metadata["list"])
            continue
        if not block.bbox or block.kind in NON_PROSE or "list" in block.metadata:
            continue
        aligned = align_glyphs(block.markdown, embedded, block.bbox, project)
        by_start = {start: i for i, (start, _) in enumerate(aligned.spans)}
        protected = protected_ranges(block.markdown)
        edits = []
        # Whole tokens only: never turn one letter inside an ordinary word into math.
        for match in re.finditer(r"(?<![\w\\])(?:[A-Za-z]+|\d+)(?:\s*(?::=|[=+−/<>-])\s*(?:[A-Za-z]+|\d+))*(?!\w)", block.markdown):
            if not any(c.isalpha() for c in match[0]):
                continue
            if any(a < match.end() and match.start() < b for a, b in protected):
                continue
            glyphs = []
            for offset in range(match.start(), match.end()):
                if block.markdown[offset].isspace():
                    continue
                native = aligned.matches.get(by_start.get(offset))
                expected, _ = project(block.markdown[offset])
                if native is None or aligned.native[native] != expected:
                    break
                glyph = aligned.glyphs[native]
                if (block.markdown[offset].isalpha() and not _math_letter(glyph)) or native in aligned.parents:
                    break
                glyphs.append(glyph)
            if len(glyphs) != sum(not c.isspace() for c in match[0]):
                continue
            if any(abs(_baseline(g) - _baseline(glyphs[0])) > .1 * _em(g)[1] for g in glyphs):
                continue
            edits.append((match.start(), match.end(), rf"\({match[0]}\)"))
        changed |= _apply_edits(block, edits, "inline_math")
    return ["visual_embedded_inline_math_repair"] if changed else []


def _apply_edits(block: Block, edits: list[tuple[int, int, str]], kind: str) -> bool:
    for start, end, replacement in sorted(edits, reverse=True):
        block.metadata.setdefault("embedded_text_repairs", []).append({
            "kind": kind, "visual": block.markdown[start:end], "embedded": replacement,
        })
        block.markdown = block.markdown[:start] + replacement + block.markdown[end:]
    return bool(edits)
