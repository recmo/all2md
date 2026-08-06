from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

from bs4 import BeautifulSoup

from .compare import normalize

HTML_TABLE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
MAX_PAGE_CHARACTERS = 20_000


def output_quality_warnings(markdown: str, *, page_count: int = 1) -> list[str]:
    """Return content-derived warnings which do not require domain knowledge."""
    warnings: list[str] = []
    allowance = MAX_PAGE_CHARACTERS * max(1, page_count)
    if len(markdown) > allowance:
        warnings.append("visual_implausible_output_length")
    if severe_text_repetition(markdown):
        warnings.append("visual_text_repetition")
    if table_quality_errors(markdown):
        warnings.append("visual_malformed_table")
    if math_syntax_errors(markdown):
        warnings.append("visual_malformed_math")
    return sorted(set(warnings))


def severe_text_repetition(markdown: str) -> bool:
    """Detect runaway prose/formula generation while leaving tables to table checks."""
    return runaway_repetition_span(markdown) is not None


def runaway_repetition_span(markdown: str) -> tuple[int, int] | None:
    """Return the exact duplicate interval for a contiguous generation loop."""
    tokens = _repetition_tokens(markdown)
    words = [value for value, _, _ in tokens]
    if len(words) < 6:
        return None
    # Long outputs should not be quadratic to validate. Repetition collapse is
    # local, so overlapping bounded windows are sufficient.
    window = 2_000
    step = 1_000
    for offset in range(0, len(words), step):
        sample = words[offset : offset + window]
        for size in range(1, min(36, len(sample) // 3 + 1)):
            required = 12 if size == 1 else 8 if size <= 4 else 5 if size <= 12 else 3
            for start in range(0, len(sample) - size * required + 1):
                phrase = sample[start : start + size]
                if all(
                    phrase == sample[start + repeat * size : start + (repeat + 1) * size]
                    for repeat in range(1, required)
                ):
                    absolute_start = offset + start
                    cursor = absolute_start + size * required
                    while words[cursor : cursor + size] == phrase:
                        cursor += size
                    return tokens[absolute_start + size][1], tokens[cursor - 1][2]
        if offset + window >= len(words):
            break
    return None


def truncate_runaway_repetition(markdown: str) -> tuple[str, bool]:
    """Keep one cycle of a proven loop while preserving unrelated surrounding text."""
    repaired = markdown
    changed = False
    for _ in range(128):
        span = runaway_repetition_span(repaired)
        if span is None:
            break
        start, end = span
        repaired = repaired[:start] + repaired[end:]
        changed = True
    return repaired.rstrip(), changed


def _repetition_tokens(markdown: str) -> list[tuple[str, int, int]]:
    ignored = sorted(
        [match.span() for match in HTML_TABLE.finditer(markdown)]
        + [match.span() for match in MARKDOWN_IMAGE.finditer(markdown)]
        + [match.span() for match in re.finditer(r"(?m)^\|.*\|\s*$", markdown)]
        + [
            match.span()
            for match in re.finditer(
                r"\\\(.*?\\\)|\\\[.*?\\\]|\$\$.*?\$\$",
                markdown,
                flags=re.DOTALL,
            )
        ]
    )
    tokens: list[tuple[str, int, int]] = []
    ignored_index = 0
    for match in re.finditer(r"\S+", markdown):
        while ignored_index < len(ignored) and ignored[ignored_index][1] <= match.start():
            ignored_index += 1
        if (
            ignored_index < len(ignored)
            and ignored[ignored_index][0] < match.end()
            and match.start() < ignored[ignored_index][1]
        ):
            continue
        tokens.extend(
            (value, match.start(), match.end())
            for value in normalize(match.group()).split()
        )
    return tokens


def table_quality_errors(markdown: str) -> list[str]:
    errors: list[str] = []
    for table_index, source in enumerate(HTML_TABLE.findall(markdown), 1):
        table = BeautifulSoup(source, "html.parser").find("table")
        if table is None:
            errors.append(f"table {table_index} cannot be parsed")
            continue
        rows = table.find_all("tr")
        if not rows:
            errors.append(f"table {table_index} has no rows")
            continue
        signatures = [_row_signature(row) for row in rows]
        signatures = [signature for signature in signatures if signature]
        widths = [_row_width(row) for row in rows]
        cells = [value for signature in signatures for value in signature if value]
        errors.extend(_matrix_quality_errors(table_index, signatures, widths, cells))

    lines = markdown.splitlines()
    gfm_index = 0
    for index, line in enumerate(lines):
        if not re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", line):
            continue
        gfm_index += 1
        table_index = len(HTML_TABLE.findall(markdown)) + gfm_index
        source_rows = []
        if index:
            source_rows.append(lines[index - 1])
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].startswith("|"):
            source_rows.append(lines[cursor])
            cursor += 1
        signatures = [
            tuple(" ".join(cell.casefold().split()) for cell in _gfm_cells(row))
            for row in source_rows
        ]
        widths = [len(signature) for signature in signatures]
        cells = [value for signature in signatures for value in signature if value]
        errors.extend(_matrix_quality_errors(table_index, signatures, widths, cells))
    return errors


def _matrix_quality_errors(
    table_index: int,
    signatures: list[tuple[str, ...]],
    widths: list[int],
    cells: list[str],
) -> list[str]:
    errors: list[str] = []
    if max(widths, default=0) > 64:
        errors.append(f"table {table_index} has implausibly many columns")
    if len(signatures) > 400:
        errors.append(f"table {table_index} has implausibly many rows")
    if len(signatures) >= 12:
        duplicate_rows = len(signatures) - len(set(signatures))
        if duplicate_rows / len(signatures) >= 0.40:
            errors.append(f"table {table_index} repeats too many rows")
    if len(cells) >= 40:
        most_common = Counter(cells).most_common(1)[0][1]
        if most_common / len(cells) >= 0.45:
            errors.append(f"table {table_index} repeats one cell excessively")
    return errors


def _gfm_cells(row: str) -> list[str]:
    return [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", row.strip("|"))]


def math_syntax_errors(markdown: str) -> list[str]:
    """Validate math delimiters and braces without interpreting the mathematics."""
    errors: list[str] = []
    pairs = ((r"\(", r"\)"), (r"\[", r"\]"))
    for opening, closing in pairs:
        if markdown.count(opening) != markdown.count(closing):
            errors.append(f"unbalanced math delimiter {opening}")
    if markdown.count("$$") % 2:
        errors.append("unbalanced display-math delimiter")

    spans = []
    spans.extend(match.group(1) for match in re.finditer(r"\\\((.*?)\\\)", markdown, re.DOTALL))
    spans.extend(match.group(1) for match in re.finditer(r"\\\[(.*?)\\\]", markdown, re.DOTALL))
    spans.extend(match.group(1) for match in re.finditer(r"\$\$(.*?)\$\$", markdown, re.DOTALL))
    for index, span in enumerate(spans, 1):
        if not _balanced_braces(span):
            errors.append(f"math span {index} has unbalanced braces")
        if not _balanced_environments(span):
            errors.append(f"math span {index} has unbalanced environments")
    return errors


def adjacent_overlap(left: str, right: str) -> bool:
    """Detect content assigned to both of two consecutive physical pages."""
    left_rows, right_rows = _table_signatures(left), _table_signatures(right)
    if min(len(left_rows), len(right_rows)) >= 5:
        shared = len(set(left_rows) & set(right_rows))
        denominator = min(len(set(left_rows)), len(set(right_rows)))
        ratio = shared / max(1, denominator)
        if (shared >= 5 and ratio >= 0.50) or (shared >= 10 and ratio >= 0.20):
            return True
    left_norm, right_norm = normalize(left), normalize(right)
    if min(len(left_norm), len(right_norm)) < 120:
        return False
    shorter, longer = sorted((left_norm, right_norm), key=len)
    if shorter in longer and len(shorter) / max(1, len(longer)) >= 0.35:
        return True
    probe = min(1_500, len(left_norm), len(right_norm))
    if probe >= 500 and left_norm[:probe] == right_norm[:probe]:
        return True
    suffix_probe = min(1_500, len(left_norm), len(right_norm))
    if suffix_probe >= 300 and left_norm[-suffix_probe:] == right_norm[:suffix_probe]:
        return True
    left_tail, right_head = left_norm[-5_000:], right_norm[:5_000]
    left_tokens, right_tokens = set(left_tail.split()), set(right_head.split())
    token_overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    if token_overlap < 0.65:
        return False
    match = SequenceMatcher(None, left_tail, right_head, autojunk=False).find_longest_match()
    return bool(
        match.size >= 0.75 * min(len(left_tail), len(right_head))
        and match.a + match.size >= len(left_tail) - 80
        and match.b <= 80
    )


def _balanced_braces(value: str) -> bool:
    depth = 0
    escaped = False
    for character in value:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _balanced_environments(value: str) -> bool:
    stack: list[str] = []
    for match in re.finditer(r"\\(begin|end)\{([^{}]+)\}", value):
        operation, name = match.groups()
        if operation == "begin":
            stack.append(name)
        elif not stack or stack.pop() != name:
            return False
    return not stack


def _row_signature(row) -> tuple[str, ...]:
    return tuple(
        " ".join(cell.get_text(" ", strip=True).casefold().split())
        for cell in row.find_all(["th", "td"], recursive=False)
    )


def _row_width(row) -> int:
    return sum(
        max(1, int(cell.get("colspan", 1)))
        for cell in row.find_all(["th", "td"], recursive=False)
    )


def _table_signatures(markdown: str) -> list[tuple[str, ...]]:
    signatures: list[tuple[str, ...]] = []
    for source in HTML_TABLE.findall(markdown):
        table = BeautifulSoup(source, "html.parser").find("table")
        if table is not None:
            signatures.extend(_row_signature(row) for row in table.find_all("tr"))
    for line in markdown.splitlines():
        if line.startswith("|") and not re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", line):
            signatures.append(
                tuple(" ".join(cell.casefold().split()) for cell in _gfm_cells(line))
            )
    return [signature for signature in signatures if any(signature)]
