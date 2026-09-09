from pages2md.quality import repeated_region_pairs,unresolved_decode_errors
from pages2md.native import parse_native_observation
import pytest


TEXT="A substantial paragraph can occur again with slightly different mathematical formatting and wording."


def block(y=100,page=1):
    return {'kind':'paragraph','bbox':[100,y,900,y+50],'markdown':TEXT,'source_pages':[page]}


def test_same_region_repeated_beyond_short_window():
    blocks=[block()]+[block(200+i*60) for i in range(8)]+[block()]
    assert repeated_region_pairs(blocks)==[(0,9)]
    assert unresolved_decode_errors({'blocks':blocks,'visual_markdown':TEXT*10,'embedded':{'text':TEXT*10}})==['unresolved OCR region repetition']


def test_legitimate_different_regions_and_pages_are_preserved():
    assert not repeated_region_pairs([block(),block(200),block(page=2)])
    a,b=block(),block()
    a['kind']=b['kind']='table'
    assert not repeated_region_pairs([a,b])


def test_native_validation_detects_long_distance_repeat():
    raw=f'<|det|>text [100,100,900,150]<|/det|>{TEXT}'
    raw+=''.join(f'<|det|>text [100,{200+i*60},900,{250+i*60}]<|/det|>{TEXT}' for i in range(8))
    raw+=f'<|det|>text [100,100,900,150]<|/det|>{TEXT}'
    assert 'visual_region_repetition' in parse_native_observation(raw,mode='multi_base',source_pages=[1]).warnings


@pytest.mark.parametrize('failure',[None,'unsupported','unrelated_prefix','distant','already_covered'])
def test_preserve_supported_opening_without_copying_a_repeated_body(failure):
    from pages2md.native import _restore_supported_opening
    from pages2md.model import EmbeddedEvidence
    opening='Thus lambda is incident to at least three distinct subspaces.'
    primary=parse_native_observation(f'<|det|>text [100,100,500,120]<|/det|>{opening}',mode='multi_base',source_pages=[1])
    prefix='the index of three distinct subspaces'
    if failure=='unrelated_prefix':prefix='unrelated invented sentence about completely different things'
    top=150 if failure=='distant' else 125
    if failure=='already_covered':top=100
    recovery=parse_native_observation(prefix+f'<|det|>text [100,{top},900,180]<|/det|>The clean body continues here.',mode='gundam_detail',source_pages=[1])
    native=EmbeddedEvidence(text=opening,blocks=[{'bbox':[100,100,900,180],'text':opening,'lines':[{'bbox':[100,100,900,120],'text':opening}]}])
    if failure=='unsupported':native.blocks=[]
    result,actions=_restore_supported_opening(primary,recovery,native)
    assert result.raw==recovery.raw
    if failure:
        assert not actions
        assert result is recovery
    else:
        assert result.blocks[0].markdown==opening
        assert actions[0]['action']=='preserved_supported_opening'
        assert recovery.blocks[0].markdown==prefix


def test_region_repetition_requests_base_even_when_detail_is_clean(tmp_path):
    from types import SimpleNamespace
    from copy import deepcopy
    from pages2md.model import EmbeddedEvidence
    from pages2md.pipeline import _collect_page_candidates
    raw=f'<|det|>text [100,100,900,150]<|/det|>{TEXT}'
    primary=parse_native_observation(raw+raw,mode='multi_base',source_pages=[1])
    group=deepcopy(primary);group.source_pages=[1,2];group.warnings=[]
    calls=[]
    backend=SimpleNamespace(
        supports_region_recovery=False,
        recognize_detail=lambda image, **kw:(calls.append('detail') or raw,{'finish_reason':'stop'}),
        recognize_pages=lambda images, **kw:(calls.append('base') or raw,{'finish_reason':'stop'}),
    )
    source=SimpleNamespace(number=1,image_path=tmp_path/'page.png',embedded=EmbeddedEvidence())
    candidates,warnings=_collect_page_candidates(source,primary,group,backend,tmp_path)
    assert calls==['detail','base']
    assert len(candidates)==2
    assert not warnings
