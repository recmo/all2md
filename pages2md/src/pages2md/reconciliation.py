"""Evidence-backed transcription repairs; no conversion or filesystem ownership."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from .model import Block, EmbeddedEvidence, FIGURE_KINDS, FORMULA_KINDS
from .embedded import (
    EmbeddedTrust, iter_embedded_characters, bbox_coverage, embedded_text_for_bbox,
)
from .alignment import (
    align_glyphs, delimiter_edits, script_edits, script_substitutions,
    math_font_role, semantic_math_projection, MATH_CHARACTER_COMMANDS,
)
from .syntax import math_spans, non_math_ranges, protected_ranges
from .edits import apply_edits
from .lists import editable_leaves
from .semantics import repair_accents, restore_inline_math


def reconcile_text(blocks: list[Block], embedded: EmbeddedEvidence, trust: EmbeddedTrust) -> list[str]:
    """One explicit repair sequence for all editable text leaves."""
    usable = embedded if trust.usable else EmbeddedEvidence()
    geometric = embedded if trust.geometric else EmbeddedEvidence()
    warnings = []
    with editable_leaves(blocks) as leaves:
        warnings.extend(_repair_embedded_digit_runs(leaves, usable))
        warnings.extend(_repair_embedded_math_structure(leaves, usable))
        warnings.extend(_repair_malformed_math_syntax(leaves))
        warnings.extend(_repair_embedded_short_insertions(leaves, geometric))
        warnings.extend(_restore_embedded_math_alphabets(leaves, geometric))
        warnings.extend(_repair_embedded_word_tokens(leaves, usable))
        warnings.extend(repair_accents(leaves, geometric, semantic_math_projection))
        warnings.extend(restore_inline_math(leaves, geometric, semantic_math_projection))
    return warnings


def _repair_embedded_digit_runs(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Join OCR-split numeric literals only when matched PDF text confirms them."""
    repaired = False
    decimal = re.compile(r"(?<!\w)(\d+)\.\s+((?:\d\s+)+\d)(?!\w)")
    integer = re.compile(r"(?<![\w.])(\d(?:\s+\d){1,})(?![\w.])")
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS:
            continue
        evidence = embedded_text_for_bbox(embedded, block.bbox, either_box=True)
        if not evidence:
            continue
        if block.kind in {"code", "code_block"}:
            continue
        # Whitespace in native text is evidence of token boundaries, not OCR
        # damage. Never manufacture a corroborating number by removing it.
        native_numbers = set(re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?!\w|\.\d)", evidence))
        # Linear PDF text can concatenate a base and its exponent ('218').
        # Recover separate digit runs only across touching glyphs sharing a
        # baseline and scale; spaces and script transitions end a run.
        run = ""
        previous = None
        for glyph in iter_embedded_characters(embedded):
            if bbox_coverage(glyph["bbox"], block.bbox) < 0.45:
                continue
            value, origin, em = glyph["text"], glyph.get("origin"), glyph.get("em")
            usable = value.isascii() and value.isdigit() and origin and em
            adjacent = bool(usable and previous
                and abs(origin[1] - previous["origin"][1]) <= .08 * em[1]
                and abs(em[1] - previous["em"][1]) <= .08 * em[1]
                and -.05 * em[0] <= glyph["bbox"][0] - previous["bbox"][2] <= .12 * em[0])
            if not adjacent:
                if run:
                    native_numbers.add(run)
                run = ""
            if usable:
                run += value
                previous = glyph
            else:
                previous = None
        if run:
            native_numbers.add(run)
        protected = non_math_ranges(block.markdown)

        def is_protected(match):
            # Exact native agreement on separated digits must beat a matching
            # compact number elsewhere in the same OCR block.
            separated = r"\s+".join(re.escape(part) for part in match.group().split())
            return (any(a < match.end() and match.start() < b for a, b in protected)
                    or re.search(r"(?<!\w)" + separated + r"(?!\w)", evidence) is not None)

        native_decimals = sorted(set(
            number for number in native_numbers if re.fullmatch(r"\d+\.\d{3,}", number)
        ))

        def join_decimal(match: re.Match[str]) -> str:
            if is_protected(match):
                return match.group(0)
            candidate = match.group(1) + "." + re.sub(r"\s+", "", match.group(2))
            replacement = candidate
            if candidate not in native_numbers:
                compatible = [
                    native
                    for native in native_decimals
                    if native.split(".", 1)[0] == match.group(1)
                    and abs(len(native) - len(candidate)) <= 1
                    and SequenceMatcher(None, candidate, native, autojunk=False).ratio() >= 0.72
                ]
                if len(compatible) != 1:
                    return match.group(0)
                replacement = compatible[0]
            return replacement

        def join_integer(match: re.Match[str]) -> str:
            if is_protected(match):
                return match.group(0)
            candidate = re.sub(r"\s+", "", match.group(1))
            if len(candidate) < 3:
                left = match.string[max(0, match.start() - 3) : match.start()]
                right = match.string[match.end() : match.end() + 3]
                if "}" not in right or not any(marker in left for marker in ("{", "/", "^", "_")):
                    return match.group(0)
            if candidate not in native_numbers:
                return match.group(0)
            return candidate

        # Both passes use original offsets so earlier edits cannot shift a
        # protected code/link span underneath a later match.
        edits = []
        for pattern, repair in ((decimal, join_decimal), (integer, join_integer)):
            for match in pattern.finditer(block.markdown):
                if any(a < match.end() and match.start() < b for a, b, _ in edits):
                    continue
                replacement = repair(match)
                if replacement != match.group():
                    edits.append((*match.span(), replacement))
        repaired |= apply_edits(block, edits, "numeric")
    return ["visual_embedded_numeric_repair"] if repaired else []


