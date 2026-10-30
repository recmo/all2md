"""Read-only replay of saved page observations; never invokes the OCR model.

Usage: PYTHONPATH=pages2md/src python pages2md/scripts/replay_decoding.py \
    /path/to/paper.pages2md --pages 1 5

Embedded agreement is an alignment diagnostic, NOT independent accuracy ground
truth. Inspect the source rendering before accepting an apparent improvement.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

from pages2md.decoding import SourceProgress, structural_loop
from pages2md.model import EmbeddedEvidence
from pages2md.native import parse_native_observation, reconcile_observations
from pages2md.pipeline import _cached_observation, _restore_cached_block_details
from pages2md.quality import output_quality_warnings


def replay(bundle: Path, number: int) -> dict:
    started = perf_counter()
    page = json.loads((bundle / "pages" / f"page-{number:04d}.json").read_text())
    evidence = EmbeddedEvidence(**{k: v for k, v in page["embedded"].items() if k != "links"})
    visual = page["visual"]
    group = _cached_observation(visual["multi_page"], bundle)
    primary = parse_native_observation(page["raw_ocr"], mode=page["generation"].get("mode", "multi_base"),
                                       source_pages=page["generation"].get("source_pages", [number]),
                                       generation=page["generation"])
    _restore_cached_block_details(primary, group)
    primary.id = group.id
    recoveries = [_cached_observation(value, bundle) for value in visual.get("candidates", [])]
    blocks, actions, warnings = reconcile_observations(primary, recoveries, embedded=evidence, embedded_text=evidence.text)
    text = "\n\n".join(block.markdown for block in blocks)
    return {"bundle": str(bundle), "page": number, "old_characters": len(page["visual_markdown"]),
            "replayed_characters": len(text), "replayed_blocks": len(blocks),
            "source_regions": len(SourceProgress([evidence]).regions),
            "actions": actions, "warnings": warnings, "remaining_quality": output_quality_warnings(text),
            "attempts": [{"mode": o.mode, "structural_loop": structural_loop(o.raw),
                          "tokens": o.generation.get("generation_tokens")} for o in [primary, *recoveries]],
            "seconds": round(perf_counter() - started, 3)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--pages", type=int, nargs="+", required=True)
    args = parser.parse_args()
    for number in args.pages:
        print(json.dumps(replay(args.bundle, number), ensure_ascii=False), flush=True)
