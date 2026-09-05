from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .compare import compare_text
from .model import FORMULA_KINDS, Block, PageResult


LIST_KINDS = {"list", "list_item", "bullet", "bulleted_list", "enumeration", "ordered_list"}
BREAK_KINDS = {
    "heading", "title", "page_title", "section_header", "table", "figure",
    "caption", "formula", "equation", "display_formula",
}
BULLETS = "•◦▪‣●○"
BULLET = re.compile(rf"^(?P<indent>[ \t]*)(?P<marker>[{re.escape(BULLETS)}*+\-–—])\s+(?P<text>\S.*)$")
ENUMERATOR = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>"
    r"\((?P<paren>\d+|[A-Za-z]|[IVXLCDMivxlcdm]{2,})\)|"
    r"(?P<label>\d+|[A-Za-z]|[IVXLCDMivxlcdm]{2,})(?P<delimiter>[.)])"
    r")\s+(?P<text>\S.*)$"
)
ROMAN = frozenset("ivxlcdm")


@contextmanager
def editable_leaves(blocks: list[Block]):
    """Expose list item bodies and ordinary blocks through the same interface.

    List dictionaries are the checkpoint representation; their Markdown is
    derived here after editing, never an independent repair target.
    """
    leaves = []
    bindings = []
    containers = []

    def visit(node, owner):
        for item in node.get("items", []):
            for child in item.get("blocks", []):
                leaf = Block(child.get("kind", "paragraph"), child.get("markdown", ""),
                             bbox=child.get("bbox") or owner.bbox,
                             metadata=child.setdefault("metadata", {}))
                leaves.append(leaf)
                bindings.append((child, leaf))
            for child in item.get("children", []):
                visit(child, owner)

    for block in blocks:
        node = block.metadata.get("list")
        if isinstance(node, dict):
            containers.append(block)
            visit(node, block)
        else:
            leaves.append(block)
    yield leaves
    for child, leaf in bindings:
        child["markdown"] = leaf.markdown
    for block in containers:
        block.markdown = render_list(block.metadata["list"])


@dataclass
class Marker:
    source_marker: str
    source_label: str | None
    source_ordinal: int | None
    marker_style: str
    marker_case: str | None
    indent: int
    text: str


def parse_marker(line: str) -> Marker | None:
    """Parse an item marker only at the beginning of a physical OCR line."""
    bullet = BULLET.match(line)
    if bullet:
        return Marker(
            source_marker=bullet.group("marker"),
            source_label=None,
            source_ordinal=None,
            marker_style="bullet",
            marker_case=None,
            indent=_indent_width(bullet.group("indent")),
            text=bullet.group("text").strip(),
        )
    enumerator = ENUMERATOR.match(line)
    if not enumerator:
        return None
    label = enumerator.group("paren") or enumerator.group("label")
    if label.isdigit():
        style = "decimal"
        ordinal = int(label)
        case = None
    else:
        style = "ambiguous" if len(label) == 1 or set(label.casefold()) <= ROMAN else "alpha"
        ordinal = None
        case = "upper" if label.isupper() else "lower"
    return Marker(
        source_marker=enumerator.group("marker"),
        source_label=label,
        source_ordinal=ordinal,
        marker_style=style,
        marker_case=case,
        indent=_indent_width(enumerator.group("indent")),
        text=enumerator.group("text").strip(),
    )


def annotate_native_list_block(block: Block) -> None:
    """Retain native list evidence before document-level grouping."""
    candidates = _line_candidates(block.markdown)
    if candidates:
        block.metadata["list_candidates"] = candidates
    if block.kind in LIST_KINDS and not block.markdown.strip():
        block.metadata["list_container"] = True


def normalize_lists(pages: list[PageResult]) -> None:
    """Normalize OCR structure before rendering list nodes."""
    _filter_unsupported_ungrounded_blocks(pages)
    for page in pages:
        page.blocks = normalize_page_blocks(page.blocks)
    _continue_items_across_pages(pages)
    _stitch_list_hierarchy(pages)
    _link_lists_across_pages(pages)


