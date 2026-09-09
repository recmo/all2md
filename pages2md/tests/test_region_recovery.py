from copy import deepcopy
from pathlib import Path
from PIL import Image
import pytest
from pages2md.model import Block
from pages2md.native import parse_native_observation, reconcile_observations
from pages2md.region_recovery import math_key, corroborates, disputed_regions, render_region, apply_regions


GOOD = r"\leq n_{\text{out}}\cdot\epsilon_{\mathrm{MCA}}(\gamma)"
BAD = r"\leq\sum_{i\in[n_{out}]}c_{MCA}(\gamma)"


def observation(body, mode="multi_base", box="100,400,400,440"):
    return parse_native_observation(f"<|det|>equation [{box}]<|/det|>\\[{body}\\]", mode=mode, source_pages=[1], generation={"finish_reason":"stop"})


def test_math_key_preserves_semantics():
    assert math_key(r"x_i") != math_key(r"x^i")
    assert math_key(r"\mathbb{F}") != math_key("F")
    assert math_key(r"\Omega") != math_key("O")
    assert math_key(r"\frac{ab}{c}") != math_key(r"\frac{a}{bc}")
    assert math_key(r"\leqslant") != math_key(r"\le")
    assert math_key(GOOD) == math_key(GOOD.replace("mathrm","text"))


@pytest.mark.parametrize("peer", [r"x_{index}^2",r"\frac{x_{index}}{2}",r"x_{index}+y",r"xx_{index}"])
def test_corroboration_rejects_subexpression(peer):
    assert not corroborates(r"x_{index}",peer)


def test_corroboration_accepts_complete_merged_row():
    assert corroborates(GOOD, r"\begin{array}{l}X=Y\\" + GOOD + r".\\\end{array}")
    assert not corroborates(GOOD,GOOD+r"\\"+GOOD)


def test_crop_replaces_only_agreed_local_expression():
    base = observation(BAD)
    peer = observation(r"\begin{array}{l}X=Y\\"+GOOD+r".\\\end{array}","gundam_detail","100,100,700,300")
    region = observation(GOOD,"region_detail","200,200,800,700")
    region.generation["region_target_bbox"] = [100,400,400,440]
    original = deepcopy(base.blocks)
    blocks, actions, _ = apply_regions(deepcopy(base.blocks),[region],[peer],base)
    assert GOOD in blocks[0].markdown
    assert blocks[0].bbox == original[0].bbox
    assert base.blocks == original
    assert actions[0]["confirming_observation"] == peer.id
    assert actions[0]["recovery_observation"] == region.id
    # Ordinary reconciliation must not use the crop's canvas coordinates.
    result, actions, _ = reconcile_observations(base,[peer,region])
    assert GOOD in result[0].markdown


@pytest.mark.parametrize("failure",["unconfirmed","truncated","multiple","wrong_page","wrong_box"])
def test_region_abstains(failure):
    base,peer,region = observation(BAD),observation(GOOD,"gundam_detail"),observation(GOOD,"region_detail")
    region.generation["region_target_bbox"] = [100,400,400,440]
    if failure=="unconfirmed":peer.blocks[0].markdown="different"
    if failure=="truncated":region.generation["finish_reason"]="length"
    if failure=="multiple":region.blocks.append(deepcopy(region.blocks[0]))
    if failure=="wrong_page":region.source_pages=[2]
    if failure=="wrong_box":region.generation["region_target_bbox"]=[500,500,800,800]
    result,actions,_=reconcile_observations(base,[peer,region])
    assert not any(a["action"]=="selected_region_consensus" for a in actions)


def test_disputes_are_bounded_and_agreement_skips_work():
    base=observation(BAD)
    base.blocks *= 4
    assert len(disputed_regions(base,[observation(GOOD,"gundam_detail")]))==2
    assert not disputed_regions(base,[observation(BAD,"gundam_detail")])


def test_crop_padding_avoids_neighbor(tmp_path):
    page=tmp_path/"page.png"
    Image.new("RGB",(1000,1000),"white").save(page)
    geometry=render_region(page,(100,400,400,440),[Block("formula","",bbox=(100,300,400,398))],tmp_path/"crop.png")
    assert geometry["page_bbox"][1]==399
    assert geometry["canvas_size"][0]==1024


def test_one_failed_crop_keeps_other_attempt(monkeypatch,tmp_path):
    from types import SimpleNamespace
    import pages2md.region_recovery as region
    base=observation(BAD)
    base.blocks.append(deepcopy(base.blocks[0]))
    def read(*args):
        if args[2]==1:raise RuntimeError("fixture failure")
        return observation(GOOD,"region_detail")
    monkeypatch.setattr(region,"_read_region",read)
    warnings=[]
    result=region.collect_regions(None,base,[observation(GOOD)],SimpleNamespace(supports_region_recovery=True),tmp_path,warnings=warnings)
    assert len(result)==1
    assert warnings==["visual_region_ocr_failed"]


def test_region_budget_is_internal(monkeypatch):
    from pages2md.ocr import MlxUnlimitedOcr
    backend=MlxUnlimitedOcr()
    seen={}
    monkeypatch.setattr(backend,"_recognize",lambda image,**kwargs:seen.update(kwargs))
    backend.recognize_region(Path("fixture.png"))
    assert seen["max_tokens"]==4096
    assert backend.max_tokens==32768
    assert "embedded" not in seen


def test_identical_crop_text_retains_distinct_targets(tmp_path):
    from types import SimpleNamespace
    from pages2md.region_recovery import collect_regions
    page=tmp_path/"page.png"
    Image.new("RGB",(1000,1000),"white").save(page)
    base=observation(BAD)
    base.blocks.append(deepcopy(base.blocks[0]))
    raw=observation(GOOD).raw
    backend=SimpleNamespace(supports_region_recovery=True,recognize_detail=lambda image:(raw,{"finish_reason":"stop"}))
    regions=collect_regions(SimpleNamespace(image_path=page,number=1),base,[observation(GOOD)],backend,tmp_path)
    assert len(regions)==2
    assert regions[0].raw==regions[1].raw
    assert regions[0].id!=regions[1].id
    assert all((tmp_path/"raw"/f"{r.id}.txt").exists() for r in regions)


def test_crop_image_provenance_is_verified(tmp_path):
    from pages2md.verify import _verify_region_evidence
    from pages2md.util import sha256_file
    image=tmp_path/"crop.png"
    Image.new("RGB",(20,20),"white").save(image)
    value={"mode":"region_detail","generation":{"region_image":"crop.png","region_geometry":{"image_sha256":sha256_file(image)}}}
    assert not _verify_region_evidence(value,tmp_path)
    value["generation"]["region_geometry"]["image_sha256"]="wrong"
    assert _verify_region_evidence(value,tmp_path)==["region observation image hash mismatch"]
    value["generation"]["region_image"]="../outside.png"
    assert _verify_region_evidence(value,tmp_path)==["region observation image is missing or outside bundle"]
