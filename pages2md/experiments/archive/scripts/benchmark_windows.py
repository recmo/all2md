"""Read-only corpus window canary; retain images, raw output and diagnostics."""
from pathlib import Path
import argparse
import time
from types import SimpleNamespace
import fitz

from benchmark_decoding import CASES, metrics
from pages2md.adapters import _raw_text_blocks
from pages2md.model import EmbeddedEvidence
from pages2md.ocr import MlxUnlimitedOcr, split_multi_page_output
from pages2md.native import parse_native_observation, reconcile_observations
from pages2md.pipeline import _has_embedded_coverage_gap, _align_multi_results
from pages2md.util import atomic_json, atomic_text, sha256_file


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[1] / "src/pages2md"
    for name in ("ocr.py", "decoding.py", "block_decoding.py", "pipeline.py", "native.py"):
        atomic_text(output / "code" / name, (source / name).read_text())
    backend = MlxUnlimitedOcr()
    results = []
    for stem, numbers in [("BCIKS20", [60, 61]), ("BCGM25", [44, 45]), ("GaoKL24", [10, 11])]:
        pdf = Path("/Users/remco/Documents/Better.codes") / f"{stem}.pdf"
        folder = output / stem
        folder.mkdir()
        images, evidence = [], []
        with fitz.open(pdf) as doc:
            for number in numbers:
                page = doc[number - 1]
                image = folder / f"page-{number}.png"
                page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(image)
                images.append(image)
                evidence.append(EmbeddedEvidence(text=page.get_text("text", sort=True), blocks=_raw_text_blocks(page), extractor="pymupdf"))
        started = time.perf_counter()
        raw, generation = backend.recognize_pages(images, embedded=evidence)
        atomic_text(folder / "group.txt", raw)
        atomic_json(folder / "group.json", generation)
        segments = split_multi_page_output(raw, len(numbers))
        aligned = _align_multi_results(
            [SimpleNamespace(number=n, embedded=e) for n, e in zip(numbers, evidence)],
            [(part, generation) for part in segments],
        )
        record = {"source": str(pdf), "sha256": sha256_file(pdf), "seconds": time.perf_counter() - started,
                  "segments": len(segments), "pages": []}
        for number, image, native, (segment, segment_gen) in zip(numbers, images, evidence, aligned):
            anchors = CASES.get(f"{stem}-{number}", (None, None, []))[2]
            primary = parse_native_observation(segment, mode="multi_base", source_pages=segment_gen["source_pages"], generation=segment_gen)
            detail_raw, detail_gen = backend.recognize_detail(image, embedded=native)
            detail_gen["target_block_indices"] = list(range(len(primary.blocks)))
            atomic_text(folder / f"detail-{number}.txt", detail_raw)
            atomic_json(folder / f"detail-{number}.json", detail_gen)
            detail = parse_native_observation(detail_raw, mode="gundam_detail", source_pages=[number], generation=detail_gen)
            blocks, provenance, warnings = reconcile_observations(primary, [detail], embedded=native)
            canonical = "\n\n".join(b.markdown for b in blocks)
            atomic_text(folder / f"canonical-{number}.md", canonical)
            entry = {"page": number, "base": metrics(segment, generation, native, anchors),
                     "detail": metrics(detail_raw, detail_gen, native, anchors),
                     "canonical": metrics(canonical, {}, native, anchors),
                     "base_gap": _has_embedded_coverage_gap(primary.blocks, native),
                     "canonical_gap": _has_embedded_coverage_gap(blocks, native),
                     "provenance": provenance, "warnings": warnings}
            record["pages"].append(entry)
            print(f"{stem}-{number}: base anchors {entry['base']['anchor_recall']}, canonical {entry['canonical']['anchor_recall']}, gap {entry['canonical_gap']}", flush=True)
        results.append(record)
        atomic_json(output / "results.json", results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
