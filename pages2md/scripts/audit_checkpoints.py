"""Read-only corpus audit; no OCR calls or writes to existing bundles.

Run with pages2md's Python environment:
  python scripts/audit_checkpoints.py SOURCE.pages2md --output /tmp/audit.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from pages2md.model import Block, EmbeddedEvidence
from pages2md.regions import audit_regions, public_audit, source_inventory
from pages2md.util import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundles", type=Path, nargs="+")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if any(output.is_relative_to(p.resolve()) for p in args.bundles):
        parser.error("--output must be outside the input bundles")
    reports = []
    for bundle in args.bundles:
        for path in sorted((bundle / "pages").glob("page-*.json")):
            value = json.loads(path.read_text())
            native = value.get("embedded", {})
            embedded = EmbeddedEvidence(text=native.get("text", ""),
                                        blocks=native.get("blocks", []),
                                        extractor=native.get("extractor"))
            blocks = [Block(**block) for block in value["blocks"]]
            audit = public_audit(audit_regions(
                source_inventory(value["number"], embedded), blocks, embedded))
            reports.append({"bundle": str(bundle), "page": value["number"], **audit})
    counts = Counter(f["kind"] for p in reports for f in p["findings"])
    atomic_json(output, {"pages": reports, "finding_counts": dict(counts),
                         "scope": "cached block/native-glyph audit; no raster or OCR calls"})
    print(json.dumps({"pages": len(reports), "finding_counts": dict(counts),
                      "output": str(output)}))


if __name__ == "__main__":
    main()
