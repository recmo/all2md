"""Bounded, loss-averse regional recovery with persistent raw observations.

Embedded glyphs are comparison evidence, never a substitute transcription.
Raster associations alone do not authorize mathematical replacements.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from PIL import Image

from .embedded import bbox_coverage
from .native import parse_native_observation
from .quality import output_quality_warnings, MAX_PAGE_CHARACTERS
from .regions import (alignment_cache, audit_regions, preserves_coverage, public_audit,
                      source_inventory, valid_box)
from .util import atomic_json, atomic_text


def _score(audit):
    return (sum(f["severity"] == "error" for f in audit["findings"]),
            len(audit["findings"]), -len(audit["matched_glyphs"]))


def _insertion_index(blocks, box):
    """Only insert in a uniquely bracketed gap in the existing column order."""
    gaps = []
    for index in range(1, len(blocks)):
        before, after = blocks[index - 1].bbox, blocks[index].bbox
        if not valid_box(before) or not valid_box(after):
            continue
        if (before[3] <= box[1] < box[3] <= after[1]
                and max(before[0], after[0]) <= box[0]
                and box[2] <= min(before[2], after[2])):
            gaps.append(index)
    return gaps[0] if len(gaps) == 1 else None


def select_candidates(blocks, candidates, inventory, embedded, *, align=None):
    """Try local replacements and insertions; never replace an entire page."""
    blocks = deepcopy(blocks)
    if align is None:
        align = alignment_cache(embedded)
    audit = audit_regions(inventory, blocks, embedded, align=align)
    attempts = []
    if not audit["findings"]:
        return blocks, audit, attempts
    for candidate in candidates:
        quality = (["visual_implausible_output_length"]
                   if len(candidate.raw) > MAX_PAGE_CHARACTERS * max(1, len(candidate.source_pages))
                   else output_quality_warnings(candidate.raw))
        if any(w in quality for w in
               ("visual_math_repetition", "visual_implausible_output_length")):
            attempts.append({"observation": candidate.id, "accepted": False,
                             "reason": "runaway_candidate"})
            continue
        for block in candidate.blocks:
            if not valid_box(block.bbox) or not block.markdown.strip():
                continue
            overlapping = [i for i, old in enumerate(blocks) if old.bbox and
                           bbox_coverage(old.bbox, block.bbox) >= .7]
            # Merging blocks loses the location of errors and may swallow a
            # column or a previously correct formula. Require a local edit.
            if len(overlapping) > 1:
                attempts.append({"observation": candidate.id, "accepted": False,
                                 "reason": "ambiguous_multi_block_replacement"})
                continue
            if any(old.bbox and i not in overlapping
                   and bbox_coverage(block.bbox, old.bbox) > 0
                   for i, old in enumerate(blocks)):
                attempts.append({"observation": candidate.id, "accepted": False,
                                 "reason": "ambiguous_source_overlap"})
                continue
            local = audit_regions(inventory, [block], embedded, align=align)
            glyphs = set(map(tuple, local["matched_glyphs"]))
            if any(glyphs.intersection(map(tuple, ids)) for i, ids in
                   enumerate(audit["block_glyphs"]) if i not in overlapping):
                attempts.append({"observation": candidate.id, "accepted": False,
                                 "reason": "source_already_owned"})
                continue
            index = overlapping[0] if overlapping else _insertion_index(blocks, block.bbox)
            if index is None:
                attempts.append({"observation": candidate.id, "accepted": False,
                                 "reason": "ambiguous_reading_order"})
                continue
            proposal = deepcopy(blocks)
            replacement = deepcopy(block)
            replacement.provenance.append({"kind": "region_recovery", "observation": candidate.id})
            proposal[index:index + bool(overlapping)] = [replacement]
            after = audit_regions(inventory, proposal, embedded, align=align)
            # A lower warning count alone is insufficient: require real source
            # support, no new findings, and no loss of matched glyph identities.
            # Unchanged blocks retain their findings; changed content must be
            # locally clean, not merely trade errors of the same global kind.
            clean_replacement = not any("block" in f for f in local["findings"])
            before_regions = {(f["kind"], f["region"]) for f in audit["findings"]
                              if "region" in f}
            accepted = (preserves_coverage(audit, after)
                        and len(after["matched_glyphs"]) > len(audit["matched_glyphs"])
                        and _score(after) < _score(audit)
                        and clean_replacement
                        and not {(f["kind"], f["region"]) for f in after["findings"]
                                 if "region" in f} - before_regions)
            attempts.append({"observation": candidate.id, "bbox": block.bbox,
                             "accepted": accepted, "reason": "coverage_improved" if accepted
                             else "no_safe_coverage_improvement"})
            if accepted:
                blocks, audit = proposal, after
    return blocks, audit, attempts


def recover_regions(source_page, blocks, candidates, *, backend=None, bundle=None, budget=4):
    inventory = source_inventory(source_page.number, source_page.embedded, source_page.image_path)
    align = alignment_cache(source_page.embedded)
    blocks, audit, attempts = select_candidates(blocks, candidates, inventory, source_page.embedded,
                                                align=align)
    recognize = getattr(backend, "recognize_detail", None)
    if bundle is not None:
        cache = Path(bundle) / "region-observations"
        cache.mkdir(exist_ok=True)
        identity = dict(backend.identity) if backend is not None else None
        source_hash = hashlib.sha256(source_page.image_path.read_bytes()).hexdigest()
        # Every run tries saved crops first, including explicitly requested
        # fresh recovery. Neither success nor failure discards raw evidence.
        for path in sorted(cache.glob("*.json")):
            try:
                value = json.loads(path.read_text())
                if value.get("source_hash") != source_hash:
                    continue
                if "raw" in value:
                    blocks, audit, tried = select_candidates(
                        blocks, [_crop_observation(value)], inventory, source_page.embedded,
                        align=align)
                    attempts.extend({**item, "cache": str(path.relative_to(bundle))} for item in tried)
                else:
                    attempts.append({"cache": str(path.relative_to(bundle)), "accepted": False,
                                     "error": value.get("error", "invalid_cache")})
            except (ValueError, TypeError, KeyError):
                attempts.append({"cache": str(path.relative_to(bundle)), "accepted": False,
                                 "error": "invalid_cache"})
        targets = []
        for finding in audit["findings"]:
            if finding["kind"] == "uncovered_ink":
                continue
            box = finding.get("bbox")
            if valid_box(box) and not any(bbox_coverage(box, b) > .7 for b in targets):
                targets.append(tuple(box))
        calls = 0
        for box in targets:
            for scale in (1, 2):
                if calls >= budget:
                    break
                padded = (max(0, box[0] - 12 * scale), max(0, box[1] - 8 * scale),
                          min(1000, box[2] + 12 * scale), min(1000, box[3] + 8 * scale))
                # Cache survives assembly-code changes. Replay does not need the
                # backend identity: persisted observations are tried separately.
                key = hashlib.sha256(json.dumps(
                    [1, source_hash, identity, padded, scale], sort_keys=True).encode()).hexdigest()
                path = cache / f"{key}.json"
                if path.exists():
                    # All saved responses were considered above; don't replay
                    # one twice or overwrite corrupt/failed evidence.
                    continue
                elif callable(recognize):
                    calls += 1
                    crop_path = cache / f"{key}.png"
                    with Image.open(source_page.image_path) as image:
                        w, h = image.size
                        crop = image.crop((int(padded[0]*w/1000), int(padded[1]*h/1000),
                                           int(padded[2]*w/1000), int(padded[3]*h/1000)))
                        if scale != 1:
                            crop = crop.resize((crop.width * scale, crop.height * scale))
                        crop.save(crop_path)
                    try:
                        raw, generation = recognize(crop_path)
                        value = {"raw": raw, "generation": dict(generation), "bbox": padded,
                                 "page": source_page.number, "source_hash": source_hash}
                    except Exception as error:
                        value = {"error": type(error).__name__, "page": source_page.number,
                                 "source_hash": source_hash}
                    atomic_json(path, value)
                else:
                    continue
                if "raw" in value:
                    observation = _crop_observation(value)
                    blocks, audit, tried = select_candidates(
                        blocks, [observation], inventory, source_page.embedded, align=align)
                    attempts.extend({**item, "cache": str(path.relative_to(bundle))} for item in tried)
                else:
                    attempts.append({"cache": path.name, "accepted": False, "error": value["error"]})
    return blocks, {"stage": "region_recovery", "audit": public_audit(audit), "attempts": attempts}


def _crop_observation(value):
    if not valid_box(value["bbox"]) or not isinstance(value["raw"], str):
        raise ValueError("invalid crop observation")
    observation = parse_native_observation(value["raw"], mode="region_detail",
                                          source_pages=[value["page"]],
                                          generation=value["generation"])
    x0, y0, x1, y1 = value["bbox"]
    observation.id += "-" + hashlib.sha256(json.dumps(value["bbox"]).encode()).hexdigest()[:8]
    for block in observation.blocks:
        if valid_box(block.bbox):
            a, b, c, d = block.bbox
            block.bbox = (x0+a*(x1-x0)/1000, y0+b*(y1-y0)/1000,
                          x0+c*(x1-x0)/1000, y0+d*(y1-y0)/1000)
        else:
            block.bbox = None
    return observation


def write_review(bundle, pages, source_pages):
    """Separate final findings from historical failed/rejected OCR attempts."""
    report = {"schema_version": 1, "status": "checked", "pages": []}
    directory = Path(bundle) / "review"
    directory.mkdir(exist_ok=True)
    lines = ["# Transcription review", "",
             "Findings indicate uncertainty, not certified mathematical errors.",
             "Raster-only associations do not establish transcription accuracy.", ""]
    sources = {p.number: p for p in source_pages}
    for page in pages:
        source = sources[page.number]
        inventory = source_inventory(page.number, page.embedded, source.image_path)
        # Content may have moved to an adjacent page during document assembly.
        blocks = list(page.blocks)
        for other in pages:
            if other.number != page.number:
                blocks.extend(b for b in other.blocks if page.number in b.source_pages)
        audit = public_audit(audit_regions(inventory, blocks, page.embedded))
        attempts = page.visual.get("region_review", {}).get("attempts", [])
        entry = {"page": page.number, **audit, "attempt_history": attempts}
        report["pages"].append(entry)
        if audit["findings"]:
            report["status"] = "needs_review"
            lines.extend([f"## Page {page.number}", ""])
            for i, finding in enumerate(audit["findings"]):
                crop_name = None
                if valid_box(finding.get("bbox")):
                    with Image.open(source.image_path) as image:
                        w, h = image.size
                        a, b, c, d = finding["bbox"]
                        crop = image.crop((max(0, int(a*w/1000)-8), max(0, int(b*h/1000)-8),
                                           min(w, int(c*w/1000)+8), min(h, int(d*h/1000)+8)))
                        crop_name = f"page-{page.number:04d}-{i:03d}.png"
                        crop.save(directory / crop_name)
                    finding["source_crop"] = f"review/{crop_name}"
                lines.append(f"- {finding['kind']}" + (f" — [source crop]({crop_name})" if crop_name else ""))
            lines.append("")
    atomic_text(directory / "index.md", "\n".join(lines) + "\n")
    atomic_json(Path(bundle) / "review.json", report)
    return {"status": report["status"], "report": "review.json",
            "finding_count": sum(len(p["findings"]) for p in report["pages"])}
