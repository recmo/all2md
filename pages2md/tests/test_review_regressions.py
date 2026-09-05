from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
import pytest

from pages2md.model import Block
from pages2md.reconciliation import (
    _repair_embedded_digit_runs,
    _repair_embedded_math_structure,
)
from pages2md.footnotes import normalize_footnotes, place_footnotes
from pages2md.markdown import strict_page_markdown, title_case_heading
from test_alignment import evidence, BOX
from test_footnotes import page


@pytest.mark.parametrize('text', [
    'The measured values are 1 2 3 in this order.',
    'The measured values are 1 2 3; another value is 91234.',
    'The measured values are 1 2 3; another value is 123.',
    'The measured values are 0. 1 2 3 in this order.',
    'The measured values are 0. 1 2 3; another value is 0.123.',
])
def test_numeric_repairs_preserve_native_token_boundaries(text):
    block = Block('paragraph', text, bbox=BOX)
    native = evidence([(text, 10, 100, 10, 'Times-Roman')])
    assert not _repair_embedded_digit_runs([block], native)
    assert block.markdown == text


@pytest.mark.parametrize('kind,text', [
    ('code', '1 2 3'),
    ('paragraph', '`1 2 3`'),
    ('paragraph', '[example](https://example.com/1 2 3)'),
])
def test_numeric_repairs_leave_code_and_links_untouched(kind, text):
    block = Block(kind, text, bbox=BOX)
    native = evidence([('123', 10, 100, 10, 'Times-Roman')])
    assert not _repair_embedded_digit_runs([block], native)
    assert block.markdown == text


def test_sum_binders_do_not_borrow_evidence_from_another_sum():
    text = r'\(\sum_{i=0}^{n} a_i b_i + \sum_{j=0}^{n} a_j b_j\)'
    block = Block('formula', text, bbox=BOX)
    native = evidence([('sum i=0 n ai bi + sum j=0 n aj bj', 10, 100, 10, 'Times-Roman')])
    assert not _repair_embedded_math_structure([block], native)
    assert block.markdown == text


def test_numeric_script_boundary_does_not_need_native_whitespace():
    block = Block('formula', r'\(2^{1 8}\)', bbox=BOX)
    native = evidence([('2', 10, 100, 10, 'Times-Roman'),
                       ('18', 15, 96, 7, 'Times-Roman')])
    assert native.text == '218'
    _repair_embedded_digit_runs([block], native)
    assert block.markdown == r'\(2^{18}\)'


def test_native_sentence_period_is_not_part_of_a_number():
    block = Block('formula', r'\(x^{1 / 1 8}\).', bbox=BOX)
    native = evidence([('x1/18.', 10, 100, 10, 'Times-Roman')])
    _repair_embedded_digit_runs([block], native)
    assert block.markdown == r'\(x^{1 / 18}\).'


def test_heading_footnote_survives_title_case_and_placement():
    p = page()
    p.blocks[0].kind = 'heading'
    p.blocks[0].markdown = r'# Important result\(^{1}\)'
    normalize_footnotes([p])
    rendered = strict_page_markdown(p, [])
    output = place_footnotes(rendered, [p])
    assert '# Important Result[^p1-note-1]' in output
    assert '[^p1-note-1]: A detailed explanatory note.' in output


def test_heading_link_preserves_nested_reference_placeholder():
    assert title_case_heading('[important result[^p1-note-1]](https://example.com)') == (
        '[Important Result[^p1-note-1]](https://example.com)')


@pytest.mark.parametrize('prefix,continuation', [
    ('> ', '> '), ('> > ', '> > '), ('> - ', '>   '), ('- > ', '  > '),
])
def test_footnote_placement_preserves_quote_and_list_containers(prefix, continuation):
    p = page()
    normalize_footnotes([p])
    source = f'{prefix}First[^p1-note-1].\n{continuation}\n{continuation}Second paragraph.\n'
    output = place_footnotes(source, [p])
    assert continuation + '[^p1-note-1]:' in output
    parser = MarkdownIt('commonmark').use(footnote_plugin)
    containers = lambda text: [t.type for t in parser.parse(text)
                              if t.type in {'blockquote_open', 'bullet_list_open', 'list_item_open'}]
    assert containers(output) == containers(source)
    assert any(t.type == 'footnote_open' for t in parser.parse(output))