def _filter_unsupported_ungrounded_blocks(pages: list[PageResult]) -> None:
    for page in pages:
        has_grounded_content = any(
            block.bbox is not None and block.markdown.strip()
            for block in page.blocks
        )
        if not has_grounded_content:
            continue
        retained: list[Block] = []
        removed_ungrounded = False
        for block in page.blocks:
            if not block.metadata.get("native_ungrounded"):
                retained.append(block)
                continue
            support = compare_text(block.markdown, page.embedded.text).token_coverage
            if support is not None and support >= 0.8:
                block.metadata["embedded_token_support"] = support
                retained.append(block)
            else:
                removed_ungrounded = True
        if removed_ungrounded:
            page.warnings = sorted(set([*page.warnings, "visual_unsupported_ungrounded_text"]))
        page.blocks = retained


def render_list(node: dict[str, Any], indent: int = 0) -> str:
    lines: list[str] = []
    prefix_indent = " " * indent
    style = node["marker_style"]
    for item in node["items"]:
        if style == "decimal":
            marker = f"{item['source_ordinal']}."
        elif style in {"alpha", "roman"}:
            marker = f"- **{item['source_marker']}**"
        else:
            marker = "-"
        paragraphs = item.get("blocks", [])
        first = paragraphs[0].get("markdown", "") if paragraphs else ""
        lines.append(f"{prefix_indent}{marker} {first}".rstrip())
        child_indent = indent + (max(4, len(marker) + 1) if style == "decimal" else 4)
        for paragraph in paragraphs[1:]:
            lines.append("")
            continuation_indent = " " * child_indent
            content = paragraph.get("markdown", "")
            lines.extend(f"{continuation_indent}{line}".rstrip() for line in content.splitlines())
        for child in item.get("children", []):
            lines.extend(render_list(child, child_indent).splitlines())
    return "\n".join(lines)


