"""Bounded image-only / structural / native-guided live comparison, stdout only.

This is a smoke test, not a transcription-accuracy benchmark. All modes use the
same image and fresh invocation state. It does not modify existing OCR bundles.
"""
import argparse
import hashlib
from difflib import SequenceMatcher
import json
from pathlib import Path
from time import perf_counter

from pages2md.model import EmbeddedEvidence
from pages2md.ocr import MlxUnlimitedOcr


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("page_json", type=Path)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--summary", action="store_true", help="print a short output preview and exact-baseline comparison")
    args = parser.parse_args()
    if not 1 <= args.max_tokens <= 4096:
        parser.error("--max-tokens must be between 1 and 4096")
    page = json.loads(args.page_json.read_text())
    evidence = EmbeddedEvidence(**{k: v for k, v in page["embedded"].items() if k != "links"})
    backend = MlxUnlimitedOcr(max_tokens=args.max_tokens)
    backend._initial_grounding = backend._block_decoding = False
    backend._startup_recovery = False
    backend._load()
    baseline = None
    for mode in ("exact-ngram", "structural", "native-guided"):
        backend._decode_guidance = mode != "exact-ngram"
        started = perf_counter()
        text, generation = backend.recognize_pages([args.image], embedded=[evidence] if mode == "native-guided" else None)
        generation.pop("_confidence_spans", None)
        if baseline is None:
            baseline = text
        changes = [{"baseline": baseline[a:b], "candidate": text[c:d]}
                   for kind, a, b, c, d in SequenceMatcher(None, baseline, text, autojunk=False).get_opcodes()
                   if kind != "equal"]
        print(json.dumps({"mode": mode, "seconds": round(perf_counter() - started, 3),
                          "output": text[:240] if args.summary else text,
                          "output_characters": len(text), "first_grounding_offset": text.find("<|det|>"),
                          "output_sha256": hashlib.sha256(text.encode()).hexdigest(),
                          "same_as_exact_ngram": text == baseline,
                          "changes_from_baseline": changes[:16],
                          "generation": generation}, ensure_ascii=False), flush=True)
