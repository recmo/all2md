"""Bounded, loss-averse regional recovery with persistent raw observations.

Embedded glyphs are comparison evidence, never a substitute transcription.
Raster associations alone do not authorize mathematical replacements.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace

from .embedded import bbox_coverage
from .crop_store import CropStore
from .model import RecoveryAttempt
from .quality import candidate_rejection
from .regions import PageAuditor, preserves_coverage, public_audit, source_inventory, valid_box


def _score(audit):
    return (sum(f.severity == "error" for f in audit.findings),
            len(audit.findings), -len(audit.matched_glyphs))


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


def select_candidates(blocks, candidates, inventory, embedded, *, align=None, auditor=None):
    """Try local replacements and insertions; never replace an entire page."""
    blocks = deepcopy(blocks)
    auditor = auditor or PageAuditor(inventory, embedded, align=align)
    analyses = [auditor.analyze(block) for block in blocks]
    audit = auditor.audit(analyses)
    attempts = []
    if not audit.findings:
        return blocks, audit, attempts
    for candidate in candidates:
        if candidate_rejection(candidate):
            attempts.append(RecoveryAttempt("runaway_candidate", observation=candidate.id))
            continue
        for block in candidate.blocks:
            if not valid_box(block.bbox) or not block.markdown.strip():
                continue
            overlapping = [i for i, old in enumerate(blocks) if old.bbox and
                           bbox_coverage(old.bbox, block.bbox) >= .7]
            # Merging blocks loses the location of errors and may swallow a
            # column or a previously correct formula. Require a local edit.
            if len(overlapping) > 1:
                attempts.append(RecoveryAttempt("ambiguous_multi_block_replacement",
                                                observation=candidate.id, bbox=block.bbox))
                continue
            if any(old.bbox and i not in overlapping
                   and bbox_coverage(block.bbox, old.bbox) > 0
                   for i, old in enumerate(blocks)):
                attempts.append(RecoveryAttempt("ambiguous_source_overlap",
                                                observation=candidate.id, bbox=block.bbox))
                continue
            local = auditor.analyze(block)
            glyphs = local.matched_glyphs
            if any(glyphs & analysis.matched_glyphs for i, analysis in
                   enumerate(analyses) if i not in overlapping):
                attempts.append(RecoveryAttempt("source_already_owned",
                                                observation=candidate.id, bbox=block.bbox))
                continue
            index = overlapping[0] if overlapping else _insertion_index(blocks, block.bbox)
            if index is None:
                attempts.append(RecoveryAttempt("ambiguous_reading_order",
                                                observation=candidate.id, bbox=block.bbox))
                continue
            proposal = list(blocks)
            proposed_analyses = list(analyses)
            replacement = deepcopy(block)
            replacement.provenance.append({"kind": "region_recovery", "observation": candidate.id})
            if overlapping:
                proposal[index] = replacement
                proposed_analyses[index] = local
            else:
                proposal.insert(index, replacement)
                proposed_analyses.insert(index, local)
            after = auditor.audit(proposed_analyses)
            # A lower warning count alone is insufficient: require real source
            # support, no new findings, and no loss of matched glyph identities.
            # Unchanged blocks retain their findings; changed content must be
            # locally clean, not merely trade errors of the same global kind.
            clean_replacement = not local.findings
            before_regions = {(f.kind, f.region) for f in audit.findings if f.region is not None}
            after_regions = {(f.kind, f.region) for f in after.findings if f.region is not None}
            accepted = (preserves_coverage(audit, after)
                        and len(after.matched_glyphs) > len(audit.matched_glyphs)
                        and _score(after) < _score(audit)
                        and clean_replacement
                        and not after_regions - before_regions)
            attempts.append(RecoveryAttempt(
                "coverage_improved" if accepted else "no_safe_coverage_improvement",
                accepted=accepted, observation=candidate.id, bbox=block.bbox))
            if accepted:
                blocks, analyses, audit = proposal, proposed_analyses, after
    return blocks, audit, attempts


def _crop_targets(audit):
    targets = []
    for finding in audit.findings:
        if finding.kind == "uncovered_ink":
            continue
        box = finding.bbox
        if valid_box(box) and not any(bbox_coverage(box, b) > .7 for b in targets):
            targets.append(tuple(box))
    for box in targets:
        for scale in (1, 2):
            yield (max(0, box[0] - 12 * scale), max(0, box[1] - 8 * scale),
                   min(1000, box[2] + 12 * scale), min(1000, box[3] + 8 * scale)), scale


def recover_regions(source_page, blocks, candidates, *, backend=None, bundle=None,
                    budget=4, crop_store=None):
    inventory = source_inventory(source_page.number, source_page.embedded, source_page.image_path)
    auditor = PageAuditor(inventory, source_page.embedded)
    blocks, audit, attempts = select_candidates(
        blocks, candidates, inventory, source_page.embedded, auditor=auditor)
    store = crop_store or (CropStore(bundle) if bundle is not None else None)
    if store is not None:
        source_hash = hashlib.sha256(source_page.image_path.read_bytes()).hexdigest()

        def evaluate(observation, cache_path, failure):
            nonlocal blocks, audit
            if failure is not None:
                attempts.append(failure)
                return
            blocks, audit, tried = select_candidates(
                blocks, [observation], inventory, source_page.embedded, auditor=auditor)
            attempts.extend(replace(item, cache=cache_path) for item in tried)

        for entry in store.replay(source_hash):
            evaluate(*entry)
        if callable(getattr(backend, "recognize_detail", None)):
            calls = 0
            for box, scale in _crop_targets(audit):
                if calls >= budget:
                    break
                entry = store.request(source_page, source_hash, box, scale, backend)
                if entry is not None:
                    calls += 1
                    evaluate(*entry)
    return blocks, {"stage": "region_recovery", "audit": public_audit(audit),
                    "attempts": [a.to_dict() for a in attempts]}