def validate_list_node(node: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if node.get("marker_style") not in {"bullet", "decimal", "alpha", "roman"}:
        errors.append("invalid marker style")
    items = node.get("items")
    if not isinstance(items, list) or not items:
        return [*errors, "list has no items"]
    for index, item in enumerate(items):
        if not isinstance(item.get("blocks"), list) or not item["blocks"]:
            errors.append(f"item {index + 1} has no content blocks")
        else:
            paragraphs = item["blocks"]
            for left, right in zip(paragraphs, paragraphs[1:]):
                if left.get("kind") == right.get("kind") == "paragraph" and _is_soft_continuation(
                    left.get("markdown", ""), right.get("markdown", "")
                ):
                    errors.append(f"item {index + 1} has unresolved sentence continuation")
            children = item.get("children", [])
            if len(paragraphs) > 1 and children:
                target = _last_item(children[-1])
                target_blocks = target.get("blocks", []) if target else []
                if (
                    target_blocks
                    and paragraphs[-2].get("kind") == paragraphs[-1].get("kind") == "paragraph"
                    and target_blocks[-1].get("kind") == "paragraph"
                    and _sentence_is_complete(paragraphs[-2].get("markdown", ""))
                    and _is_soft_continuation(
                        target_blocks[-1].get("markdown", ""),
                        paragraphs[-1].get("markdown", ""),
                    )
                ):
                    errors.append(f"item {index + 1} has misattached nested continuation")
        if not item.get("source_marker"):
            errors.append(f"item {index + 1} lacks source marker")
        if node.get("marker_style") == "decimal" and not isinstance(item.get("source_ordinal"), int):
            errors.append(f"decimal item {index + 1} lacks ordinal")
        if node.get("marker_style") in {"alpha", "roman"} and not item.get("source_label"):
            errors.append(f"labeled item {index + 1} lacks source label")
        for child in item.get("children", []):
            errors.extend(f"item {index + 1}: {error}" for error in validate_list_node(child))
    if node.get("marker_style") == "decimal":
        ordinals = [item.get("source_ordinal") for item in items]
        anomalies = node.get("sequence_anomalies", [])
        for left, right in zip(ordinals, ordinals[1:]):
            if right != left + 1 and not any(
                anomaly.get("previous") == left and anomaly.get("current") == right
                for anomaly in anomalies
            ):
                errors.append(f"unexplained decimal sequence change {left} -> {right}")
    return errors


def normalize_page_blocks(blocks: list[Block]) -> list[Block]:
    for block in blocks:
        if not isinstance(block.metadata.get("list"), dict):
            annotate_native_list_block(block)
    blocks = [block for block in blocks if not _is_empty_marker_artifact(block.markdown)]
    output: list[Block] = []
    index = 0
    while index < len(blocks):
        normalized = blocks[index].metadata.get("list")
        if isinstance(normalized, dict):
            _repair_node_continuations(normalized)
            blocks[index].markdown = render_list(normalized)
            output.append(blocks[index])
            index += 1
            continue
        container = blocks[index] if blocks[index].metadata.get("list_container") else None
        start = index + 1 if container else index
        if start >= len(blocks):
            if not container:
                output.append(blocks[index])
            # Native OCR may emit an empty list container alongside the actual
            # visual text.  It is structural scaffolding, not document content.
            break
        first_items = _items_from_block(blocks[start])
        if not first_items:
            if not container:
                output.append(blocks[index])
            index += 1
            continue

        run_blocks: list[Block] = []
        flat_items: list[dict[str, Any]] = []
        cursor = start
        while cursor < len(blocks):
            block = blocks[cursor]
            if flat_items:
                sibling = _enclosed_continuations(blocks, cursor, flat_items[-1])
                if sibling is not None:
                    for continuation in blocks[cursor:sibling]:
                        _append_continuation(flat_items[-1], continuation)
                        run_blocks.append(continuation)
                    cursor = sibling
                    continue
            if block.kind in BREAK_KINDS:
                break
            items = _items_from_block(block)
            if items:
                run_blocks.append(block)
                flat_items.extend(items)
                cursor += 1
                continue
            if flat_items and _is_continuation(block, flat_items[-1]):
                _append_continuation(flat_items[-1], block)
                run_blocks.append(block)
                cursor += 1
                continue
            break

        strong = (
            bool(container)
            or any(block.kind in LIST_KINDS for block in run_blocks)
            or any(
                item.get("source_marker") in BULLETS
                for item in flat_items
            )
        )
        if not _list_evidence_is_sufficient(flat_items, run_blocks, strong=strong):
            output.append(blocks[index])
            index += 1
            continue
        _assign_levels(flat_items)
        _resolve_marker_styles(flat_items)
        nodes, consumed = _build_nodes(flat_items, 0, 0)
        if consumed != len(flat_items):
            nodes.extend(_flat_nodes(flat_items[consumed:]))
        for node in nodes:
            _repair_node_continuations(node)
            list_block = _node_block(node, run_blocks, container)
            output.append(list_block)
        index = cursor
    return output


def _is_empty_marker_artifact(value: str) -> bool:
    """Discard OCR blocks made only of list glyphs with no item content."""
    compact = "".join(value.split())
    return bool(compact) and all(character in BULLETS for character in compact)


def _items_from_block(block: Block) -> list[dict[str, Any]]:
    candidates = block.metadata.get("list_candidates") or _line_candidates(block.markdown)
    if not candidates:
        return []
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        bbox = list(block.bbox) if block.bbox else None
        geometry_indent = float(block.bbox[0]) if block.bbox else 0.0
        item = {
            **candidate,
            "geometry_indent": geometry_indent + candidate["text_indent"] * 4,
            "nesting_level": 0,
            "blocks": [{"kind": "paragraph", "markdown": candidate["text"]}],
            "children": [],
            "source_pages": list(block.source_pages),
            "bbox": bbox,
            "provenance": list(block.provenance),
            "source_block_kind": block.kind,
        }
        items.append(item)
    return items


def _line_candidates(value: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    paragraph_break = False
    for raw_line in value.splitlines():
        if not raw_line.strip():
            paragraph_break = True
            continue
        marker = parse_marker(raw_line)
        if marker:
            items.append({
                "source_marker": marker.source_marker,
                "source_label": marker.source_label,
                "source_ordinal": marker.source_ordinal,
                "marker_style": marker.marker_style,
                "marker_case": marker.marker_case,
                "text_indent": marker.indent,
                "text": marker.text,
                "continuation_paragraphs": [],
            })
            paragraph_break = False
        elif items:
            line = raw_line.strip()
            if paragraph_break:
                items[-1]["continuation_paragraphs"].append(line)
            else:
                items[-1]["text"] = f"{items[-1]['text']} {line}".strip()
            paragraph_break = False
        else:
            return []
    return items


def _repair_node_continuations(node: dict[str, Any]) -> None:
    """Join sentence fragments and reattach fragments assigned before a nested item."""
    _recover_rendered_labels(node)
    repairs = int(node.get("continuation_repairs", 0))
    for item in node.get("items", []):
        blocks = item.get("blocks", [])
        children = item.get("children", [])

        if len(blocks) > 1 and children:
            fragment = blocks[-1].get("markdown", "").strip()
            parent_text = blocks[-2].get("markdown", "").strip()
            target = _last_item(children[-1])
            target_blocks = target.get("blocks", []) if target else []
            target_text = target_blocks[-1].get("markdown", "").strip() if target_blocks else ""
            if (
                fragment
                and blocks[-2].get("kind") == blocks[-1].get("kind") == "paragraph"
                and target_blocks and target_blocks[-1].get("kind") == "paragraph"
                and _sentence_is_complete(parent_text)
                and _is_soft_continuation(target_text, fragment)
            ):
                target_blocks[-1]["markdown"] = f"{target_text} {fragment}".strip()
                blocks.pop()
                repairs += 1

        index = 1
        while index < len(blocks):
            previous = blocks[index - 1].get("markdown", "").strip()
            continuation = blocks[index].get("markdown", "").strip()
            if (blocks[index - 1].get("kind") == blocks[index].get("kind") == "paragraph"
                and _is_soft_continuation(previous, continuation)):
                blocks[index - 1]["markdown"] = f"{previous} {continuation}".strip()
                blocks.pop(index)
                repairs += 1
            else:
                index += 1

        for child in children:
            _repair_node_continuations(child)
            repairs += int(child.pop("continuation_repairs", 0))
    if repairs:
        node["continuation_repairs"] = repairs


def _recover_rendered_labels(node: dict[str, Any]) -> None:
    """Restore labeled-list semantics from an older rendered bullet representation."""
    items = node.get("items", [])
    if node.get("marker_style") == "bullet" and items:
        recovered: list[tuple[dict[str, Any], Marker]] = []
        for item in items:
            blocks = item.get("blocks", [])
            text = blocks[0].get("markdown", "") if blocks else ""
            match = re.match(r"^\*\*(\(?[A-Za-zIVXLCDMivxlcdm]+[.)])\*\*\s+(.+)$", text)
            marker = parse_marker(f"{match.group(1)} {match.group(2)}") if match else None
            if marker is None or marker.marker_style == "bullet":
                recovered = []
                break
            recovered.append((item, marker))
        if recovered:
            for item, marker in recovered:
                item["source_marker"] = marker.source_marker
                item["source_label"] = marker.source_label
                item["source_ordinal"] = marker.source_ordinal
                item["marker_style"] = marker.marker_style
                item["marker_case"] = marker.marker_case
                item["blocks"][0]["markdown"] = marker.text
            _resolve_marker_styles(items)
            styles = {(item["marker_style"], item.get("marker_case")) for item in items}
            if len(styles) == 1:
                style, case = styles.pop()
                node["ordered"] = True
                node["marker_style"] = style
                node["marker_case"] = case
                node["recovered_rendered_labels"] = True


def _last_item(node: dict[str, Any]) -> dict[str, Any] | None:
    items = node.get("items", [])
    return items[-1] if items else None


def _is_soft_continuation(previous: str, continuation: str) -> bool:
    return (
        bool(previous)
        and bool(continuation)
        and not _sentence_is_complete(previous)
        and _starts_lowercase(continuation)
    )


def _sentence_is_complete(value: str) -> bool:
    return bool(re.search(r"[.!?…][\"'’”\)\]]*$", value.rstrip()))


def _starts_lowercase(value: str) -> bool:
    for character in value.lstrip():
        if character.isalpha():
            return character.islower()
        if character.isdigit():
            return False
    return False


def _list_evidence_is_sufficient(
    items: list[dict[str, Any]], blocks: list[Block], *, strong: bool
) -> bool:
    if strong:
        return bool(items)
    if len(items) < 2:
        return False
    compatible_counts: dict[str, int] = {}
    for item in items:
        style = item["marker_style"]
        key = "label" if style in {"ambiguous", "alpha", "roman"} else style
        compatible_counts[key] = compatible_counts.get(key, 0) + 1
    if max(compatible_counts.values(), default=0) < 2:
        return False
    boxes = [block.bbox for block in blocks if block.bbox]
    if len(boxes) >= 2:
        for left, right in zip(boxes, boxes[1:]):
            gap = right[1] - left[3]
            if gap < -10 or gap > 160:
                return False
        roots = sorted(item["geometry_indent"] for item in items)
        return roots[-1] - roots[0] <= 240
    return True


def _assign_levels(items: list[dict[str, Any]]) -> None:
    bands: list[float] = []
    for item in items:
        value = float(item["geometry_indent"])
        nearest = next((index for index, band in enumerate(bands) if abs(value - band) <= 12), None)
        if nearest is None:
            bands.append(value)
            bands.sort()
            nearest = bands.index(value)
        item["nesting_level"] = nearest


def _resolve_marker_styles(items: list[dict[str, Any]]) -> None:
    index = 0
    while index < len(items):
        item = items[index]
        if item["marker_style"] != "ambiguous":
            index += 1
            continue
        level = item["nesting_level"]
        case = item["marker_case"]
        end = index
        group = []
        while end < len(items):
            candidate = items[end]
            if candidate["nesting_level"] != level or candidate["marker_case"] != case:
                break
            if candidate["marker_style"] not in {"ambiguous", "alpha"}:
                break
            group.append(candidate)
            end += 1
        labels = [candidate["source_label"] for candidate in group]
        alpha_values = [_alpha_value(label) for label in labels]
        roman_values = [_roman_value(label) for label in labels]
        alpha_sequence = all(right == left + 1 for left, right in zip(alpha_values, alpha_values[1:]))
        roman_sequence = all(
            value is not None for value in roman_values
        ) and all(right == left + 1 for left, right in zip(roman_values, roman_values[1:]))
        style = "roman" if roman_sequence and (len(group) > 1 or len(labels[0]) > 1) and not alpha_sequence else "alpha"
        for candidate in group:
            candidate["marker_style"] = style
            candidate["source_ordinal"] = (
                _roman_value(candidate["source_label"])
                if style == "roman"
                else _alpha_value(candidate["source_label"])
            )
        index = end


def _build_nodes(
    items: list[dict[str, Any]], index: int, level: int
) -> tuple[list[dict[str, Any]], int]:
    nodes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    while index < len(items):
        item = items[index]
        item_level = item["nesting_level"]
        if item_level < level:
            break
        if item_level > level:
            if current and current["items"]:
                children, index = _build_nodes(items, index, item_level)
                current["items"][-1]["children"].extend(children)
                continue
            item["nesting_level"] = level
        style_key = (item["marker_style"], item.get("marker_case"))
        if current is None or (current["marker_style"], current.get("marker_case")) != style_key:
            current = _new_node(item)
            nodes.append(current)
        for continuation in item.pop("continuation_paragraphs", []):
            item["blocks"].append({"kind": "paragraph", "markdown": continuation})
        current["items"].append(item)
        index += 1
    for node in nodes:
        _finish_node(node)
    return nodes, index


def _flat_nodes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = []
    for item in items:
        item["nesting_level"] = 0
        node = _new_node(item)
        node["items"].append(item)
        _finish_node(node)
        nodes.append(node)
    return nodes


def _new_node(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "ordered": item["marker_style"] != "bullet",
        "marker_style": item["marker_style"],
        "marker_case": item.get("marker_case"),
        "nesting_level": item["nesting_level"],
        "items": [],
        "source_pages": [],
        "bbox": None,
        "provenance": [],
        "sequence_anomalies": [],
    }


def _finish_node(node: dict[str, Any]) -> None:
    node["source_pages"] = sorted({page for item in node["items"] for page in item["source_pages"]})
    node["provenance"] = [entry for item in node["items"] for entry in item["provenance"]]
    boxes = [item["bbox"] for item in node["items"] if item.get("bbox")]
    if boxes:
        node["bbox"] = [
            min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes),
        ]
    if node["marker_style"] == "decimal":
        ordinals = [item["source_ordinal"] for item in node["items"]]
        node["sequence_anomalies"] = [
            {"previous": left, "current": right, "reason": "source_sequence"}
            for left, right in zip(ordinals, ordinals[1:])
            if right != left + 1
        ]


def _node_block(node: dict[str, Any], blocks: list[Block], container: Block | None) -> Block:
    metadata: dict[str, Any] = {"list": node}
    repairs = [repair for block in blocks
               for repair in block.metadata.get("embedded_text_repairs", [])]
    if repairs:
        metadata["embedded_text_repairs"] = repairs
    return Block(
        kind="list",
        markdown=render_list(node),
        bbox=tuple(node["bbox"]) if node.get("bbox") else None,
        source_pages=list(node["source_pages"]),
        provenance=list(node["provenance"]),
        metadata=metadata,
    )


def _is_continuation(block: Block, previous: dict[str, Any]) -> bool:
    if block.kind != "paragraph" or not block.markdown.strip() or not block.bbox or not previous.get("bbox"):
        return False
    if parse_marker(block.markdown.splitlines()[0]):
        return False
    left, top, _, _ = block.bbox
    previous_box = previous["bbox"]
    return left >= previous_box[0] + 14 and -10 <= top - previous_box[3] <= 120


def _append_continuation(item: dict[str, Any], block: Block) -> None:
    # Preserve block boundaries and formula source, including TeX line breaks.
    for paragraph in item.pop("continuation_paragraphs", []):
        item["blocks"].append({"kind": "paragraph", "markdown": paragraph})
    item["continuation_paragraphs"] = []
    item["blocks"].append({"kind": block.kind, "markdown": block.markdown.strip(),
                           "bbox": list(block.bbox) if block.bbox else None,
                           "source_pages": list(block.source_pages),
                           "provenance": list(block.provenance)})
    item["source_pages"] = sorted(set([*item["source_pages"], *block.source_pages]))
    item["provenance"].extend(block.provenance)
    if item.get("bbox") and block.bbox:
        a, b = item["bbox"], block.bbox
        item["bbox"] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _enclosed_continuations(blocks: list[Block], start: int, previous: dict[str, Any]) -> int | None:
    """Confirm item-body blocks by indentation AND a following sibling marker.

    A display equation alone cannot extend a list. All intervening blocks must
    stay inside the item indentation, in reading order, up to a compatible next
    item at the original margin. Headings/tables/figures and ambiguous geometry
    remain boundaries; no document vocabulary is involved.
    """
    box = previous.get("bbox")
    if not box:
        return None
    bottom = box[3]
    for index in range(start, len(blocks)):
        block = blocks[index]
        if block.kind not in {"paragraph", *FORMULA_KINDS, *LIST_KINDS} or not block.bbox:
            return None
        left, top, _, end = block.bbox
        if not -10 <= top - bottom <= 120:
            return None
        candidates = _items_from_block(block) if block.kind not in FORMULA_KINDS else []
        if candidates:
            following = candidates[0]
            if index == start or abs(following["geometry_indent"] - previous["geometry_indent"]) > 12:
                return None
            return index if _successive_markers(previous, following) else None
        if left < box[0] + 14 or not block.markdown.strip():
            return None
        bottom = end
    return None


def _successive_markers(left: dict[str, Any], right: dict[str, Any]) -> bool:
    a, b = left["marker_style"], right["marker_style"]
    if a == b == "decimal":
        return right["source_ordinal"] == left["source_ordinal"] + 1
    if a == b == "bullet":
        return True
    if a not in {"ambiguous", "alpha", "roman"} or b not in {"ambiguous", "alpha", "roman"}:
        return False
    if left.get("marker_case") != right.get("marker_case"):
        return False
    for decode in (_alpha_value, _roman_value):
        x, y = decode(left["source_label"]), decode(right["source_label"])
        if x is not None and y is not None and y == x + 1:
            return True
    return False


def _continue_items_across_pages(pages: list[PageResult]) -> None:
    for previous, current in zip(pages, pages[1:]):
        if current.number != previous.number + 1 or not previous.blocks or not current.blocks:
            continue
        list_block = previous.blocks[-1]
        continuation = current.blocks[0]
        node = list_block.metadata.get("list") if list_block.kind == "list" else None
        if not node or continuation.kind != "paragraph" or not continuation.bbox:
            continue
        last_item = node["items"][-1]
        last_box = last_item.get("bbox")
        if (
            not last_box
            or continuation.bbox[0] < last_box[0] + 14
            or continuation.bbox[1] > 200
            or parse_marker(continuation.markdown.splitlines()[0])
        ):
            continue
        last_item["blocks"].append({"kind": "paragraph", "markdown": " ".join(continuation.markdown.split())})
        last_item["source_pages"] = sorted(set([*last_item["source_pages"], current.number]))
        last_item["provenance"].extend(continuation.provenance)
        _repair_node_continuations(node)
        _finish_node(node)
        list_block.source_pages = list(node["source_pages"])
        list_block.markdown = render_list(node)
        current.blocks.pop(0)


def _stitch_list_hierarchy(pages: list[PageResult]) -> None:
    """Preserve list nesting across OCR block and physical-page boundaries."""
    for page in pages:
        retained: list[Block] = []
        for block in page.blocks:
            previous = retained[-1] if retained else None
            if previous and _can_nest_adjacent_blocks(previous, block):
                parent = previous.metadata["list"]
                child = block.metadata["list"]
                _attach_child_node(parent, child)
                previous.markdown = render_list(parent)
                previous.source_pages = sorted(set([*previous.source_pages, *block.source_pages]))
                previous.provenance.extend(block.provenance)
                previous.metadata["stitched_list_blocks"] = (
                    int(previous.metadata.get("stitched_list_blocks", 0)) + 1
                )
                if previous.bbox and block.bbox:
                    previous.bbox = (
                        min(previous.bbox[0], block.bbox[0]),
                        min(previous.bbox[1], block.bbox[1]),
                        max(previous.bbox[2], block.bbox[2]),
                        max(previous.bbox[3], block.bbox[3]),
                    )
                continue
            retained.append(block)
        page.blocks = retained

    for previous, current in zip(pages, pages[1:]):
        if current.number != previous.number + 1 or not previous.blocks or not current.blocks:
            continue
        left, right = previous.blocks[-1], current.blocks[0]
        left_node = left.metadata.get("list") if left.kind == "list" else None
        right_node = right.metadata.get("list") if right.kind == "list" else None
        if not isinstance(left_node, dict) or not isinstance(right_node, dict):
            continue
        depth = _continued_list_depth(left_node, right_node)
        if depth is None:
            continue
        indent = depth * 4
        right.metadata["render_indent"] = indent
        right.metadata["continues_from_page"] = previous.number
        right_node["nesting_level"] = depth
        _set_item_levels(right_node, depth)
        matching = _node_at_open_depth(left_node, depth)
        if matching is not None:
            matching["continues_on_page"] = current.number
            right_node["continues_from_page"] = previous.number


def _can_nest_adjacent_blocks(parent: Block, child: Block) -> bool:
    parent_node = parent.metadata.get("list") if parent.kind == "list" else None
    child_node = child.metadata.get("list") if child.kind == "list" else None
    if not isinstance(parent_node, dict) or not isinstance(child_node, dict):
        return False
    if not parent.bbox or not child.bbox:
        return False
    horizontal = child.bbox[0] - parent.bbox[0]
    vertical = child.bbox[1] - parent.bbox[3]
    if not 12 <= horizontal <= 96 or not -12 <= vertical <= 96:
        return False
    return _can_be_child_style(parent_node, child_node)


def _can_be_child_style(parent: dict[str, Any], child: dict[str, Any]) -> bool:
    ranks = {"decimal": 0, "alpha": 1, "roman": 2, "bullet": 3}
    parent_rank = ranks.get(parent.get("marker_style"))
    child_rank = ranks.get(child.get("marker_style"))
    return parent_rank is not None and child_rank is not None and child_rank > parent_rank


def _attach_child_node(parent: dict[str, Any], child: dict[str, Any]) -> None:
    item = _last_item(parent)
    if item is None:
        return
    depth = int(parent.get("nesting_level", 0)) + 1
    child["nesting_level"] = depth
    _set_item_levels(child, depth)
    children = item.setdefault("children", [])
    if children and _nodes_continue(children[-1], child):
        children[-1]["items"].extend(child.get("items", []))
        _finish_node(children[-1])
    else:
        children.append(child)


def _continued_list_depth(left: dict[str, Any], right: dict[str, Any]) -> int | None:
    for depth, candidate in reversed(_open_list_stack(left)):
        if _nodes_continue(candidate, right):
            return depth
    return None


def _open_list_stack(node: dict[str, Any], depth: int = 0) -> list[tuple[int, dict[str, Any]]]:
    stack = [(depth, node)]
    item = _last_item(node)
    children = item.get("children", []) if item else []
    if children:
        stack.extend(_open_list_stack(children[-1], depth + 1))
    return stack


def _node_at_open_depth(node: dict[str, Any], depth: int) -> dict[str, Any] | None:
    return next((candidate for level, candidate in _open_list_stack(node) if level == depth), None)


def _nodes_continue(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if (left.get("marker_style"), left.get("marker_case")) != (
        right.get("marker_style"), right.get("marker_case")
    ):
        return False
    left_item, right_items = _last_item(left), right.get("items", [])
    if left_item is None or not right_items:
        return False
    if left.get("marker_style") == "bullet":
        return True
    left_ordinal = left_item.get("source_ordinal")
    right_ordinal = right_items[0].get("source_ordinal")
    return isinstance(left_ordinal, int) and right_ordinal == left_ordinal + 1


def _set_item_levels(node: dict[str, Any], depth: int) -> None:
    for item in node.get("items", []):
        item["nesting_level"] = depth
        for child in item.get("children", []):
            child["nesting_level"] = depth + 1
            _set_item_levels(child, depth + 1)


def _link_lists_across_pages(pages: list[PageResult]) -> None:
    for previous, current in zip(pages, pages[1:]):
        if current.number != previous.number + 1 or not previous.blocks or not current.blocks:
            continue
        left, right = previous.blocks[-1], current.blocks[0]
        left_node = left.metadata.get("list") if left.kind == "list" else None
        right_node = right.metadata.get("list") if right.kind == "list" else None
        if not left_node or not right_node:
            continue
        if (left_node["marker_style"], left_node.get("marker_case")) != (
            right_node["marker_style"], right_node.get("marker_case")
        ):
            continue
        if left.bbox and right.bbox and abs(left.bbox[0] - right.bbox[0]) > 24:
            continue
        if left_node["marker_style"] == "decimal":
            previous_ordinal = left_node["items"][-1]["source_ordinal"]
            current_ordinal = right_node["items"][0]["source_ordinal"]
            if current_ordinal != previous_ordinal + 1:
                continue
        left_node["continues_on_page"] = current.number
        right_node["continues_from_page"] = previous.number


def _alpha_value(label: str) -> int:
    value = 0
    for character in label.casefold():
        value = value * 26 + ord(character) - ord("a") + 1
    return value


def _roman_value(label: str) -> int | None:
    folded = label.casefold()
    if not folded or set(folded) - ROMAN:
        return None
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    previous = 0
    for character in reversed(folded):
        value = values[character]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total


def _indent_width(value: str) -> int:
    return len(value.expandtabs(4))
