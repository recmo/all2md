"""Serialize explicit bare web URLs without rewriting their address or label."""
from __future__ import annotations

import re
from bisect import bisect_left

from linkify_it import LinkifyIt
from markdown_it import MarkdownIt
from markdown_it.rules_inline import text as inline_text
from markdown_it.rules_inline.emphasis import tokenize as emphasis
from markdown_it.rules_inline.strikethrough import tokenize as strikethrough

from .syntax import mask_math, math_spans, non_math_ranges


def autolink_urls(markdown: str) -> str:
    if not re.search(r"https?://", markdown, re.I):
        return markdown
    parser = MarkdownIt("commonmark").enable("strikethrough")
    env = {}
    parser.parse(markdown, env)
    excluded = non_math_ranges(markdown)
    spans, _ = math_spans(markdown)
    masked = list(mask_math(markdown, spans))
    for start, end in excluded:
        for index in range(start, end):
            if masked[index] not in "\r\n":
                masked[index] = "x"
    source = "".join(masked)
    # Resolve emphasis first: punctuation such as the closing '*' in
    # '*Visit https://example.org/path.*' is Markdown, not part of the address.
    # Retain token identities through Markdown's delimiter-resolution pass.
    delimiters = []

    def located_emphasis(state, silent):
        start = state.pos
        accepted = emphasis(state, silent)
        if accepted and not silent and state.src == source:
            count = state.pos - start
            delimiters.extend((token, start + offset)
                              for offset, token in enumerate(state.tokens[-count:]))
        return accepted

    parser.inline.ruler.at("emphasis", located_emphasis)

    def located_strikethrough(state, silent):
        before = len(state.tokens)
        accepted = strikethrough(state, silent)
        if accepted and not silent and state.src == source:
            cursor = state.pos
            for token in reversed(state.tokens[before:]):
                if not token.content or set(token.content) != {"~"}:
                    break
                cursor -= len(token.content)
                delimiters.append((token, cursor))
        return accepted

    parser.inline.ruler.at("strikethrough", located_strikethrough)
    parser.parseInline(source, env)
    url_input = list(source)
    for token, start in delimiters:
        if token.type in {"em_open", "em_close", "strong_open", "strong_close", "s_open", "s_close"}:
            if token.type == "strong_open":
                start -= 1
            for index in range(start, start + len(token.markup)):
                url_input[index] = " "
    parser.inline.ruler.at("emphasis", emphasis)
    parser.inline.ruler.at("strikethrough", strikethrough)
    linkifier = LinkifyIt().set({"fuzzy_link": False, "fuzzy_email": False})
    matches = {match.index: match.last_index for match in linkifier.match("".join(url_input)) or []
               if match.schema.casefold() in {"http:", "https:"}
               # Escapes/entities have different literal and rendered forms;
               # do not guess which form was intended as the URL address.
               and not re.search(r"\\|&(?:#\w+|\w+);", match.raw)}
    if not matches:
        return markdown
    starts = sorted(matches)
    edits = set()

    def bare_url(state, silent):
        if state.src != source or state.pos not in matches:
            return False
        end = matches[state.pos]
        if end > state.posMax:
            return False
        if not silent:
            if state.linkLevel == 0:
                edits.add((state.pos, end))
            state.pending += state.src[state.pos:end]
        state.pos = end
        return True

    def bounded_text(state, silent):
        # Let Markdown consume code spans, links, autolinks, tags, etc. normally,
        # but stop a prose text run at the next URL so our rule can see it.
        if state.src == source:
            next_index = bisect_left(starts, state.pos + 1)
            if next_index < len(starts):
                next_start = starts[next_index]
                terminator = state.md.inline.terminator_re.search(state.src, state.pos)
                end = min(terminator.start() if terminator else state.posMax, state.posMax)
                if state.pos < next_start < end:
                    if not silent:
                        state.pending += state.src[state.pos:next_start]
                    state.pos = next_start
                    return True
        return inline_text(state, silent)

    parser.inline.ruler.before("text", "bare_url", bare_url)
    parser.inline.ruler.at("text", bounded_text)
    parser.parseInline(source, env)
    for start, end in sorted(edits, reverse=True):
        markdown = markdown[:start] + "<" + markdown[start:end] + ">" + markdown[end:]
    return markdown
