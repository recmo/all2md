"""Shadow-only prefix experiment: can geometry identify the next math glyph?

Never changes OCR. Uses prefix-only alignment, never the full formula to choose
a frontier; completed OCR is only a comparator, not a truth label.
"""
from pathlib import Path
from collections import Counter
import json
import fitz
from pages2md.adapters import _raw_text_blocks
from pages2md.alignment import align_glyphs
from pages2md.model import EmbeddedEvidence
from pages2md.native import parse_native_observation
from pages2md.util import atomic_json


def run(root):
    counts = Counter()
    examples = []
    for record in json.loads((root / "results.json").read_text()):
        with fitz.open(record["source"]) as doc:
            for entry in record["pages"]:
                number = entry["page"]
                page = doc[number - 1]
                evidence = EmbeddedEvidence(text=page.get_text(), blocks=_raw_text_blocks(page), extractor="pymupdf")
                raw = (root / Path(record["source"]).stem / f"detail-{number}.txt").read_text()
                observation = parse_native_observation(raw, mode="gundam_detail", source_pages=[number])
                for block in observation.blocks:
                    if block.kind != "formula" or not block.bbox:
                        continue
                    counts["formulas"] += 1
                    for stop in range(16, len(block.markdown), 16):
                        counts["prefixes"] += 1
                        aligned = align_glyphs(block.markdown[:stop], evidence, block.bbox)
                        last = len(aligned.text) - 1
                        frontier = [aligned.matches.get(i) for i in range(last - 5, last + 1)]
                        if None in frontier or not frontier:
                            counts["abstain_unaligned"] += 1
                            continue
                        if frontier != list(range(frontier[0], frontier[0] + 6)):
                            counts["abstain_noncontiguous"] += 1
                            continue
                        anchor = aligned.text[-6:]
                        if aligned.native.count(anchor) != 1:
                            counts["abstain_ambiguous"] += 1
                            continue
                        next_index = frontier[-1] + 1
                        if next_index >= len(aligned.native):
                            counts["abstain_end"] += 1
                            continue
                        counts["eligible_frontier"] += 1
                        # A glyph is not a token: record parent relation but do
                        # not translate it to a forced TeX serialization.
                        examples.append({"source": record["source"], "page": number,
                                         "prefix": block.markdown[:stop],
                                         "next_glyph": aligned.native[next_index],
                                         "parent": aligned.parents.get(next_index),
                                         "ocr_suffix": block.markdown[stop:stop + 24]})
    atomic_json(root / "math-frontiers.json", {"counts": counts, "examples": examples,
                "policy": "shadow-only; no model steering and no accuracy claim"})
    print(dict(counts))


if __name__ == "__main__":
    import sys
    run(Path(sys.argv[1]))
