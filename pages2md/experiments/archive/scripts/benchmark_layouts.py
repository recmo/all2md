"""Deterministic raster layout controls; never modifies corpus inputs.

Uses the production blank-page gate, Base/Detail calls, and multi-page splitter.
Synthetic probes expose gross omissions, not general transcription accuracy.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

from PIL import Image, ImageDraw, ImageFont

from benchmark_decoding import metrics
from pages2md.constants import MODEL_REVISION
from pages2md.model import EmbeddedEvidence
from pages2md.ocr import MlxUnlimitedOcr, split_multi_page_output
from pages2md.pipeline import _is_visually_blank
from pages2md.util import atomic_json, atomic_text, sha256_file


def fixtures(output, font_path):
    font = ImageFont.truetype(str(font_path), 36)
    controls = {}
    for name in ("blank", "offwhite", "figure", "table-first", "sparse", "repeated-prose", "columns"):
        image = Image.new("RGB", (1600, 2200), (250, 250, 250) if name == "offwhite" else "white")
        draw = ImageDraw.Draw(image)
        probes = []
        if name == "figure":
            draw.rectangle((180, 250, 1420, 1800), outline="black", width=6)
            for x, y, color in [(300, 500, "red"), (650, 800, "blue"), (1000, 1100, "green")]:
                draw.ellipse((x, y, x + 230, y + 230), fill=color)
        elif name == "table-first":
            rows = [["Trial", "Status", "Result"], *[[str(i), "Repeated", "Accepted"] for i in range(1, 6)]]
            for row, values in enumerate(rows):
                for col, text in enumerate(values):
                    x, y = 160 + col * 420, 140 + row * 130
                    draw.rectangle((x, y, x + 420, y + 130), outline="black", width=3)
                    draw.text((x + 20, y + 40), text, font=font, fill="black")
            draw.text((160, 1030), "All five trials used the same settings.", font=font, fill="black")
            probes = ["Trial", "Status", "Result", "All five trials used the same settings"]
        elif name == "sparse":
            draw.text((180, 170), "Appendix", font=font, fill="black")
            draw.text((180, 250), "End of document.", font=font, fill="black")
            draw.text((780, 2090), "42", font=font, fill="black")
            probes = ["Appendix", "End of document"]
        elif name == "repeated-prose":
            paragraph = "The same measurement is repeated for each independent trial.\nEach record is retained separately for later comparison.\nRepeated observations are legitimate source content."
            for y in (180, 1050):
                draw.multiline_text((160, y), paragraph, font=font, fill="black", spacing=20)
            probes = ["same measurement", "Each record is retained separately", "legitimate source content"]
        elif name == "columns":
            for x, title, lines in [(100, "Left column", ["The first experiment measures", "agreement across observations.", "Its complete record ends here."]),
                                    (850, "Right column", ["The second experiment checks", "independent measurements.", "Its final conclusion is retained."])]:
                draw.multiline_text((x, 160), title + "\n\n" + "\n".join(lines), font=font, fill="black", spacing=20)
            probes = ["Left column", "complete record ends here", "Right column", "final conclusion is retained"]
        path = output / f"{name}.png"
        image.save(path)
        controls[name] = (path, probes)
    return controls


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "src" / "pages2md"
    manifest = {"model_revision": MODEL_REVISION, "max_tokens": 4096,
                "font_sha256": sha256_file(args.font), "harness_sha256": sha256_file(Path(__file__)),
                "code": {name: sha256_file(source / name) for name in ("ocr.py", "decoding.py", "block_decoding.py", "pipeline.py")}}
    if (output / "manifest.json").exists() and json.loads((output / "manifest.json").read_text()) != manifest:
        raise ValueError("choose a fresh output directory after changing code")
    atomic_json(output / "manifest.json", manifest)
    atomic_text(output / "harness.py", Path(__file__).read_text())
    for name in manifest["code"]:
        atomic_text(output / "code" / name, (source / name).read_text())
    controls = fixtures(output, args.font)
    backend = MlxUnlimitedOcr(max_tokens=4096)
    results = []
    for case, (image, anchors) in controls.items():
        blank, ink = _is_visually_blank(image)
        if blank:
            result = {"case": case, "blank_gate": True, "ink_fraction": ink, "model_calls": 0}
            results.append(result)
            print(json.dumps(result), flush=True)
            continue
        for mode in ("base", "detail"):
            for policy in ("previous", "blocks"):
                key = f"{case}-{mode}-{policy}"
                path = output / f"{key}.json"
                if path.exists():
                    result = json.loads(path.read_text())
                    if result["raw_sha256"] != sha256_file(output / f"{key}.txt"):
                        raise ValueError("raw output no longer matches its recorded hash")
                    results.append(result)
                    continue
                backend._initial_grounding = False
                backend._block_decoding = policy == "blocks"
                backend._startup_recovery = policy == "blocks"
                start = perf_counter()
                raw, generation = backend.recognize_pages([image]) if mode == "base" else backend.recognize_detail(image)
                generation.pop("_confidence_spans", None)
                atomic_text(output / f"{key}.txt", raw)
                result = {"case": case, "mode": mode, "policy": policy, "seconds": round(perf_counter()-start, 3),
                          "ink_fraction": ink, "image_sha256": sha256_file(image),
                          "raw_sha256": sha256_file(output / f"{key}.txt"), "generation": generation,
                          "metrics": metrics(raw, generation, EmbeddedEvidence(), anchors)}
                atomic_json(path, result)
                results.append(result)
                atomic_json(output / "results.json", results)
                print(json.dumps({k:v for k,v in result.items() if k != "generation"}), flush=True)
    images = [controls["columns"][0], controls["table-first"][0]]
    for policy in ("previous", "blocks"):
        key = f"multi-{policy}"
        path = output / f"{key}.json"
        if path.exists():
            result = json.loads(path.read_text())
            if result["raw_sha256"] != sha256_file(output / f"{key}.txt"):
                raise ValueError("multi-page raw output changed")
            results.append(result)
            continue
        backend._initial_grounding = False
        backend._block_decoding = backend._startup_recovery = policy == "blocks"
        raw, generation = backend.recognize_pages(images)
        generation.pop("_confidence_spans", None)
        segments = split_multi_page_output(raw, 2)
        atomic_text(output / f"{key}.txt", raw)
        result = {"case": "multi", "policy": policy, "segments": len(segments),
                  "raw_sha256": sha256_file(output / f"{key}.txt"), "generation": generation,
                  "pages": [metrics(text, generation, EmbeddedEvidence(), controls[name][1])
                            for text, name in zip(segments, ["columns", "table-first"])]}
        atomic_json(path, result)
        results.append(result)
        print(json.dumps({k:v for k,v in result.items() if k != "generation"}), flush=True)
    atomic_json(output / "results.json", results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font", type=Path, default=Path("/System/Library/Fonts/Helvetica.ttc"))
    run(parser.parse_args())
