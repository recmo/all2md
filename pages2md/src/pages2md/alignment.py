"""Occurrence-level OCR/PDF alignment. No document vocabulary or substitutions.

PDF baselines establish reading rows; smaller glyphs retain their script parent.
Alignment uses all visible text, not a separate stream of capital letters.
Bidirectional agreement and unique local contexts establish glyph occurrences.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from statistics import median

from .embedded import embedded_characters_for_bbox, iter_embedded_characters
from .model import EmbeddedEvidence


@dataclass
class GlyphAlignment:
    text: str
    spans: list[tuple[int, int]]
    glyphs: list[dict]
    matches: dict[int, int]
    native: str
    parents: dict[int, tuple[int, str]]
    layout_matches: dict[int, int]

    def with_text_fallback(self, native: str):
        if self.native:
            return self
        matches = _agreed_matches(self.text, native)
        return replace(self, native=native, matches=matches, layout_matches=matches)

    def substitutions(self, fallback: str = ""):
        """Yield locally anchored character changes from agreed occurrences.

        Text-only evidence uses the same bidirectional agreement policy, but
        cannot establish geometry or font semantics.
        """
        native = self.native or fallback
        matches = self.matches if self.native else _agreed_matches(self.text, native)
        for left, right in matches.items():
            if self.text[left] == native[right]:
                continue
            anchors = []
            for direction in (-1, 1):
                length, cursor = 0, left + direction
                while cursor in matches and self.text[cursor] == native[matches[cursor]]:
                    length += 1
                    cursor += direction
                anchors.append(length)
            yield left, right, native[right], *anchors


def glyph_em(glyph: dict) -> tuple[float, float]:
    if glyph.get("em") and min(glyph["em"]) > 0:
        return tuple(glyph["em"])
    box = glyph["bbox"]
    height = max(1.0, box[3] - box[1])
    return height, height


def glyph_baseline(glyph: dict) -> float:
    if "layout_baseline" in glyph:
        return glyph["layout_baseline"]
    return glyph["origin"][1] if glyph.get("origin") else glyph["bbox"][3]


def ordered_glyphs(glyphs: list[dict]) -> tuple[list[dict], dict[int, tuple[int, str]]]:
    """Linearize rows as base, subscript, superscript, retaining the edges.

    Thresholds are relative to the font em, never page coordinates. Rotated text
    and legacy evidence without baselines are not used for structural edits.
    """
    if not glyphs:
        return [], {}
    glyphs = [dict(g) for g in glyphs]
    largest = max(float(g.get("size") or glyph_em(g)[1]) for g in glyphs)
    main = [g for g in glyphs if float(g.get("size") or glyph_em(g)[1]) >= largest * .9]
    main_ids = {id(g) for g in main}
    rows: list[list[dict]] = []
    def center(glyph):
        return (glyph["bbox"][1] + glyph["bbox"][3]) / 2
    for glyph in sorted(main, key=center):
        if not rows or abs(center(glyph) - median(center(g) for g in rows[-1])) > glyph_em(glyph)[1] * .35:
            rows.append([])
        rows[-1].append(glyph)
    for row in rows:
        baseline = median(glyph_baseline(g) for g in row)
        for glyph in row:
            glyph["layout_baseline"] = baseline
    for glyph in glyphs:
        if id(glyph) in main_ids:
            continue
        row = min(rows, key=lambda r: abs(glyph_baseline(glyph) - glyph_baseline(r[0])))
        row.append(glyph)
    output: list[dict] = []
    relations: dict[int, tuple[int, str]] = {}
    for row in rows:
        row.sort(key=lambda g: (g["bbox"][0], glyph_baseline(g)))
        parents: dict[int, tuple[int, str]] = {}
        for child, glyph in enumerate(row):
            if not glyph.get("origin") or tuple(glyph.get("direction", (1, 0))) != (1, 0):
                continue
            size = float(glyph.get("size") or 0)
            candidates = []
            for parent, base in enumerate(row):
                if float(base.get("size") or 0) <= size * 1.1 or not base.get("origin"):
                    continue
                ex, ey = glyph_em(base)
                gap = glyph["bbox"][0] - base["bbox"][2]
                dy = glyph_baseline(glyph) - glyph_baseline(base)
                # Small labels centered over a relation precede that relation
                # in textual reading order (e.g. an annotated equality).
                if (base["text"] in {"=", "≈", "≤", "≥", "→"}
                    and -1.4 * ey < dy < -.12 * ey
                    and abs((glyph["bbox"][0] + glyph["bbox"][2]
                             - base["bbox"][0] - base["bbox"][2]) / 2) < .9 * ex):
                    candidates.append((0, float(base["size"]), abs(dy) / ey, parent, "over"))
                    continue
                if (base["bbox"][0] - .2 * ex <= glyph["bbox"][0]
                    and gap <= 2 * ex and .08 * ey < abs(dy) < 1.4 * ey):
                    candidates.append((max(0, gap), float(base["size"]), abs(dy) / ey,
                                       parent, "_" if dy > 0 else "^"))
            if candidates:
                candidates.sort()
                if len(candidates) == 1 or candidates[0][:3] != candidates[1][:3]:
                    _, _, _, parent, kind = candidates[0]
                    parents[child] = (parent, kind)

        def emit(index: int, parent: int | None = None, kind: str = "") -> None:
            for child, edge in parents.items():
                if edge == (index, "over"):
                    emit(child)
            target = len(output)
            output.append(row[index])
            if parent is not None:
                relations[target] = (parent, kind)
            for script in ("_", "^"):
                for child, edge in parents.items():
                    if edge == (index, script):
                        emit(child, target, script)

        for index in range(len(row)):
            if index not in parents:
                emit(index)
    return output, relations


def _agreed_matches(visual: str, native: str) -> dict[int, int]:
    def match(left: str, right: str) -> dict[int, int]:
        found = {}
        ops = SequenceMatcher(None, left, right, autojunk=False).get_opcodes()
        for pos, (kind, a, b, c, d) in enumerate(ops):
            if kind == "equal":
                found.update(zip(range(a, b), range(c, d)))
            elif kind == "replace" and b - a == d - c == 1:
                before = ops[pos - 1] if pos else None
                after = ops[pos + 1] if pos + 1 < len(ops) else None
                anchors = sum(op[2] - op[1] for op in (before, after) if op and op[0] == "equal")
                if anchors >= 4:
                    found[a] = c
        return found
    forward = match(visual, native)
    backward = {len(visual) - a - 1: len(native) - b - 1
                for a, b in match(visual[::-1], native[::-1]).items()}
    agreed = {a: b for a, b in forward.items() if backward.get(a) == b}
    # Multi-line formula layouts need not be globally monotone. Unique exact
    # local contexts can still identify an occurrence (e.g. an equation's LHS).
    local: dict[int, set[int]] = {}
    for size in (4, 8, 12):
        left_windows: dict[str, list[int]] = {}
        right_windows: dict[str, list[int]] = {}
        for value, windows in ((visual, left_windows), (native, right_windows)):
            for start in range(len(value) - size + 1):
                windows.setdefault(value[start:start + size], []).append(start)
        for value, starts in left_windows.items():
            targets = right_windows.get(value, [])
            if len(starts) == len(targets) == 1:
                for offset in range(size):
                    local.setdefault(starts[0] + offset, set()).add(targets[0] + offset)
    for a, targets in local.items():
        if len(targets) == 1 and (a not in agreed or agreed[a] in targets):
            agreed[a] = next(iter(targets))
        else:
            agreed.pop(a, None)
    return agreed


def align_glyphs(markdown: str, embedded: EmbeddedEvidence, bbox) -> GlyphAlignment:
    text, spans = semantic_math_projection(markdown)
    glyphs = embedded_characters_for_bbox(embedded, bbox)
    # OCR boxes can clip a line's final symbols. Complete intersecting PDF
    # lines instead of inventing a fixed pixel padding or borrowing a neighbor.
    lines = {g["order"][:2] for g in glyphs}
    glyphs = [g for g in iter_embedded_characters(embedded) if g["order"][:2] in lines]
    # Font extension pieces / accent encodings have no independent text identity.
    glyphs = [g for g in glyphs if g["text"].strip()
              and g["text"] not in "®©ª«¬\xad︁︂︃︄"
              and tuple(g.get("direction", (1, 0))) == (1, 0)]
    ordered, edges = ordered_glyphs(glyphs)
    expanded: list[dict] = []
    native = []
    old_to_new = {}
    for index, glyph in enumerate(ordered):
        value, _ = semantic_math_projection(str(glyph["text"]))
        if value:
            old_to_new[index] = len(expanded)
        for letter in value:
            expanded.append({**glyph, "letter": letter})
            native.append(letter)
    parents = {old_to_new[c]: (old_to_new[p], kind) for c, (p, kind) in edges.items()
               if c in old_to_new and p in old_to_new}
    source = "".join(native)
    # PDF drawing order and geometric reading order differ for fractions and
    # cases. Preserve both views of the SAME glyph identities. Conflicting
    # occurrence assignments abstain; neither view may overwrite the other.
    matches, geometric = _occurrence_matches(text, source, expanded)
    return GlyphAlignment(text, spans, expanded, matches, source, parents, geometric)


def _occurrence_matches(text: str, source: str, glyphs: list[dict]):
    geometric = _agreed_matches(text, source)
    drawing_order = sorted(range(len(glyphs)), key=lambda i: glyphs[i]["order"])
    drawing = "".join(source[i] for i in drawing_order)
    original = {a: drawing_order[b] for a, b in _agreed_matches(text, drawing).items()}
    matches = {a: b for a, b in {**geometric, **original}.items()
               if a not in geometric or a not in original or geometric[a] == original[a]}
    # A source glyph cannot corroborate two OCR occurrences, even if separate
    # serialization views happened to align each of them independently.
    counts: dict[int, int] = {}
    for b in matches.values():
        counts[b] = counts.get(b, 0) + 1
    matches = {a: b for a, b in matches.items() if counts[b] == 1}
    return matches, geometric


def delimiter_edits(
    markdown: str, alignment: GlyphAlignment, ranges: list[tuple[int, int]],
) -> list[tuple[int, int, str]]:
    """Repair paired ceiling/floor endpoints using their exact glyph identities.

    Both TeX endpoints must match the same native pair. Nested or neighboring
    ceilings cannot supply evidence for unrelated probability brackets. Only
    endpoint tokens are changed, so a second pass is a no-op.
    """
    closing = {"⌉": "⌈", "⌋": "⌊", "]": "["}
    commands = {"⌈": r"\lceil", "⌉": r"\rceil",
                "⌊": r"\lfloor", "⌋": r"\rfloor", "[": "[", "]": "]"}
    stack: list[int] = []
    native_pairs: set[tuple[int, int]] = set()
    for index, letter in enumerate(alignment.native):
        if letter in closing.values():
            stack.append(index)
        elif letter in closing:
            if stack and alignment.native[stack[-1]] == closing[letter]:
                native_pairs.add((stack.pop(), index))
            else:
                stack.clear()

    if not native_pairs:
        return []

    # Align delimiter families before recovering their exact forms. Otherwise
    # adjacent OCR ']]' versus native '⌉]' has no unique character-level match.
    # Original glyphs remain intact and decide the final endpoint spelling.
    families = str.maketrans({"⌈": "[", "⌉": "]", "⌊": "[", "⌋": "]", "{": "[", "}": "]"})
    visual, native = alignment.text.translate(families), alignment.native.translate(families)
    matches = (_occurrence_matches(visual, native, alignment.glyphs)[0] if alignment.glyphs
               else _agreed_matches(visual, native))
    identities = {alignment.spans[a]: b for a, b in matches.items()}
    token = re.compile(r"(?:\\(?:left|right)\b\s*)?(?P<glyph>\\(?:lceil|rceil|lfloor|rfloor)\b|\\[{}]|[\[\]()])")
    edits = []
    for start, end in ranges:
        endpoints = []
        for match in token.finditer(markdown, start, end):
            if match.group("glyph") in {"[", "(", r"\{", r"\lceil", r"\lfloor"}:
                endpoints.append(match)
                continue
            if not endpoints:
                continue
            left = endpoints.pop()
            pair = (identities.get(left.span("glyph")), identities.get(match.span("glyph")))
            if pair not in native_pairs:
                continue
            # Endpoint shapes alone are not context: require enclosed content
            # to corroborate this occurrence as well (not just two PDF marks).
            if not any(
                left.end("glyph") <= alignment.spans[a][0] < match.start()
                and pair[0] < b < pair[1]
                and alignment.text[a] == alignment.native[b]
                and alignment.text[a] not in "[](){}⌈⌉⌊⌋"
                for a, b in matches.items()
            ):
                continue
            # Only repair bracket-like OCR substitutions, never other operators
            # or invisible TeX delimiters whose intent is not established here.
            allowed = {"[", "]", "(", ")", r"\{", r"\}", *commands.values()}
            if any(endpoint.group("glyph") not in allowed for endpoint in (left, match)):
                continue
            for endpoint, index in zip((left, match), pair):
                replacement = commands[alignment.native[index]]
                if endpoint.group("glyph") != replacement:
                    if replacement.startswith("\\") and re.match(r"[A-Za-z]", markdown[endpoint.end("glyph"):]):
                        replacement += " "  # Terminate the TeX control word.
                    edits.append((*endpoint.span("glyph"), replacement))
    return edits


def math_font_role(glyph: dict) -> str | None:
    """Decode Unicode first, then known math-alphabet font encodings.

    In symbol fonts the mapping applies to Latin alphabet slots only, not the
    entire font (which also contains operators). Unknown encodings abstain.
    """
    value = str(glyph.get("text", ""))
    if len(value) != 1:
        return None
    name = unicodedata.name(value, "")
    if "DOUBLE-STRUCK" in name:
        return "mathbb"
    if ("SCRIPT" in name and "LETTER" not in name) or name.startswith("SCRIPT "):
        return "mathcal"
    if "MATHEMATICAL ITALIC" in name:
        return "ordinary"
    if not re.fullmatch("[A-Z]", unicodedata.normalize("NFKC", value)):
        return None
    font = re.sub(r"^[A-Z]{6}\+", "", str(glyph.get("font", ""))).casefold()
    if font in {"txsym", "txsyb"} or font.startswith(("msbm", "bbold")):
        return "mathbb"
    if font in {"txsys", "txsy"} or font.startswith(("cmsy", "rsfs", "eusm")):
        return "mathcal"
    if font.startswith(("newtxmi", "cmmi", "stixmathitalic")):
        return "ordinary"
    return None


@dataclass
class TexGroup:
    start: int
    end: int
    script: str | None
    children: list[TexGroup]


def tex_groups(value: str) -> list[TexGroup]:
    """Parse balanced TeX groups and braced script arguments, preserving ranges.

    Commands remain opaque; malformed groups are never repaired by guessing a
    closing delimiter. This small structural parser does not evaluate TeX.
    """
    roots: list[TexGroup] = []
    stack: list[TexGroup] = []
    pending = None
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\":
            command = re.match(r"\\(?:[A-Za-z]+|.)", value[index:])
            index += len(command[0]) if command else 1
            pending = None
            continue
        if char in "_^":
            pending = char
        elif char == "{":
            group = TexGroup(index + 1, -1, pending, [])
            (stack[-1].children if stack else roots).append(group)
            stack.append(group)
            pending = None
        elif char == "}":
            if stack:
                stack.pop().end = index
            pending = None
        elif not char.isspace():
            pending = None
        index += 1
    return roots


def script_edits(markdown: str, alignment: GlyphAlignment) -> list[tuple[int, int, str]]:
    """Reconstruct a simple script subtree only from fully aligned PDF glyphs.

    A literal slash can be discarded only when it has no PDF counterpart and
    the following glyph is demonstrably a smaller, lowered/raised script.
    All symbols and all new parent edges must be independently corroborated.
    """
    edits = []
    matches = alignment.layout_matches
    def visit(group: TexGroup) -> None:
        if group.end < 0:
            return
        for child in group.children:
            visit(child)
        if not group.script:
            return
        indexes = [i for i, (s, e) in enumerate(alignment.spans) if group.start <= s < e <= group.end]
        letters = [i for i in indexes if alignment.text[i].isalnum()]
        if not letters or len(letters) > 16:
            return
        if any(not (alignment.text[i].isalnum() or alignment.text[i] == "/") for i in indexes):
            return
        if any(i not in matches or alignment.text[i] != alignment.native[matches[i]] for i in letters):
            return
        if any(i in matches for i in indexes if alignment.text[i] == "/"):
            return
        native_ids = [matches[i] for i in letters]
        if native_ids != list(range(min(native_ids), max(native_ids) + 1)):
            return
        edges = {n: alignment.parents[n] for n in native_ids
                 if n in alignment.parents and alignment.parents[n][0] in native_ids}
        if not edges:
            return
        def render(n: int) -> str:
            result = alignment.native[n]
            for kind in ("_", "^"):
                children = [c for c in native_ids if edges.get(c) == (n, kind)]
                if children:
                    result += kind + "{" + "".join(render(c) for c in children) + "}"
            return result
        target = "".join(render(n) for n in native_ids if n not in edges)
        original = markdown[group.start:group.end]
        # Never strip accents, font commands, or other semantic markup.
        canonical = re.sub(r"([_^])([A-Za-z0-9])", r"\1{\2}", re.sub(r"\s+", "", original))
        if "\\" not in original and canonical != target:
            edits.append((group.start, group.end, target))
    for root in tex_groups(markdown):
        visit(root)
    return [edit for edit in edits if not any(other[0] < edit[0] and edit[1] < other[1] for other in edits)]


def script_substitutions(markdown: str, alignment: GlyphAlignment) -> list[tuple[int, int, str]]:
    """Resolve a script glyph through its already aligned base occurrence.

    Nested scripts can be interleaved in PDF drawing order. The matched base
    and its parent edges provide the context; another occurrence cannot lend
    its superscript to this one.
    """
    atom = re.compile(r"(?P<base>[A-Za-z])(?P<scripts>(?:\s*[_^]\s*\{(?:[^{}]|\{[^{}]*\})+\}){1,2})")
    by_start = {start: i for i, (start, _) in enumerate(alignment.spans)}
    edits = []

    def descendants(parent, kind=None):
        result = []
        for child, (base, script) in alignment.parents.items():
            if base == parent and (kind is None or script == kind):
                result.append(child)
                result.extend(descendants(child))
        return result

    for match in atom.finditer(markdown):
        base = alignment.matches.get(by_start.get(match.start("base")))
        if base is None or alignment.native[base] != match["base"]:
            continue
        offset = match.start("scripts")
        for group in tex_groups(match["scripts"]):
            if group.end < 0 or not group.script:
                continue
            ids = descendants(base, group.script)
            target = "".join(alignment.native[i] for i in ids)
            source = markdown[offset + group.start:offset + group.end]
            text, spans = semantic_math_projection(source)
            if len(text) != len(target) or not text:
                continue
            differences = [i for i in range(len(text)) if text[i] != target[i]]
            if len(differences) != 1:
                continue
            i = differences[0]
            if not (text[i].isascii() and text[i].isalpha() and target[i].isascii() and target[i].isalpha()):
                continue
            start, end = spans[i]
            edits.append((offset + group.start + start, offset + group.start + end, target[i]))
    return edits


_MATH_COMMAND_CHARACTERS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
    "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω", "leq": "≤", "le": "≤", "geq": "≥",
    "ge": "≥", "lceil": "⌈", "rceil": "⌉", "lfloor": "⌊",
    "rfloor": "⌋", "in": "∈", "equiv": "≡", "approx": "≈",
    "pm": "±", "times": "×", "cdot": "·", "sum": "∑", "prod": "∏",
    "mid": "|", "vert": "|",
}
MATH_CHARACTER_COMMANDS = {
    character: command
    for command, character in _MATH_COMMAND_CHARACTERS.items()
    if command not in {"epsilon", "vartheta", "varphi", "le", "ge", "vert"}
}
def semantic_math_projection(value: str) -> tuple[str, list[tuple[int, int]]]:
    """Project TeX or PDF Unicode text to comparable mathematical characters."""
    projected: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\":
            if index + 1 < len(value) and value[index + 1] in "{}[]()|":
                if value[index + 1] in "{}|":
                    projected.append(value[index + 1])
                    spans.append((index, index + 2))
                index += 2
                continue
            command = re.match(r"\\([A-Za-z]+)", value[index:])
            if command:
                name = command.group(1)
                end = index + len(command.group(0))
                if name in _MATH_COMMAND_CHARACTERS:
                    projected.append(_MATH_COMMAND_CHARACTERS[name])
                    spans.append((index, end))
                elif name in {"dots", "ldots", "cdots"}:
                    projected.append("…")
                    spans.append((index, end))
                index = end
                continue
            index += 1
            continue
        normalized = unicodedata.normalize("NFKC", character)
        for visible in normalized:
            if visible.isspace() or visible in "{}_^$*`®©ª«¬︁︂︃︄":
                continue
            if visible in "−–—":
                visible = "-"
            if visible.isprintable():
                projected.append(visible)
                spans.append((index, index + 1))
        index += 1
    return "".join(projected), spans


def math_letter(glyph: dict) -> bool:
    value = glyph["text"]
    if len(value) != 1 or not re.fullmatch("[A-Za-z]", unicodedata.normalize("NFKC", value)):
        return False
    if unicodedata.name(value, "").startswith("MATHEMATICAL "):
        return True
    if math_font_role(glyph) in {"mathbb", "mathcal"}:
        return True
    font = re.sub(r"^[A-Z]{6}\+", "", glyph.get("font", "")).casefold()
    return bool(re.fullmatch(r"(?:newtxmi|cmmi|stixmathitalic)\d*", font))
