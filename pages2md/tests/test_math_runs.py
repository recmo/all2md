from pages2md.model import Block
from pages2md.alignment import GlyphAlignment, semantic_math_projection
from pages2md.reconciliation import _math_glyph_run_edits
from pages2md.edits import apply_edits
import pytest


def fixture():
    markdown=r"contains \(2^{O_p(1/\eta)}\) such points"
    text,spans=semantic_math_projection(markdown)
    start=text.index("Op")
    native=text[:start]+"Ωρ"+text[start+2:]
    glyphs=[{"text":c,"origin":[i,10],"direction":[1,0],"bbox":[i,1,i+1,10]} for i,c in enumerate(native)]
    matches={i:i for i in range(len(text)) if i not in (start,start+1)}
    return Block("paragraph",markdown),GlyphAlignment(text,spans,glyphs,matches,native,{},matches.copy()),start


def test_two_glyph_math_run_preserves_scripts():
    block,a,_=fixture()
    edits=_math_glyph_run_edits(block,a,[(0,len(block.markdown))])
    assert len(edits)==2
    apply_edits(block,edits,"math_glyph")
    assert r"2^{\Omega_\rho(1/\eta)}" in block.markdown


@pytest.mark.parametrize("failure",["prose","ambiguous","missing_anchor","no_geometry","rotated","latin","case","invalid_box"])
def test_math_run_abstains(failure):
    block,a,start=fixture()
    ranges=[(0,len(block.markdown))]
    if failure=="prose":ranges=[]
    if failure=="ambiguous":a.native+=a.native
    if failure=="missing_anchor":a.matches.pop(start-1)
    if failure=="no_geometry":a.glyphs[start].pop("origin")
    if failure=="rotated":a.glyphs[start]["direction"]=[0,1]
    if failure=="latin":a.native=a.native[:start]+"AB"+a.native[start+2:]
    if failure=="case":a.native=a.native[:start]+"ωΡ"+a.native[start+2:]
    if failure=="invalid_box":a.glyphs[start]["bbox"]=[0,0,float("nan"),20]
    assert not _math_glyph_run_edits(block,a,ranges)
