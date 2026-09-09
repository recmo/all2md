"""Reproducible hard-page experiments; source PDFs and OCR bundles stay read-only.

Outputs include the exact production-renderer image, unmodified raw generations,
contracts, fingerprints and diagnostics. Native agreement is not ground truth.
The small manually reviewed prose anchors measure gross page coverage only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from time import perf_counter

import fitz

from pages2md.adapters import _raw_text_blocks
from pages2md.constants import MODEL_REVISION, MLX_VLM_REVISION
from pages2md.decoding import structural_loop, words
from pages2md.block_decoding import duplicate_blocks
from pages2md.model import EmbeddedEvidence
from pages2md.native import parse_native_observation
from pages2md.ocr import MlxUnlimitedOcr
from pages2md.util import atomic_json, atomic_text, sha256_file


CASES = {
    "BCIKS20-61": ("BCIKS20", 61, ["For any of the", "Repeating the argument", "From here the rest of the claim",
                                    "Proof of Claim", "By definition", "This completes the proof"]),
    "BCGM25-40": ("BCGM25", 40, ["Polynomial generators have MCA", "Definition", "Theorem", "We proceed in two steps",
                                  "Proof of Theorem", "Applying Lemmas", "Proof of Lemma"]),
    "ABF26-25": ("ABF26", 25, ["Theorem", "Limitations", "Finally we present evidence", "characteristic",
                                "In fact the above holds", "by the Johnson bound", "large compared to"]),
    "Jo26-1": ("Jo26", 1, ["Interleaving Stability", "Sunghyeon Jo", "Abstract", "We prove that row",
                             "the transfer is exact", "We further establish", "smaller interleaved"]),
}


CASES.update({
    "BCGM25-4": ("BCGM25", 4, ["seed", "generator", "mutual correlated agreement"]),
    "Jo26-3": ("Jo26", 3, ["affine", "interleaving", "curve"]),
    "ABF26-34": ("ABF26", 34, ["This instantiation closely resembles", "Knowledge soundness upperbound",
                                  "We consider the above expression", "Fixing", "Argument size when enforcing",
                                  "We detail the result"]),
    "ABF26-35": ("ABF26", 35, ["Round by round knowledge soundness", "Soundness lower bound",
                                  "We lower bound both terms", "List decoding lowerbound", "Additionally"]),
    "ABF26-45": ("ABF26", 45, ["Definition", "Empty transcript", "Prover moves", "Full transcript",
                                  "total extraction time", "Univariate multiplicity codes", "Using the formal derivative"]),
    "Jo26-10": ("Jo26", 10, ["For proof system analyses", "by an exact identity", "The list size term remains",
                                "In the concrete parametrizations", "Polynomial Generators", "We use two closure properties"]),
    "Jo26-11": ("Jo26", 11, ["be a polynomial generator", "Run the proof", "By the tensor product closure lemma",
                                "Corollary", "Thus the same polynomial generator bound", "Reed Solomon polynomial generator bounds"]),
    "Jo26-14": ("Jo26", 14, ["Exact Preservation", "Projection does not increase distance", "At every coordinate",
                                "Exact preservation of curve decodability", "Fix a marked instance", "Scalar closure is identical"]),
    "GaoKL24-5": ("GaoKL24", 5, ["Their analysis is confined", "A previous version", "Applications",
                                   "We introduce the application", "In practical applications", "Preliminaries"]),
    "GaoKL24-14": ("GaoKL24", 14, ["Bad combining over affine spaces", "In real world applications",
                                     "Bad combining points", "We use induction", "According to our assumption", "For the second item"]),
    "BCGM25-23": ("BCGM25", 23, ["Lemma", "Our goal is to bound", "By the law of total probability",
                                    "can be rewritten as follows", "The probability inside the expectation", "which is bounded above"]),
    "BCGM25-45": ("BCGM25", 45, ["so that if there exists", "can naturally be considered", "Further for all", "We compute"]),
    "BCIKS20-57": ("BCIKS20", 57, ["this bound by finding", "The number of variables", "Claim",
                                      "For this computation", "The size of the index set", "where the inequality"]),
    "CS25-21": ("CS25", 21, ["having degree at most", "are independent and uniformly distributed",
                                "Substituting we now have", "We now turn our attention", "For the denominator", "For the numerator"]),
})


@dataclass(frozen=True)
class Variant:
    visual: str
    guidance: bool = False
    native: bool = False
    grounded: bool = False
    blocks: bool = False
    retries: int = 2


VARIANTS = {
    "base-automatic": Variant("base", True, True, blocks=True),
    "detail-automatic": Variant("detail", True, True, blocks=True),
    "base-blocks-guided": Variant("base", True, True, True, True),
    "detail-blocks-guided": Variant("detail", True, True, True, True),
    "base-blocks": Variant("base", grounded=True, blocks=True),
    "base-blocks-no-retry": Variant("base", grounded=True, blocks=True, retries=0),
    "detail-blocks": Variant("detail", grounded=True, blocks=True),
    "base-exact": Variant("base"),
    "base-guided": Variant("base", True, True),
    "base-grounded": Variant("base", grounded=True),
    "base-grounded-guided": Variant("base", True, True, True),
    "detail-exact": Variant("detail"),
    "detail-guided": Variant("detail", True, True),
    "detail-grounded": Variant("detail", grounded=True),
    "detail-grounded-guided": Variant("detail", True, True, True),
    "legacy-detail": Variant("legacy"),
}


def matching_text(text):
    text = re.sub(r"\\[A-Za-z]+", " ", text)
    return " ".join(re.findall(r"[a-z]+", text.casefold()))


def metrics(raw, generation, evidence, anchors):
    observation = parse_native_observation(raw, mode=str(generation.get("mode")), source_pages=[1], generation=generation)
    text = "\n\n".join(b.markdown for b in observation.blocks)
    native, visual = Counter(words(evidence.text)), Counter(words(text))
    shared = sum((native & visual).values())
    normalized = matching_text(text)
    found = [anchor for anchor in anchors if matching_text(anchor) in normalized]
    first = raw.find("<|det|>")
    return {"tokens": generation.get("generation_tokens"), "finish": generation.get("finish_reason"),
            "first_grounding_offset": first, "ungrounded_prefix_characters": first if first >= 0 else len(raw),
            "structural_loop": structural_loop(raw), "blocks": len(observation.blocks),
            "duplicate_blocks": duplicate_blocks(raw),
            "formula_blocks": sum(b.kind == "formula" for b in observation.blocks),
            "anchors_found": found, "anchors_missing": [a for a in anchors if a not in found],
            "anchor_recall": round(len(found) / len(anchors), 4) if anchors else None,
            "native_token_precision": round(shared / max(1, sum(visual.values())), 4),
            "native_token_recall": round(shared / max(1, sum(native.values())), 4),
            "warnings": observation.warnings}


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parents[1] / "src" / "pages2md"
    fingerprint = {"model_revision": MODEL_REVISION, "declared_mlx_vlm_revision": MLX_VLM_REVISION,
                   "dpi": args.dpi, "max_tokens": args.max_tokens,
                   "code": {name: sha256_file(source_root / name) for name in ("ocr.py", "decoding.py", "block_decoding.py", "native.py")},
                   "harness_sha256": sha256_file(Path(__file__))}
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != fingerprint:
        raise ValueError("experiment directory belongs to a different configuration; choose a fresh --output")
    atomic_json(manifest_path, fingerprint)
    atomic_text(output / "harness.py", Path(__file__).read_text())
    for name in fingerprint["code"]:
        atomic_text(output / "code" / name, (source_root / name).read_text())
    backend = MlxUnlimitedOcr(max_tokens=args.max_tokens)
    backend._load()
    results = []
    for case in args.cases:
        if case in CASES:
            name, number, anchors = CASES[case]
        else:
            name, number = case.rsplit("-", 1)
            number, anchors = int(number), []
            if Path(name).name != name or number < 1:
                raise ValueError("case must be a PDF stem and positive physical page number")
        pdf = args.corpus / f"{name}.pdf"
        case_dir = output / case
        case_dir.mkdir(exist_ok=True)
        image = case_dir / f"source-{args.dpi}dpi.png"
        with fitz.open(pdf) as document:
            page = document[number - 1]
            if not image.exists():
                page.get_pixmap(matrix=fitz.Matrix(args.dpi / 72, args.dpi / 72), alpha=False).save(image)
            evidence = EmbeddedEvidence(text=page.get_text("text", sort=True), blocks=_raw_text_blocks(page), extractor="pymupdf")
        provenance = {"source": str(pdf), "source_sha256": sha256_file(pdf), "pdf_page": number,
                      "image_sha256": sha256_file(image), "anchors": anchors}
        atomic_json(case_dir / "source.json", provenance)
        for name in args.variants:
            result_path = case_dir / f"{name}.json"
            raw_path = case_dir / f"{name}.txt"
            if result_path.exists():
                result = json.loads(result_path.read_text())
                if result["source"] != provenance or result["raw_sha256"] != sha256_file(raw_path):
                    raise ValueError("cached experiment artifact differs from recorded provenance")
                results.append(result)
                print(json.dumps({"case": case, "variant": name, "cached": True, **result["metrics"]}), flush=True)
                continue
            variant = VARIANTS[name]
            backend._decode_guidance = variant.guidance
            backend._initial_grounding = variant.grounded
            backend._block_decoding = variant.blocks
            backend._block_retries = variant.retries
            backend._startup_recovery = name.endswith("automatic")
            supplied = evidence if variant.native else None
            start = perf_counter()
            if variant.visual == "base":
                raw, generation = backend.recognize_pages([image], embedded=[supplied] if supplied else None)
            elif variant.visual == "detail":
                raw, generation = backend.recognize_detail(image, embedded=supplied)
            else:
                raw, generation = backend._recognize(image, task="document parsing.", cropping=True,
                                                     image_size=1024, mode="legacy_detail", ngram_window=128)
            seconds = round(perf_counter() - start, 3)
            generation.pop("_confidence_spans", None)
            result = {"case": case, "variant": name, "seconds": seconds, "source": provenance,
                      "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(), "generation": generation,
                      "forced_grounding_prefix": bool(generation.get("decoding", {}).get("forced_prefix_tokens")),
                      "metrics": metrics(raw, generation, evidence, anchors)}
            atomic_text(raw_path, raw)
            atomic_json(result_path, result)
            results.append(result)
            atomic_json(output / "results.json", results)
            print(json.dumps({"case": case, "variant": name, "seconds": seconds, **result["metrics"]}), flush=True)
    atomic_json(output / "results.json", results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", default=list(CASES), help="PDF-stem-PAGE; known cases also have manually reviewed probes")
    parser.add_argument("--variants", choices=VARIANTS, nargs="+", default=["base-exact", "base-guided", "detail-exact", "base-grounded"])
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--dpi", type=int, default=300)
    args = parser.parse_args()
    if not 128 <= args.max_tokens <= 8192 or not 72 <= args.dpi <= 600:
        parser.error("token budget must be 128..8192 and DPI 72..600")
    run(args)
