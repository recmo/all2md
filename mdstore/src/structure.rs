use pulldown_cmark::{Event, Options, Parser, Tag};
use regex::Regex;

use crate::{
    Config, DateOrder, SectionListRule,
    markdown::{Finding, ParsedPage},
};

pub(crate) fn validate(
    config: &Config,
    path: &str,
    text: &str,
    page: &ParsedPage,
    findings: &mut Vec<Finding>,
) {
    if !config
        .sections
        .iter()
        .any(|rule| rule.level.is_some() || rule.list.is_some())
    {
        return;
    }
    let mut offsets = vec![0];
    offsets.extend(text.match_indices('\n').map(|(offset, _)| offset + 1));
    let body_offset = offsets
        .get(page.body_start_line - 1)
        .copied()
        .unwrap_or(text.len());
    let mut depth = 0_usize;
    let mut headings = Vec::new();
    for (event, range) in Parser::new_ext(&text[body_offset..], Options::all()).into_offset_iter() {
        match event {
            Event::Start(tag) => {
                if let Tag::Heading { level, .. } = tag
                    && depth == 0
                {
                    headings.push((
                        offsets.partition_point(|offset| *offset <= body_offset + range.start),
                        level as u8,
                        body_offset + range.start,
                        body_offset + range.end,
                    ));
                }
                depth += 1;
            }
            Event::End(_) => depth -= 1,
            _ => {}
        }
    }
    for rule in &config.sections {
        if rule.include.as_ref().is_some_and(|glob| {
            !globset::Glob::new(glob)
                .expect("validated glob")
                .compile_matcher()
                .is_match(path)
        }) {
            continue;
        }
        for heading in page.headings.iter().filter(|h| h.text == rule.heading) {
            let mut report = |line, message: String| {
                findings.push(Finding {
                    path: path.into(),
                    line: Some(line),
                    message: format!("section {:?}: {message}", rule.heading),
                })
            };
            if rule.level.is_some_and(|level| level != heading.level) {
                report(
                    heading.line,
                    format!("heading must have level {}", rule.level.unwrap()),
                );
            }
            let Some(list) = &rule.list else {
                continue;
            };
            if let Some(index) = headings.iter().position(|(line, ..)| *line == heading.line) {
                let heading_end = headings[index].3;
                let end = headings[index + 1..]
                    .iter()
                    .find(|(_, level, ..)| *level <= heading.level)
                    .map_or(text.len(), |(_, _, start, _)| *start);
                validate_list(
                    &text[heading_end..end],
                    heading_end,
                    &offsets,
                    list,
                    &mut report,
                );
            } else {
                report(
                    heading.line,
                    "structured sections must use uncontained headings".into(),
                );
            }
        }
    }
}

pub(crate) fn validate_list(
    text: &str,
    offset: usize,
    offsets: &[usize],
    rule: &SectionListRule,
    report: &mut impl FnMut(usize, String),
) {
    let marker = Regex::new(r"^(?:[-+*]|[0-9]{1,9}[.)])[ \t]+").expect("constant regex");
    let pattern = rule
        .item_pattern
        .as_ref()
        .map(|pattern| Regex::new(pattern).expect("validated regex"));
    let mut depth = 0_usize;
    let mut items = 0;
    let mut previous = None;
    for (event, range) in Parser::new_ext(text, Options::all()).into_offset_iter() {
        let line = offsets.partition_point(|start| *start <= offset + range.start);
        match event {
            Event::Start(tag) => {
                if depth > 0 && matches!(tag, Tag::Heading { .. }) {
                    report(line, "list section must not contain subsections".into());
                }
                if depth == 0 {
                    if let Tag::List(start) = &tag {
                        if rule
                            .ordered
                            .is_some_and(|ordered| ordered != start.is_some())
                        {
                            report(
                                line,
                                "list marker style does not match ordered setting".into(),
                            );
                        }
                    } else {
                        report(
                            line,
                            "body must contain only lists (no prose or subsections)".into(),
                        );
                    }
                }
                if depth == 1 && matches!(tag, Tag::Item) {
                    items += 1;
                    let raw = &text[range];
                    let content = marker.find(raw).map_or(raw, |marker| &raw[marker.end()..]);
                    if pattern
                        .as_ref()
                        .is_some_and(|pattern| !pattern.is_match(content))
                    {
                        report(line, "item does not match item_pattern".into());
                    }
                    if let Some(order) = rule.date_order {
                        if let Some(date) = date_prefix(content) {
                            if previous.is_some_and(|previous| match order {
                                DateOrder::Ascending => date < previous,
                                DateOrder::Descending => date > previous,
                            }) {
                                report(line, "item date is out of order".into());
                            }
                            previous = Some(date);
                        } else {
                            report(line, "item must start with a valid YYYY-MM-DD date followed by whitespace or end of item".into());
                        }
                    }
                }
                depth += 1;
            }
            Event::End(_) => depth -= 1,
            _ if depth == 0 => report(line, "body must contain only lists".into()),
            _ => {}
        }
    }
    if items < rule.minimum_items {
        report(
            offsets.partition_point(|start| *start <= offset),
            format!("list has {items} items; minimum is {}", rule.minimum_items),
        );
    }
}

