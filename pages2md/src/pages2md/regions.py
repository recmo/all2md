"""Independent source inventory and bidirectional transcription accounting.

An overlapping OCR box is only a candidate association. Native glyph identities
must also match; raster-only regions retain the weaker 'visually_associated'
status. Findings are evidence of uncertainty, not mathematical corrections.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

from .alignment import align_glyphs, semantic_math_projection
from .embedded import bbox_coverage, iter_embedded_characters
from .model import Block, Box, EmbeddedEvidence, FIGURE_KINDS
from .quality import output_quality_warnings
from .texstructure import script_memberships, balanced_sizing_delimiters
from .syntax import math_spans


@dataclass(frozen=True)
class Region:
    id: str
    bbox: Box
    text: str
    kind: str
    glyph_ids: tuple[tuple[int, ...], ...] = ()
    excluded: str | None = None


def _id(page: int, kind: str, bbox: Box, text: str) -> str:
    digest = hashlib.sha256(f"{kind}:{bbox}:{text}".encode()).hexdigest()[:12]
    return f"p{page:04d}-{digest}"


def union_box(boxes) -> Box:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def valid_box(b) -> bool:
    return bool(b and len(b) == 4 and all(isinstance(x, (int, float)) for x in b)
                and 0 <= b[0] < b[2] <= 1000 and 0 <= b[1] < b[3] <= 1000)


def source_inventory(page: int, embedded: EmbeddedEvidence,
                     image: Path | None = None) -> list[Region]:
    """Use locally legible glyphs regardless of the OCR's quality score."""
    by_line = {}
    for glyph in iter_embedded_characters(embedded):
        text = glyph["text"]
        if not valid_box(glyph["bbox"]) or not text.strip():
            continue
        # Broken encodings cannot establish missing symbols. Their ink is still
        # accounted for by the raster inventory.
        if "\ufffd" in text or any(not c.isprintable() for c in text):
            continue
        by_line.setdefault(glyph["order"][:2], []).append(glyph)
    regions = []
    for glyphs in by_line.values():
        bbox = union_box(g["bbox"] for g in glyphs)
        text = "".join(g["text"] for g in glyphs)
        excluded = "page_number" if re.fullmatch(r"\d+|[ivxlcdm]+", text, re.I) and (
            bbox[1] > 890 or bbox[3] < 65) else None
        regions.append(Region(_id(page, "native", bbox, text), bbox, text, "native",
                              tuple(g["order"] for g in glyphs), excluded))
    if image is not None and image.is_file():
        for bbox in _ink_regions(image):
            # Do not let one native line account for an entire raster paragraph.
            intersections = [r for r in regions if bbox_coverage(r.bbox, bbox) > .5]
            if intersections and bbox_coverage(bbox, union_box(r.bbox for r in intersections)) > .8:
                continue
            regions.append(Region(_id(page, "ink", bbox, ""), bbox, "", "ink"))
    return sorted(regions, key=lambda r: (r.bbox[1], r.bbox[0], r.id))


def _runs(flags):
    start = None
    for i, active in enumerate([*flags, False]):
        if active and start is None:
            start = i
        elif not active and start is not None:
            yield start, i
            start = None


