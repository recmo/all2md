use pulldown_cmark::{CodeBlockKind, Event, Options, Parser, Tag, TagEnd};

use crate::{
    config::MarkdownConfig,
    markdown::{Finding, ParsedPage},
};

pub(crate) fn validate(
    rules: &MarkdownConfig,
    path: &str,
    text: &str,
    page: &ParsedPage,
    findings: &mut Vec<Finding>,
) {
    let mut report = |rule: &str, line: usize, message: &str| {
        findings.push(Finding {
            path: path.into(),
            message: format!("markdown.{rule}: {message}"),
            line: Some(line),
        });
    };
    let mut previous_level = None;
    for heading in &page.headings {
        if rules.nonempty_headings && heading.text.trim().is_empty() {
            report(
                "nonempty_headings",
                heading.line,
                "heading must contain text",
            );
        }
        if rules.heading_increment && previous_level.is_some_and(|level| heading.level > level + 1)
        {
            report("heading_increment", heading.line, "heading skips a level");
        }
        previous_level = Some(heading.level);
    }
    let mut body_offset = 0;
    for (index, raw) in text.split_inclusive('\n').enumerate() {
        let line_number = index + 1;
        if line_number < page.body_start_line {
            body_offset += raw.len();
            continue;
        }
        if page
            .code_blocks
            .iter()
            .any(|range| range.start_line <= line_number && line_number <= range.end_line)
        {
            continue;
        }
        let line = raw.strip_suffix('\n').unwrap_or(raw);
        let line = line.strip_suffix('\r').unwrap_or(line);
        let trimmed = line.trim_end_matches([' ', '\t']);
        let trailing = &line[trimmed.len()..];
        if rules.no_trailing_whitespace
            && !trailing.is_empty()
            && !(trailing == "  " && !trimmed.is_empty())
        {
            report(
                "no_trailing_whitespace",
                line_number,
                "unexpected trailing whitespace",
            );
        }
        if rules.no_tabs && line.contains('\t') {
            report("no_tabs", line_number, "tab outside a code block");
        }
        if rules
            .max_line_length
            .is_some_and(|limit| line.chars().count() > limit)
        {
            report(
                "max_line_length",
                line_number,
                "line exceeds configured character limit",
            );
        }
    }
    if rules.final_newline && !text.is_empty() && !text.ends_with('\n') {
        report(
            "final_newline",
            text.lines().count(),
            "document must end with a newline",
        );
    }
    if !(rules.closed_fences || rules.fence_language || rules.nonempty_links) {
        return;
    }
    let body = &text[body_offset..];
    let mut fence = None;
    for (event, range) in Parser::new_ext(body, Options::all()).into_offset_iter() {
        match event {
            Event::Start(Tag::CodeBlock(CodeBlockKind::Fenced(info))) => {
                let line = page.body_start_line
                    + body[..range.start].bytes().filter(|b| *b == b'\n').count();
                if rules.fence_language && info.trim().is_empty() {
                    report(
                        "fence_language",
                        line,
                        "fenced code requires a language/info string",
                    );
                }
                let opening_end = body[range.clone()]
                    .find('\n')
                    .map_or(range.end, |offset| range.start + offset + 1);
                fence = Some((line, opening_end, range.end));
            }
            Event::Text(_) => {
                if let Some((_, content_end, _)) = &mut fence {
                    *content_end = range.end;
                }
            }
            Event::End(TagEnd::CodeBlock) => {
                if let Some((line, content_end, end)) = fence.take()
                    && rules.closed_fences
                    && !body[content_end..end].contains(['`', '~'])
                {
                    report("closed_fences", line, "fenced code block is not closed");
                }
            }
            Event::Start(Tag::Link { dest_url, .. })
                if rules.nonempty_links && dest_url.trim().is_empty() =>
            {
                let line = page.body_start_line
                    + body[..range.start].bytes().filter(|b| *b == b'\n').count();
                report("nonempty_links", line, "link destination must not be empty");
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use crate::{Config, markdown::validate_corpus};

    fn check(text: &str, rules: &str) -> Vec<crate::Finding> {
        let config = Config::from_yaml(&format!(
            "documents:\n  include: ['**/*.md']\nmarkdown:\n{rules}"
        ))
        .unwrap();
        validate_corpus(
            &config,
            &HashMap::from([("page.md".into(), text.into())]),
            &HashMap::new(),
        )
        .err()
        .unwrap_or_default()
    }

    #[test]
    fn detects_unclosed_fences_in_containers() {
        for text in [
            "```rust\nx\n",
            "~~~\n",
            "> ```\n> x\n",
            "- ```\n  x\n",
            "```\n```not a closing fence\n",
        ] {
            let findings = check(text, "  closed_fences: true");
            assert_eq!(findings.len(), 1, "{text:?}: {findings:?}");
            assert_eq!(findings[0].line, Some(1));
        }
        for text in [
            "```rust\nx\n```\n",
            "~~~\n~~~",
            "> ```\n> x\n> ```\n",
            "- ```\n  x\n  ```\n",
            "````\n```\n````\n",
            "```\n```",
            "    ```\n",
        ] {
            assert!(check(text, "  closed_fences: true").is_empty(), "{text:?}");
        }
    }

    #[test]
    fn style_is_configured_and_reports_source_lines() {
        let text = "---\nname: value\n---\n#\n### Jump\n[text]()\n```\nx\n```\ntrailing \n\ttext\nlonger than ten";
        assert!(check(text, "  final_newline: false").is_empty());
        let findings = check(
            text,
            "  nonempty_headings: true\n  heading_increment: true\n  nonempty_links: true\n  fence_language: true\n  no_trailing_whitespace: true\n  no_tabs: true\n  max_line_length: 10\n  final_newline: true",
        );
        let lines: Vec<_> = findings.iter().map(|f| f.line.unwrap()).collect();
        assert_eq!(findings.len(), 8, "{findings:?}");
        for line in [4, 5, 6, 7, 10, 11, 12] {
            assert!(lines.contains(&line), "missing {line}: {findings:?}");
        }
    }

    #[test]
    fn preserves_frontmatter_code_and_hard_breaks() {
        let text = "---\nvalue: 'long value'\n---\n## 中\ntext  \n```rust\n\tlong code with spaces   \n```\n";
        assert!(check(text, "  no_trailing_whitespace: true\n  no_tabs: true\n  max_line_length: 6\n  heading_increment: true\n  closed_fences: true").is_empty());
        assert!(check("中日\r\n", "  max_line_length: 2\n  final_newline: true").is_empty());
        assert!(
            Config::from_yaml("documents: {include: ['**/*.md']}\nmarkdown: {max_line_length: 0}")
                .is_err()
        );
        assert!(
            Config::from_yaml("documents: {include: ['**/*.md']}\nmarkdown: {unknown_rule: true}")
                .is_err()
        );
    }
}
