"""Bounded visual reads of disputed equations, corroborated independently.

Crop coordinates never masquerade as page coordinates. All raw reads remain
observations; only an agreed complete expression may replace one page block.
"""
from __future__ import annotations

import re
from copy import deepcopy

from PIL import Image

from .embedded import bbox_iou
from .model import FORMULA_KINDS
from .quality import math_syntax_errors, output_quality_warnings
from .syntax import math_spans
from .util import atomic_text, sha256_file


def math_key(text):
    """Comparison only: preserve operators, script direction, and font roles."""
    text = re.sub(r"\\(?:mathrm|text)\b", "", text)
    text = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)\b", "", text)
    text = re.sub(r"\\(leq|geq)\b", lambda m: "\\"+m[1][:2], text)
    text = re.sub(r"\s+", "", text)
    for marker in (r"\(", r"\)", r"\[", r"\]", "$$"):
        text = text.replace(marker, "")
    return text.strip(".,; ")


def corroborates(body, candidate):
    key, peer = math_key(body), math_key(candidate)
    if not key or peer.count(key) != 1:
        return False
    before, after = peer.split(key)
    # A complete expression or a top-level-looking array row, not a matching
    # substring inside a different expression (x_i versus x_i^2, for example).
    return (not before or before.endswith(r"\\")) and (
        not after or after.lstrip(".,;").startswith((r"\\", r"\end{")))


def standalone_math(block):
    spans, unmatched = math_spans(block.markdown)
    if unmatched or len(spans) != 1 or math_syntax_errors(block.markdown):
        return None
    span = spans[0]
    outside = block.markdown[:span.start] + block.markdown[span.end:]
    if outside.strip(" .,:;\n\t"):
        return None
    return block.markdown[span.content_start:span.content_end].strip()


def disputed_regions(primary, peers):
    if len(primary.source_pages) != 1 or not peers:
        return []
    targets = []
    for index, block in enumerate(primary.blocks):
        box, body = block.bbox, standalone_math(block)
        if (block.kind not in FORMULA_KINDS or not box or body is None
                or not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000)
                or not 8 <= len(math_key(body)) <= 240 or box[3]-box[1] > 100):
            continue
        agreed = any(candidate.bbox and bbox_iou(box, candidate.bbox) >= .65
            and math_key(candidate.markdown) == math_key(block.markdown)
            for peer in peers for candidate in peer.blocks)
        if not agreed:
            targets.append((index, block))
    return sorted(targets, key=lambda item: len(math_key(item[1].markdown)))[:2]


def render_region(image, target, neighbors, destination):
    left, top, right, bottom = target
    crop_top, crop_bottom = max(0, top-10), min(1000, bottom+10)
    for block in neighbors:
        box = block.bbox
        if not box or tuple(box) == tuple(target) or min(right,box[2]) <= max(left,box[0]):
            continue
        if box[3] <= top:
            crop_top = max(crop_top, (box[3]+top)/2)
        if box[1] >= bottom:
            crop_bottom = min(crop_bottom, (bottom+box[1])/2)
    box = (max(0,left-12),crop_top,min(1000,right+12),crop_bottom)
    with Image.open(image) as page:
        pixels = tuple(round(v*s/1000) for v,s in zip(box,(page.width,page.height,page.width,page.height)))
        crop = page.crop(pixels).convert("RGB")
        scale = min(3,960/crop.width,900/crop.height)
        crop = crop.resize((max(1,round(crop.width*scale)),max(1,round(crop.height*scale))),Image.Resampling.LANCZOS)
        canvas = Image.new("RGB",(1024,max(384,crop.height+64)),"white")
        offset = ((canvas.width-crop.width)//2,32)
        canvas.paste(crop,offset)
        destination.parent.mkdir(parents=True,exist_ok=True)
        canvas.save(destination)
    return {"page_bbox":list(box),"pixel_bbox":pixels,"canvas_size":canvas.size,
            "canvas_offset":offset,"scale":scale,"image_sha256":sha256_file(destination)}


def collect_regions(source_page, primary, peers, backend, bundle, *, warnings=None):
    if not getattr(backend, "supports_region_recovery", False):
        return []
    results = []
    for index, block in disputed_regions(primary, peers):
        try:
            results.append(_read_region(source_page,primary,index,block,backend,bundle))
        except Exception:
            if warnings is not None:
                warnings.append("visual_region_ocr_failed")
    return results


def _read_region(source_page, primary, index, block, backend, bundle):
    from .native import parse_native_observation
    image = bundle / "region-crops" / f"page-{source_page.number}-{primary.id}-{index}.png"
    geometry = render_region(source_page.image_path,block.bbox,primary.blocks,image)
    recognize = getattr(backend,"recognize_region",backend.recognize_detail)
    raw, generation = recognize(image)
    generation = {**generation,"coordinate_frame":"crop","region_geometry":geometry,
                  "region_target_bbox":list(block.bbox),"region_target_kind":block.kind,
                  "region_target_index":index,"region_primary":primary.id,
                  "region_image":str(image.relative_to(bundle))}
    # Include the target as well as the image: two identical equations at
    # distinct physical positions must retain distinct evidence identities.
    mode = "region_detail-"+geometry["image_sha256"][:12]+f"-{index}"
    observation = parse_native_observation(raw,mode=mode,source_pages=[source_page.number],generation=generation)
    observation.mode = "region_detail"
    path = bundle / "raw" / f"{observation.id}.txt"
    if not path.exists():
        atomic_text(path,raw)
    return observation


def apply_regions(blocks, regions, peers, primary):
    provenance, warnings = [], []
    for region in regions:
        target = region.generation.get("region_target_bbox")
        if not target or len(target) != 4 or region.generation.get("finish_reason") != "stop":
            continue
        expressions = [(b, standalone_math(b)) for b in region.blocks]
        expressions = [(b, body) for b,body in expressions if body is not None]
        if len(expressions) != 1:
            continue
        crop, body = expressions[0]
        if any(b is not crop and len(b.markdown.strip()) > 8 for b in region.blocks):
            continue
        if output_quality_warnings(body):
            continue
        key = math_key(body)
        if len(key) < 8:
            continue
        confirming = next((peer for peer in peers
            if peer.source_pages == region.source_pages and peer.mode != "region_detail"
            and sum(corroborates(body,b.markdown) for b in peer.blocks) == 1),None)
        if confirming is None:
            warnings.append("visual_region_ocr_unresolved")
            continue
        matches = [(i,b) for i,b in enumerate(blocks) if b.bbox and b.kind in FORMULA_KINDS
                   and bbox_iou(b.bbox,target) >= .8]
        if len(matches) != 1:
            continue
        index, block = matches[0]
        if math_key(block.markdown) == key:
            continue
        replacement = deepcopy(block)
        replacement.markdown = "\\[\n" + body + "\n\\]"
        if math_syntax_errors(replacement.markdown):
            continue
        replacement.confidence = crop.confidence
        replacement.metadata.pop("uncertain_spans",None)
        action = {"primary_observation":primary.id,"recovery_observation":region.id,
                  "confirming_observation":confirming.id,"action":"selected_region_consensus",
                  "block":index,"target_bbox":list(target)}
        replacement.provenance.append({"observation":region.id,**action})
        blocks[index] = replacement
        provenance.append(action)
    return blocks,provenance,warnings
