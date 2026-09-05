from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from pages2md.formatting import format_and_lint
from pages2md.markdown import write_markdown
from pages2md.model import Chapter, Comparison, EmbeddedEvidence, PageResult
from pages2md.urls import autolink_urls


@pytest.mark.parametrize(("source", "expected"), [
    ("See http://example.org/book.", "See <http://example.org/book>."),
    ("(https://example.org/a(b)).", "(<https://example.org/a(b)>)."),
    ("**https://example.org/a**", "**<https://example.org/a>**"),
    ("~~Visit https://example.org/path.~~", "~~Visit <https://example.org/path>.~~"),
    ("~~https://example.org/path~~", "~~<https://example.org/path>~~"),
    ("*Visit https://example.org/a_(b).*", "*Visit <https://example.org/a_(b)>.*"),
    ("See HTTPS://Example.org/a_b?x=1&y=2#frag now.",
     "See <HTTPS://Example.org/a_b?x=1&y=2#frag> now."),
    ("https://example.org/über", "<https://example.org/über>"),
])
def test_autolinks_preserve_addresses_and_surrounding_punctuation(source, expected):
    assert autolink_urls(source) == expected
    assert autolink_urls(expected) == expected


@pytest.mark.parametrize("source", [
    "`https://example.org` and ``code ` http://example.org``",
    "```text\nhttp://example.org\n```\n",
    "    http://example.org\n",
    r"\(\text{http://example.org}\) and $\text{https://example.org}$",
    "<http://example.org>",
    "[http://example.org](https://elsewhere.org)",
    "![http://example.org](https://elsewhere.org/image.png)",
    '[ref]: http://example.org "http://example.org/title"\n\n[http://example.org][ref]\n',
    '<a href="http://example.org">http://example.org</a>',
    '<img src="http://example.org/image.png">',
    '<!-- http://example.org -->',
    '<div>\nhttp://example.org\n</div>\n',
    'www.example.org and user@example.org',
    'http://example.org/?x=1&amp;y=2',
])
def test_existing_markup_code_math_and_ambiguous_entities_are_untouched(source):
    assert autolink_urls(source) == source


def test_repeated_address_is_only_linked_in_prose():
    url = "http://example.org"
    source = f"`{url}` then {url}, [{url}]({url}) and <{url}>."
    assert autolink_urls(source) == f"`{url}` then <{url}>, [{url}]({url}) and <{url}>."


@pytest.mark.parametrize("split", [False, True])
def test_bare_url_serialization_is_used_in_single_and_split_outputs(tmp_path, split):
    page = PageResult(number=1, image="page.png", visual_markdown="Visit http://example.org/book.",
                      blocks=[], embedded=EmbeddedEvidence(), comparison=Comparison())
    paths = write_markdown(tmp_path, [page], [Chapter("Chapter", 1, 1, "chapter")],
                           split=split, title="Book")
    content = "\n".join((tmp_path / path).read_text() for path in paths)
    assert "<http://example.org/book>" in content
    result = format_and_lint([tmp_path / path for path in paths])
    assert not any("MD034" in error for error in result.lint_errors)
    renderer = MarkdownIt("commonmark")
    assert 'href="http://example.org/book"' in renderer.render(content)
