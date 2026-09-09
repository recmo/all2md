"""Replay retained windows through production alignment and candidate recovery."""
from pathlib import Path
from types import SimpleNamespace
import json
import fitz
from benchmark_decoding import CASES, metrics
from pages2md.adapters import _raw_text_blocks
from pages2md.model import EmbeddedEvidence
from pages2md.native import parse_native_observation, reconcile_observations
from pages2md.ocr import MlxUnlimitedOcr, split_multi_page_output
from pages2md.pipeline import _align_multi_results, _collect_page_candidates, _has_embedded_coverage_gap
from pages2md.util import atomic_json, atomic_text


def run(root, output):
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[1] / "src/pages2md"
    for name in ("ocr.py", "decoding.py", "block_decoding.py", "pipeline.py", "native.py"):
        atomic_text(output / "code" / name, (source / name).read_text())
    backend = MlxUnlimitedOcr()
    results = []
    for record in json.loads((root / "results.json").read_text()):
        folder = root / Path(record["source"]).stem
        pages = []
        with fitz.open(record["source"]) as doc:
            for image in sorted(folder.glob("page-*.png")):
                number = int(image.stem.split("-")[-1])
                p = doc[number - 1]
                pages.append(SimpleNamespace(number=number, image_path=image,
                    embedded=EmbeddedEvidence(text=p.get_text("text", sort=True), blocks=_raw_text_blocks(p), extractor="pymupdf")))
        raw = (folder / "group.txt").read_text()
        generation = json.loads((folder / "group.json").read_text())
        group = parse_native_observation(raw, mode="multi_base", source_pages=[p.number for p in pages], generation=generation)
        aligned = _align_multi_results(pages, [(s, generation) for s in split_multi_page_output(raw, len(pages))])
        class CachedDetail:
            supports_embedded_guidance = True
            recognize_pages = backend.recognize_pages
            def recognize_detail(self, image, *, embedded=None):
                number = int(image.stem.split("-")[-1])
                cached = folder / f"detail-{number}.txt"
                if cached.exists():
                    return cached.read_text(), json.loads(cached.with_suffix(".json").read_text())
                return backend.recognize_detail(image, embedded=embedded)
        for page, (segment, gen) in zip(pages, aligned):
            primary = parse_native_observation(segment, mode="multi_base", source_pages=gen["source_pages"], generation=gen)
            candidates, extra = _collect_page_candidates(page, primary, group, CachedDetail(), output)
            blocks, provenance, warnings = reconcile_observations(primary, candidates, embedded=page.embedded)
            text = "\n\n".join(b.markdown for b in blocks)
            case = f"{folder.name}-{page.number}"
            anchors = CASES.get(case, (None, None, []))[2]
            atomic_text(output / f"{case}.md", text)
            result = {"case": case, "gap": _has_embedded_coverage_gap(blocks, page.embedded),
                      "metrics": metrics(text, {}, page.embedded, anchors), "provenance": provenance,
                      "warnings": warnings + extra, "candidates": len(candidates)}
            results.append(result)
            atomic_json(output / "results.json", results)
            print(case, "gap", result["gap"], "anchors", result["metrics"]["anchor_recall"], flush=True)


if __name__ == "__main__":
    import sys
    run(Path(sys.argv[1]), Path(sys.argv[2]))
