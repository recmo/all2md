import importlib.util
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest


@pytest.mark.parametrize("change", ["none", "source", "image", "missing_provenance"])
def test_benchmark_cache_requires_original_source_and_image(tmp_path, monkeypatch, change):
    script = Path(__file__).parents[1] / "scripts/benchmark_decoding.py"
    spec = importlib.util.spec_from_file_location("benchmark_decoding", script)
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    calls = []
    monkeypatch.setattr(benchmark.MlxUnlimitedOcr, "_load", lambda self: None)
    monkeypatch.setattr(
        benchmark.MlxUnlimitedOcr, "recognize_pages",
        lambda self, images, **kw: (calls.append(images) or "source text", {"finish_reason": "stop"}),
    )
    pdf = tmp_path / "fixture.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72,72), "source text")
        document.save(pdf)
    args = SimpleNamespace(output=tmp_path/"output", corpus=tmp_path, dpi=72,
                           max_tokens=4096, cases=["fixture-1"], variants=["base"])
    benchmark.run(args)
    folder = args.output / "fixture-1"
    raw = (folder/"base.txt").read_bytes()
    provenance = (folder/"source.json").read_bytes()
    if change == "source":
        pdf.write_bytes(b"changed source")
    elif change == "image":
        (folder/"source-72dpi.png").write_bytes(b"changed image")
    elif change == "missing_provenance":
        (folder/"source.json").unlink()
    if change == "none":
        benchmark.run(args)
    else:
        with pytest.raises(ValueError, match="fresh --output"):
            benchmark.run(args)
    assert len(calls) == 1
    assert (folder/"base.txt").read_bytes() == raw
    if change != "missing_provenance":
        assert (folder/"source.json").read_bytes() == provenance
