"""Serialize final transcription findings, independently of recovery policy."""
from collections import defaultdict
from pathlib import Path

from PIL import Image

from .regions import audit_regions, public_audit, source_inventory, valid_box
from .util import atomic_json, atomic_text


def write_review(bundle, pages, source_pages):
    report = {"schema_version": 1, "status": "checked", "pages": []}
    directory = Path(bundle) / "review"
    directory.mkdir(exist_ok=True)
    lines = ["# Transcription review", "",
             "Findings indicate uncertainty, not certified mathematical errors.",
             "Raster-only associations do not establish transcription accuracy.", ""]
    sources = {p.number: p for p in source_pages}
    moved = defaultdict(list)
    for page in pages:
        for block in page.blocks:
            for number in set(block.source_pages) - {page.number}:
                moved[number].append(block)
    for page in pages:
        source = sources[page.number]
        # Decode once for both raster inventory and all finding crops.
        with Image.open(source.image_path) as image:
            inventory = source_inventory(page.number, page.embedded, image)
            blocks = [*page.blocks, *moved[page.number]]
            audit = public_audit(audit_regions(inventory, blocks, page.embedded))
            attempts = page.visual.get("region_review", {}).get("attempts", [])
            report["pages"].append({"page": page.number, **audit, "attempt_history": attempts})
            if not audit["findings"]:
                continue
            report["status"] = "needs_review"
            lines.extend([f"## Page {page.number}", ""])
            w, h = image.size
            for i, finding in enumerate(audit["findings"]):
                crop_name = None
                if valid_box(finding.get("bbox")):
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