def _repair_embedded_word_tokens(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Repair close lexical OCR substitutions while preserving Markdown and math."""

    repaired = False
    for block in blocks:
        if not block.bbox or block.kind not in {
            "paragraph", "text", "caption", "heading", "title", "reference", "ref_text",
        }:
            continue
        evidence = embedded_text_for_bbox(embedded, block.bbox, either_box=True)
        if not evidence:
            continue
        visual_tokens = _unprotected_word_tokens(block.markdown)
        embedded_tokens = [
            (match.group(0), match.start(), match.end())
            for match in re.finditer(r"[^\W_]+", evidence, re.UNICODE)
        ]
        if not visual_tokens or not embedded_tokens:
            continue
        matcher = SequenceMatcher(
            None,
            [token[0].casefold() for token in visual_tokens],
            [token[0].casefold() for token in embedded_tokens],
            autojunk=False,
        )
        replacements: list[tuple[int, int, str, str]] = []
        for operation, visual_start, visual_end, embedded_start, embedded_end in matcher.get_opcodes():
            if operation != "replace" or visual_end - visual_start != embedded_end - embedded_start:
                continue
            for visual, native in zip(
                visual_tokens[visual_start:visual_end],
                embedded_tokens[embedded_start:embedded_end],
            ):
                if not _safe_embedded_token_repair(visual[0], native[0]):
                    continue
                replacements.append((visual[1], visual[2], visual[0], native[0]))
        if not replacements:
            continue
        repaired |= apply_edits(block, [(start, end, target) for start, end, _, target in replacements], "lexical")
    return ["visual_embedded_lexical_repair"] if repaired else []


def _repair_embedded_short_insertions(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Restore short omissions bracketed by trusted PDF text and OCR uncertainty."""
    repaired = False
    document_markdown = "\n".join(block.markdown for block in blocks)
    for block in blocks:
        if not block.bbox or block.kind not in {
            "paragraph", "text", "caption", "heading", "title", "reference", "ref_text",
        }:
            continue
        uncertain = [
            span
            for span in block.metadata.get("uncertain_spans", [])
            if float(span.get("confidence", 1.0)) <= 0.75
        ]
        if not uncertain:
            continue
        evidence = embedded_text_for_bbox(embedded, block.bbox, either_box=True)
        if not evidence:
            continue
        visual, visual_map = _alignment_projection(block.markdown)
        native, native_map = _alignment_projection(evidence)
        if not visual or not native:
            continue
        opcodes = SequenceMatcher(None, visual, native, autojunk=False).get_opcodes()
        protected = protected_ranges(block.markdown)
        insertions: list[tuple[int, str, str]] = []
        for opcode_index, (operation, left_start, left_end, right_start, right_end) in enumerate(opcodes):
            if operation != "insert" or left_start != left_end or right_start == right_end:
                continue
            if opcode_index == 0 or opcode_index + 1 >= len(opcodes):
                continue
            before = opcodes[opcode_index - 1]
            after = opcodes[opcode_index + 1]
            if before[0] != "equal" or after[0] != "equal":
                continue
            if before[2] - before[1] < 8 or after[2] - after[1] < 8:
                continue
            markdown_index = (
                visual_map[left_start]
                if left_start < len(visual_map)
                else len(block.markdown)
            )
            if not _near_uncertain_span(markdown_index, uncertain):
                continue
            if any(start <= markdown_index < end for start, end in protected):
                continue
            native_start = native_map[right_start]
            native_end = native_map[right_end - 1] + 1
            inserted = re.sub(r"\s+", " ", evidence[native_start:native_end])
            left_neighbor = visual[left_start - 1] if left_start else ""
            right_neighbor = visual[left_start] if left_start < len(visual) else ""
            if not _safe_embedded_insertion(
                inserted,
                document_markdown,
                left_neighbor=left_neighbor,
                right_neighbor=right_neighbor,
            ):
                continue
            inserted = _format_embedded_inserted_math(inserted, embedded, block.bbox)
            insertions.append((markdown_index, inserted, evidence[native_start:native_end]))
        if not insertions:
            continue
        apply_edits(block, [(index, index, inserted) for index, inserted, _ in insertions], "insertion")
        block.metadata["embedded_supported_insertion"] = True
        if all(
            any(_near_uncertain_span(index, [span]) for index, _, _ in insertions)
            for span in uncertain
        ):
            block.metadata.pop("uncertain_spans", None)
        repaired = True
    return ["visual_embedded_insertion_repair"] if repaired else []


def _alignment_projection(value: str) -> tuple[str, list[int]]:
    """Return comparable visible text plus source offsets for safe splice points."""
    projected: list[tuple[str, int]] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\":
            if index + 1 < len(value) and value[index + 1] in "()[]{}":
                index += 2
                projected.append((" ", index - 1))
                continue
            command = re.match(r"\\[A-Za-z]+", value[index:])
            if command:
                index += len(command.group(0))
                projected.append((" ", index - 1))
                continue
            index += 1
            continue
        normalized = unicodedata.normalize("NFKC", character)
        for visible in normalized:
            if visible in "{}_^$*`":
                projected.append((" ", index))
            elif visible.isprintable():
                projected.append((" " if visible.isspace() else visible.casefold(), index))
        index += 1
    collapsed: list[str] = []
    offsets: list[int] = []
    for character, source_index in projected:
        if character == " ":
            if not collapsed or collapsed[-1] == " ":
                continue
        collapsed.append(character)
        offsets.append(source_index)
    while collapsed and collapsed[-1] == " ":
        collapsed.pop()
        offsets.pop()
    return "".join(collapsed), offsets


def _near_uncertain_span(index: int, spans: list[dict[str, Any]]) -> bool:
    for span in spans:
        start = int(span.get("start", 0))
        end = int(span.get("end", start))
        distance = 0 if start <= index <= end else min(abs(index - start), abs(index - end))
        if distance <= 16:
            return True
    return False


def _safe_embedded_insertion(
    inserted: str,
    document_markdown: str,
    *,
    left_neighbor: str,
    right_neighbor: str,
) -> bool:
    plain = inserted.strip()
    words = re.findall(r"[^\W_]+", plain, re.UNICODE)
    spelling_character = bool(
        len(plain) == 1
        and plain.isalpha()
        and left_neighbor.isalpha()
        and right_neighbor.isalpha()
    )
    if not spelling_character and (len(words) < 2 or len(words) > 4 or len(plain) > 32):
        return False
    if any(not (character.isalnum() or character.isspace() or character in "-'’") for character in plain):
        return False
    identifiers = [word for word in words if len(word) == 1 and word.isascii() and word.isupper()]
    return all(
        re.search(
            rf"(?:\\(?:mathcal|mathbf|mathbb)\s*\{{\s*{letter}\s*\}}|(?<!\w){letter}(?!\w))",
            document_markdown,
        )
        for letter in identifiers
    )


def _format_embedded_inserted_math(
    inserted: str,
    embedded: EmbeddedEvidence,
    bbox: tuple[float, float, float, float],
) -> str:
    block = Block("paragraph", inserted, bbox=bbox)
    _restore_embedded_math_alphabets([block], embedded)
    return block.markdown


def _repair_embedded_delimiters(blocks, embedded):
    repaired = False
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS:
            continue
        native, _ = semantic_math_projection(embedded_text_for_bbox(embedded, block.bbox, either_box=True))
        alignment = align_glyphs(block.markdown, embedded, block.bbox, semantic_math_projection).with_text_fallback(native)
        ranges = _math_source_ranges(block.markdown)
        if not ranges and block.kind in FORMULA_KINDS:
            ranges = [(0, len(block.markdown))]
        repaired |= apply_edits(block, delimiter_edits(block.markdown, alignment, ranges), "delimiter")
    return ["visual_embedded_delimiter_repair"] if repaired else []


def _math_source_ranges(markdown: str) -> list[tuple[int, int]]:
    return [(s.start, s.end) for s in math_spans(markdown)[0]]


def _math_replacement(character: str) -> str | None:
    if character in MATH_CHARACTER_COMMANDS:
        return rf"\{MATH_CHARACTER_COMMANDS[character]}"
    if character == "{":
        return r"\{"
    if character == "}":
        return r"\}"
    if len(character) == 1 and (character.isalnum() or character in "-+=<>[]()|,.;:"):
        return character
    return None


def _math_character_family(character: str) -> str:
    if character.isalpha():
        return "letter"
    if character.isdigit():
        return "digit"
    category = unicodedata.category(character)
    if category in {"Ps", "Pe"}:
        return category
    if category in {"Sm", "Pd"} or character in "+-=<>≤≥|":
        return "operator"
    return category


def _repair_embedded_math_glyphs(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Repair isolated math glyph substitutions with strong local PDF anchors."""
    repaired = bool(_repair_embedded_delimiters(blocks, embedded))
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS:
            continue
        ranges = _math_source_ranges(block.markdown)
        if not ranges:
            continue
        evidence = embedded_text_for_bbox(embedded, block.bbox, either_box=True)
        if not evidence:
            continue
        alignment = align_glyphs(block.markdown, embedded, block.bbox, semantic_math_projection)
        visual, visual_spans = alignment.text, alignment.spans
        native = alignment.native
        if not native:
            native, _ = semantic_math_projection(evidence)
        if not visual or not native:
            continue
        replacements: list[tuple[int, int, str, str]] = []
        for left_start, right_start, native_character, left_anchor, right_anchor in alignment.substitutions(native):
            source_start, source_end = visual_spans[left_start]
            if not any(start <= source_start and source_end <= end for start, end in ranges):
                continue
            uncertain = [
                span for span in block.metadata.get("uncertain_spans", [])
                if float(span.get("confidence", 1.0)) <= 0.75
            ]
            near_uncertain = _near_uncertain_span(source_start, uncertain)
            required_anchor = 4 if near_uncertain or block.kind in FORMULA_KINDS else 6
            if min(left_anchor, right_anchor) < 1 or left_anchor + right_anchor < required_anchor:
                continue
            visual_character = visual[left_start]
            if _math_character_family(visual_character) != _math_character_family(native_character):
                continue
            if _math_character_family(visual_character) in {"Ps", "Pe"}:
                continue  # Delimiter endpoints are repaired as pairs.
            replacement = _math_replacement(native_character)
            if replacement is None or replacement == block.markdown[source_start:source_end]:
                continue
            if replacement.startswith("\\") and re.match(r"[A-Za-z]", block.markdown[source_end:]):
                replacement += " "
            if visual_character.isdigit() and native_character.isdigit():
                continue
            if (
                visual_character.isascii() and visual_character.isalpha()
                and native_character.isascii() and native_character.isalpha()
                and ((source_start and block.markdown[source_start - 1].isalpha())
                     or (source_end < len(block.markdown) and block.markdown[source_end].isalpha()))
                and not block.markdown[source_start:source_end].startswith("\\")
            ):
                continue
            replacements.append((source_start, source_end, block.markdown[source_start:source_end], replacement))
        if not replacements:
            continue
        repaired |= apply_edits(block, [(start, end, target) for start, end, _, target in replacements], "math_glyph")
    return ["visual_embedded_math_glyph_repair"] if repaired else []


def _repair_embedded_math_structure(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Use PDF geometry for scripts and paired delimiters lost by linear OCR."""
    repaired = bool(_repair_embedded_math_glyphs(blocks, embedded))
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS or not _math_source_ranges(block.markdown):
            continue
        evidence = embedded_text_for_bbox(embedded, block.bbox, either_box=True)
        if not evidence:
            continue
        replacements: list[tuple[int, int, str, str]] = []
        alignment = align_glyphs(block.markdown, embedded, block.bbox, semantic_math_projection)
        if apply_edits(block, script_substitutions(block.markdown, alignment), "script_glyph"):
            repaired = True
            alignment = align_glyphs(block.markdown, embedded, block.bbox, semantic_math_projection)
        structured = script_edits(block.markdown, alignment)
        if structured:
            repaired |= apply_edits(block, structured, "script")

        if structured:
            alignment = align_glyphs(block.markdown, embedded, block.bbox, semantic_math_projection)
        for start, end, replacement in delimiter_edits(
            block.markdown, alignment, _math_source_ranges(block.markdown)
        ):
            replacements.append((start, end, block.markdown[start:end], replacement))

        # A visually raised trailing symbol belongs inside the preceding exponent.
        exponent_tail = re.compile(
            r"(?P<base>[A-Za-z])\s*\^\s*\{(?P<exponent>[^{}\n]{1,24})\}\s+(?P<tail>[A-Za-z])(?!\w)"
        )
        for match in exponent_tail.finditer(block.markdown):
            by_start = {start: i for i, (start, _) in enumerate(alignment.spans)}
            base = match.group("base")
            native_base = alignment.matches.get(by_start.get(match.start("base")))
            indexes = [i for i, (start, end) in enumerate(alignment.spans)
                       if match.start("exponent") <= start < end <= match.end("tail")]
            supported = native_base is not None and bool(indexes)
            for i in indexes:
                glyph = alignment.matches.get(i)
                if glyph is None or alignment.text[i] != alignment.native[glyph]:
                    supported = False
                    break
                edge = alignment.parents.get(glyph)
                if edge != (native_base, "^"):
                    supported = False
                    break
            if supported:
                replacement = f"{base} ^ {{{match.group('exponent').rstrip()} {match.group('tail')}}}"
                replacements.append((match.start(), match.end(), match.group(0), replacement))

        if not replacements:
            continue
        repaired |= apply_edits(block, [(start, end, target) for start, end, _, target in replacements], "math_structure")
    return ["visual_embedded_math_structure_repair"] if repaired else []


def _repair_malformed_math_syntax(blocks: list[Block]) -> list[str]:
    """Remove universally empty TeX scripts when a real script follows."""
    repaired = False
    pattern = re.compile(r"\^\s*\{\s*\}\s*(?=\^)")
    for block in blocks:
        if block.kind in FIGURE_KINDS or not _math_source_ranges(block.markdown):
            continue
        markdown, count = pattern.subn("", block.markdown)
        if not count:
            continue
        block.markdown = markdown
        block.metadata.setdefault("syntax_repairs", []).append("removed_empty_duplicate_exponent")
        repaired = True
    return ["visual_math_syntax_repair"] if repaired else []


def _restore_embedded_math_alphabets(
    blocks: list[Block],
    embedded: EmbeddedEvidence,
) -> list[str]:
    """Restore styled capitals by aligning each OCR occurrence to one PDF glyph."""
    repaired = False
    for block in blocks:
        if not block.bbox or block.kind in FIGURE_KINDS:
            continue
        visual = _visible_uppercase_occurrences(block.markdown)
        alignment = align_glyphs(block.markdown, embedded, block.bbox, semantic_math_projection)
        by_start = {start: index for index, (start, _) in enumerate(alignment.spans)}
        aligned: list[tuple[dict[str, object], dict[str, object], bool]] = []
        for occurrence in visual:
            index = by_start.get(occurrence["start"])
            native_index = alignment.matches.get(index)
            if native_index is None:
                continue
            glyph = alignment.glyphs[native_index]
            aligned.append((occurrence, {**glyph, "match_letter": glyph["letter"]},
                            occurrence["letter"] == glyph["letter"]))

        replacements: list[tuple[int, int, str, str]] = []
        for occurrence, glyph, same_letter in aligned:
            role = math_font_role(glyph)
            style = occurrence.get("style")
            target_letter = str(glyph["match_letter"] if not same_letter else occurrence["letter"])
            if not same_letter and role not in {"mathcal", "mathbb"}:
                continue
            if role in {"mathcal", "mathbb"} and (style != role or not same_letter):
                replacement = rf"\{role} {{{target_letter}}}"
            elif role == "ordinary" and style in {"mathcal", "mathbb"}:
                replacement = target_letter
            else:
                continue
            start = int(occurrence.get("style_start", occurrence["start"]))
            end = int(occurrence.get("style_end", occurrence["end"]))
            prefix = block.markdown[:start].rstrip()
            if not occurrence["math"]:
                # Only a standalone, exactly aligned symbol may gain math delimiters.
                if (not same_letter or role not in {"mathcal", "mathbb"}
                    or (start and block.markdown[start - 1].isalnum())
                    or (end < len(block.markdown) and block.markdown[end].isalnum())):
                    continue
                replacement = rf"\({replacement}\)"
            if role in {"mathcal", "mathbb"} and prefix.endswith(("^", "_")):
                replacement = "{" + replacement + "}"
            replacements.append((
                start,
                end,
                block.markdown[start:end],
                replacement,
            ))
        if not replacements:
            continue
        repaired |= apply_edits(block, [(start, end, target) for start, end, _, target in replacements], "math_alphabet")
    return ["visual_embedded_math_alphabet_repair"] if repaired else []


def _visible_uppercase_occurrences(markdown: str) -> list[dict[str, object]]:
    command_ranges = non_math_ranges(markdown)
    math_ranges = _math_source_ranges(markdown)
    output: list[dict[str, object]] = []
    for match in re.finditer(r"[A-Z]", markdown):
        if any(start <= match.start() < end for start, end in command_ranges):
            continue
        prefix = markdown[: match.start()]
        styled = re.search(
            r"\\(mathcal|mathbb|mathbf|mathrm|mathsf)\s*\{\s*$",
            prefix,
        )
        style_end = match.end()
        if styled:
            closing = re.match(r"\s*\}", markdown[match.end():])
            if closing:
                style_end = match.end() + closing.end()
        output.append({
            "letter": match.group(0),
            "start": match.start(),
            "end": match.end(),
            "style": styled.group(1) if styled else None,
            "style_start": styled.start() if styled else match.start(),
            "style_end": style_end,
            "math": any(start <= match.start() < end for start, end in math_ranges),
        })
    return output


def _unprotected_word_tokens(markdown: str) -> list[tuple[str, int, int]]:
    protected = protected_ranges(markdown, references=False)
    output: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^\W_]+", markdown, re.UNICODE):
        if match.start() and markdown[match.start() - 1] == "\\":
            continue
        if any(start <= match.start() < end for start, end in protected):
            continue
        output.append((match.group(0), match.start(), match.end()))
    return output


def _safe_embedded_token_repair(visual: str, embedded: str) -> bool:

    if visual.casefold() == embedded.casefold():
        return False
    if any(unicodedata.name(character, "").startswith("MATHEMATICAL") for character in embedded):
        return False
    if min(len(visual), len(embedded)) < 4 or abs(len(visual) - len(embedded)) > 2:
        return False
    identifier = (
        any(character.isdigit() for character in visual)
        and any(character.isdigit() for character in embedded)
    )
    proper_name = visual[:1].isupper() and embedded[:1].isupper()
    if not (identifier or proper_name):
        return False
    if visual.isdigit() != embedded.isdigit():
        # A letter/digit confusion inside an otherwise stable identifier is safe.
        if not (visual.isalnum() and embedded.isalnum()):
            return False
    similarity = SequenceMatcher(None, visual.casefold(), embedded.casefold(), autojunk=False).ratio()
    return similarity >= 0.72
