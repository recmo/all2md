"""Cross-cutting contracts shared by every transcription repair."""
import pytest

from pages2md.alignment import semantic_math_projection
from pages2md.edits import apply_edits
from pages2md.embedded import assess_embedded
from pages2md.formatting import format_and_lint, format_markdown
from pages2md.lists import normalize_page_blocks
from pages2md.model import Block
from pages2md.reconciliation import _math_source_ranges, _repair_embedded_math_structure, reconcile_text
from pages2md.syntax import math_spans, protected_ranges
from test_alignment import BOX, evidence
from test_footnotes import page


@pytest.mark.parametrize('opening,closing', [(r'\(', r'\)'), (r'\[', r'\]'), ('$', '$'), ('$$', '$$')])
def test_math_ranges_are_the_same_for_repairs_and_validation(opening, closing):
    text = f'Before {opening}x^2{closing} after.'
    assert _math_source_ranges(text) == [(s.start, s.end) for s in math_spans(text)[0]]
    assert len(_math_source_ranges(text)) == 1


@pytest.mark.parametrize('text', [
    '`A`', '``A ` B``', '[A](https://example.com/a_(b))',
    '[label [A]](https://example.com)', '~~~\nA\n~~~',
    '[^A]', '<a href="https://example.com">A</a>',
])
def test_all_repairs_leave_opaque_syntax_alone(text):
    native = evidence([('A', 20, 100, 10, 'txsys')])
    block = Block('paragraph', text, bbox=BOX)
    reconcile_text([block], native, assess_embedded(native, text))
    assert block.markdown == text


def test_edit_application_is_source_mapped_and_rejects_conflicting_proposals():
    block = Block('paragraph', 'abcdef')
    assert apply_edits(block, [(0, 1, 'X'), (2, 4, 'Y'), (3, 5, 'Z')], 'test')
    assert block.markdown == 'Xbcdef'
    assert block.metadata['embedded_text_repairs'] == [
        {'kind': 'test', 'visual': 'a', 'embedded': 'X'}]
    assert not apply_edits(block, [(0, 1, 'X')], 'test')
    assert not apply_edits(block, [(0, 0, 'one'), (0, 0, 'two')], 'test')
    with pytest.raises(ValueError, match='bounds'):
        apply_edits(block, [(0, 99, '')], 'test')


def test_reconciliation_is_idempotent_and_list_rendering_is_derived():
    native = evidence([('a', 20, 100, 10, 'NewTXMI'), ('b', 20, 120, 10, 'NewTXMI')])
    blocks = normalize_page_blocks([Block('list', '- a\n- b', bbox=BOX)])
    trust = assess_embedded(native)
    reconcile_text(blocks, native, trust)
    once = blocks[0].markdown
    assert r'\(a\)' in once and r'\(b\)' in once
    assert not reconcile_text(blocks, native, trust)
    assert normalize_page_blocks(blocks)[0].markdown == once


def test_formatter_preserves_math_and_local_notes_without_skipping_document(tmp_path):
    source = '# Title  \n\nText[^a] with \\( x ^ {2} \\).\n\n[^a]: A note.\n\nNext paragraph.\n'
    output = format_markdown(source)
    assert output.index('[^a]:') < output.index('Next paragraph.')
    assert r'\( x ^ {2} \)' in output
    assert format_markdown(output) == output
    path = tmp_path / 'paper.md'
    path.write_text(source)
    result = format_and_lint([path])
    assert not result.preservation_skips
    assert not result.lint_errors


def test_checkpoint_does_not_store_a_second_editable_canonical_copy():
    p = page()
    p.visual['canonical'] = {'blocks': [{'markdown': 'stale'}], 'markdown': 'stale',
                             'authoritative_observation': 'raw-1'}
    serialized = p.to_dict()
    assert serialized['visual']['canonical'] == {'authoritative_observation': 'raw-1'}
    assert serialized['blocks'][0]['markdown'] == p.blocks[0].markdown


@pytest.mark.parametrize('letter', ['b', 'j'])
def test_script_repairs_use_the_matched_base_not_a_neighbor(letter):
    native = evidence([
        ('X=Y', 10, 100, 10, 'NewTXMI'),
        (letter, 26, 94, 7, 'NewTXMI'), ('0', 29.5, 97, 5, 'Times-Roman'),
        ('0', 26, 104, 7, 'Times-Roman'),
        ('+Y', 50, 100, 10, 'NewTXMI'),
        ('j', 61, 94, 7, 'NewTXMI'), ('0', 64.5, 97, 5, 'Times-Roman'),
        ('0', 61, 104, 7, 'Times-Roman'), ('+Z', 85, 100, 10, 'NewTXMI'),
    ])
    block = Block('formula', r'\(X=Y_{0}^{b_{0}}+Y_{0}^{j_{0}}+Z\)', bbox=BOX)
    _repair_embedded_math_structure([block], native)
    assert block.markdown == rf'\(X=Y_{{0}}^{{{letter}_{{0}}}}+Y_{{0}}^{{j_{{0}}}}+Z\)'