fn date_prefix(content: &str) -> Option<&str> {
    let bytes = content.as_bytes();
    if bytes.len() < 10
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || !bytes[..10]
            .iter()
            .enumerate()
            .all(|(i, b)| i == 4 || i == 7 || b.is_ascii_digit())
        || content[10..]
            .chars()
            .next()
            .is_some_and(|c| !c.is_whitespace())
    {
        return None;
    }
    let year: u16 = content[..4].parse().ok()?;
    let month: usize = content[5..7].parse().ok()?;
    let day: u8 = content[8..10].parse().ok()?;
    let leap = year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
    let days = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    (year > 0 && (1..=12).contains(&month) && day > 0 && day <= days[month - 1])
        .then_some(&content[..10])
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn check(text: &str) -> Vec<Finding> {
        let config = Config::from_yaml("documents: {include: ['**/*.md']}\nsections:\n  - include: 'people/**'\n    heading: Timeline\n    required: true\n    maximum: 1\n    level: 2\n    list:\n      minimum_items: 1\n      ordered: false\n      date_order: descending\n").unwrap();
        crate::markdown::validate_corpus(
            &config,
            &HashMap::from([
                ("people/a.md".into(), text.into()),
                ("other/a.md".into(), "# Other template\n".into()),
            ]),
            &HashMap::new(),
        )
        .err()
        .unwrap_or_default()
    }

    #[test]
    fn validates_timeline_structure() {
        for text in [
            "  ## Timeline\n- 2024-01-01 Event\n",
            "## Timeline\n- 2024-03-01 New\n- 2024-02-29 Old\n## Other\nprose\n",
            "Timeline\n--------\n- 2024-01-01 One\n- 2024-01-01 Two\n",
            "## Timeline\n- 2024-01-01 Event\n  - Supporting detail\n",
        ] {
            assert!(check(text).is_empty(), "{text:?}: {:?}", check(text));
        }
        for text in [
            "## Timeline\n- 2024-01-01 Event\n  ## Nested\n",
            "> ## Timeline\n> - 2024-01-01 Event\n",
            "# Other\n",
            "## Timeline\n",
            "### Timeline\n- 2024-01-01 Event\n",
            "## Timeline\nprose\n",
            "## Timeline\n1. 2024-01-01 Event\n",
            "## Timeline\n- 2023-02-29 Bad\n",
            "## Timeline\n- **2024-01-01** Bad\n",
            "## Timeline\n- 2024-01-01 Old\n- 2024-02-01 New\n",
            "## Timeline\n### Nested\n- 2024-01-01 Event\n",
            "## Timeline\n---\n",
        ] {
            assert!(!check(text).is_empty(), "{text:?}");
        }
        let findings = check("---\nx: 1\n---\n## Timeline\n- 2024-02-30 Bad\n");
        assert_eq!(findings[0].line, Some(5));
    }

    #[test]
    fn exact_calendar_dates() {
        for date in ["2000-02-29 yes", "2024-12-31", "0001-01-01\n"] {
            assert!(date_prefix(date).is_some(), "{date}");
        }
        for date in [
            "1900-02-29",
            "0000-01-01",
            "2024-00-01",
            "2024-13-01",
            "2024-01-00",
            "2024-04-31",
            "2024-1-01",
            "2024-01-010",
            "2024-01-01: bad",
            "中文中文中文",
        ] {
            assert!(date_prefix(date).is_none(), "{date}");
        }
    }

    #[test]
    fn alternative_folder_template_and_invalid_configuration() {
        let yaml = "documents: {include: ['**/*.md']}\nsections:\n- include: 'projects/**'\n  heading: Milestones\n  required: true\n  list:\n    ordered: true\n    minimum_items: 2\n    date_order: ascending\n    item_pattern: '^\\d{4}-\\d{2}-\\d{2} M[0-9]+: '\n";
        let config = Config::from_yaml(yaml).unwrap();
        for (text, valid) in [
            (
                "# Milestones\n1. 2024-01-01 M1: Start\n2. 2024-02-01 M2: End\n",
                true,
            ),
            (
                "# Milestones\n1. 2024-01-01 Wrong\n2. 2024-02-01 M2: End\n",
                false,
            ),
            (
                "# Milestones\n1. 2024-02-01 M2: End\n2. 2024-01-01 M1: Start\n",
                false,
            ),
            ("# Milestones\n1. 2024-01-01 M1: Start\n", false),
        ] {
            assert_eq!(
                crate::markdown::validate_corpus(
                    &config,
                    &HashMap::from([("projects/a.md".into(), text.into())]),
                    &HashMap::new()
                )
                .is_ok(),
                valid
            );
        }
        for setting in [
            "level: 7",
            "level: 0",
            "list: {item_pattern: '['}",
            "list: {date_order: random}",
            "list: {unknown: true}",
        ] {
            assert!(
                Config::from_yaml(&format!(
                    "documents: {{include: ['**/*.md']}}\nsections:\n- heading: Any\n  {setting}\n"
                ))
                .is_err()
            );
        }
    }
}