def _ink_regions(path: Path):
    with Image.open(path) as source:
        gray = source.convert("L")
        gray.thumbnail((1000, 1400))
    # Remove slow background variation, then join nearby glyph strokes. This
    # inventory is deliberately conservative: classification happens later.
    background = gray.filter(ImageFilter.MaxFilter(9))
    ink = ImageChops.subtract(background, gray).point(lambda x: 255 if x > 35 else 0)
    width, height = ink.size
    data = ink.tobytes()
    row_counts = [sum(data[y * width:(y + 1) * width]) // 255 for y in range(height)]
    for y0, y1 in _runs([count >= 3 for count in row_counts]):
        if y1 - y0 < 2:
            continue
        xs = [x for x in range(width) if any(data[y * width + x] for y in range(y0, y1))]
        if len(xs) < 3:
            continue
        # Split distant columns while retaining words and formula operands.
        groups = []
        for x in xs:
            if not groups or x - groups[-1][-1] > width * .045:
                groups.append([])
            groups[-1].append(x)
        for group in groups:
            if len(group) >= 3:
                yield (1000 * group[0] / width, 1000 * y0 / height,
                       1000 * (group[-1] + 1) / width, 1000 * y1 / height)


def audit_regions(regions: list[Region], blocks: list[Block],
                  embedded: EmbeddedEvidence) -> dict:
    matched = set()
    findings = []
    associations = {r.id: [] for r in regions}
    native_counts = {r.id: len(semantic_math_projection(r.text)[0]) for r in regions}
    # Retain source glyph identities so recovery cannot trade away good content.
    matched_counts = Counter()
    for index, block in enumerate(blocks):
        local = [r for r in regions if block.bbox and (
            bbox_coverage(r.bbox, block.bbox) >= .3 or bbox_coverage(block.bbox, r.bbox) >= .5)]
        for region in local:
            associations[region.id].append(index)
        quality = output_quality_warnings(block.markdown)
        math, _ = math_spans(block.markdown)
        if any(not balanced_sizing_delimiters(block.markdown[s.content_start:s.content_end])
               for s in math):
            findings.append({"kind": "unbalanced_sizing_delimiters", "block": index,
                             "bbox": block.bbox, "severity": "error"})
        fatal = [w for w in quality if w in {
            "visual_implausible_output_length", "visual_text_repetition",
            "visual_math_repetition", "visual_malformed_math"}]
        for warning in fatal:
            findings.append({"kind": warning.removeprefix("visual_"), "block": index,
                             "bbox": block.bbox, "severity": "error"})
        if block.kind in FIGURE_KINDS or not block.bbox or len(block.markdown) > 12000:
            continue
        if not any(r.kind == "native" for r in local):
            continue
        aligned = align_glyphs(block.markdown, embedded, block.bbox)
        equal = {a: b for a, b in aligned.matches.items()
                 if aligned.text[a] == aligned.native[b]}
        for b in equal.values():
            glyph = aligned.glyphs[b]
            # Only the original glyph identity matters for ordinary characters.
            matched.add(tuple(glyph["order"]))
        if len(aligned.text) >= 24:
            fraction = len(equal) / len(aligned.text)
            if fraction < .55:
                findings.append({"kind": "unsupported_content", "block": index,
                                 "bbox": block.bbox, "support": round(fraction, 3),
                                 "severity": "review"})
        # Check script ownership only at unambiguous, exactly matched occurrences.
        edges = script_memberships(block.markdown)
        spans, _ = math_spans(block.markdown)
        reverse = {b: a for a, b in equal.items()}
        disagreements = []
        for native_child, (native_parent, kind) in aligned.parents.items():
            if kind not in {"_", "^"} or native_child not in reverse or native_parent not in reverse:
                continue
            child = aligned.spans[reverse[native_child]][0]
            parent = aligned.spans[reverse[native_parent]][0]
            if not any(s.content_start <= child < s.content_end for s in spans):
                continue
            # A braced base or macro argument requires richer TeX expansion;
            # abstain rather than pretending its last glyph is the whole base.
            if not (block.markdown[parent:parent + 1].isalnum()
                    or re.match(r"\\(?:sum|prod)\b", block.markdown[parent:])):
                continue
            if edges.get(child) != (parent, kind):
                disagreements.append({"offset": child, "parent": parent, "script": kind})
        if disagreements:
            findings.append({"kind": "math_structure_disagreement", "block": index,
                             "bbox": block.bbox, "severity": "review",
                             "scripts": disagreements[:20]})
    for region in regions:
        for glyph in region.glyph_ids:
            if glyph in matched:
                matched_counts[region.id] += 1
    inventory = []
    for region in regions:
        entry = asdict(region)
        entry.pop("glyph_ids")
        entry["blocks"] = associations[region.id]
        if region.excluded:
            entry["status"] = "excluded"
        elif region.kind == "native":
            support = matched_counts[region.id] / max(1, len(region.glyph_ids))
            entry["support"] = round(support, 3)
            entry["status"] = "transcribed" if support >= .8 else "unresolved"
            # Ignore isolated extension glyphs, but retain them in the inventory.
            if support < .8 and native_counts[region.id] >= 3:
                findings.append({"kind": "source_content_unresolved", "region": region.id,
                                 "bbox": region.bbox, "support": round(support, 3),
                                 "severity": "review", "source": region.text[:200]})
        else:
            entry["status"] = "visually_associated" if associations[region.id] else "unresolved"
            if not associations[region.id]:
                findings.append({"kind": "uncovered_ink", "region": region.id,
                                 "bbox": region.bbox, "severity": "review"})
        inventory.append(entry)
    return {"regions": inventory, "findings": findings,
            "status": "needs_review" if findings else "checked",
            "matched_glyphs": sorted(matched)}


def preserves_coverage(before: dict, after: dict) -> bool:
    return set(map(tuple, before["matched_glyphs"])).issubset(
        set(map(tuple, after["matched_glyphs"])))


def public_audit(audit: dict) -> dict:
    return {key: value for key, value in audit.items() if key != "matched_glyphs"}
