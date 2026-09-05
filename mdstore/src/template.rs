use std::{
    collections::{HashMap, HashSet},
    path::Path,
};

use anyhow::{Context, Result, bail};
use pulldown_cmark::{Event, Options, Parser, Tag};
use serde::{Deserialize, Serialize};

use crate::{SectionListRule, markdown::Finding};

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Template {
    instructions: String,
    examples: Vec<String>,
    structure: Structure,
    preamble: Rules,
    sections: Vec<Section>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Structure {
    level: Option<u8>,
    order: Order,
    additional_sections: bool,
}

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Order {
    Enforced,
    #[default]
    Unrestricted,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Section {
    heading: String,
    #[serde(default)]
    instructions: String,
    #[serde(default)]
    examples: Vec<String>,
    #[serde(default)]
    rules: Rules,
    #[serde(default)]
    structure: Structure,
    #[serde(default)]
    sections: Vec<Self>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Rules {
    required: bool,
    nonempty: bool,
    content: Option<Content>,
    paragraphs: Bounds,
    words: Bounds,
    characters: Bounds,
    list_items: Bounds,
    include_subsections: bool,
    list: Option<SectionListRule>,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum Content {
    Paragraphs,
    List,
    Table,
    Code,
    Blockquotes,
    Empty,
}

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Bounds {
    minimum: Option<usize>,
    maximum: Option<usize>,
}

pub(crate) fn is_template(path: &str) -> bool {
    Path::new(path)
        .file_name()
        .is_some_and(|name| name == "template.yaml")
}

fn applicable<'a>(path: &str, files: &'a HashMap<String, String>) -> Option<(String, &'a str)> {
    let mut directory = Path::new(path).parent()?;
    loop {
        let candidate = directory
            .join("template.yaml")
            .to_string_lossy()
            .into_owned();
        if let Some(text) = files.get(&candidate) {
            return Some((candidate, text));
        }
        directory = directory.parent()?;
    }
}

fn parse(text: &str) -> Result<Template> {
    let template: Template = serde_yaml::from_str(text)?;
    validate_definition(&template.structure, &template.sections, 0)?;
    validate_rules(&template.preamble)?;
    Ok(template)
}

fn validate_rules(rules: &Rules) -> Result<()> {
    for bounds in [
        &rules.paragraphs,
        &rules.words,
        &rules.characters,
        &rules.list_items,
    ] {
        if matches!((bounds.minimum, bounds.maximum), (Some(min), Some(max)) if min > max) {
            bail!("minimum exceeds maximum");
        }
    }
    if let Some(list) = &rules.list {
        if let Some(pattern) = &list.item_pattern {
            let _ = regex::Regex::new(pattern)?;
        }
        if rules
            .content
            .as_ref()
            .is_some_and(|content| *content != Content::List)
        {
            bail!("list rules require list content");
        }
    }
    Ok(())
}

fn validate_definition(
    structure: &Structure,
    sections: &[Section],
    parent_level: u8,
) -> Result<()> {
    let level = structure.level.unwrap_or(parent_level + 1);
    if !sections.is_empty() && (level <= parent_level || level > 6) {
        bail!("section level must be greater than its parent and at most 6");
    }
    if structure
        .level
        .is_some_and(|level| !(1..=6).contains(&level))
    {
        bail!("section level must be between 1 and 6");
    }
    let mut names = HashSet::new();
    for section in sections {
        if section.heading.trim().is_empty() || !names.insert(&section.heading) {
            bail!("section headings must be nonempty and unique among siblings");
        }
        validate_rules(&section.rules)?;
        validate_definition(&section.structure, &section.sections, level)?;
    }
    Ok(())
}

pub(crate) fn discovery(
    path: &str,
    files: &HashMap<String, String>,
) -> Result<Option<serde_json::Value>> {
    applicable(path, files)
        .map(|(path, text)| {
            let _ = parse(text).with_context(|| format!("invalid {path}"))?;
            let definition: serde_yaml::Value = serde_yaml::from_str(text)?;
            Ok(serde_json::json!({"path": path, "definition": definition}))
        })
        .transpose()
}

pub(crate) fn validate_corpus(
    pages: &HashMap<String, String>,
    files: &HashMap<String, String>,
    findings: &mut Vec<Finding>,
) {
    let mut templates = HashMap::new();
    for (path, text) in files.iter().filter(|(path, _)| is_template(path)) {
        match parse(text) {
            Ok(template) => {
                templates.insert(path.clone(), template);
            }
            Err(error) => findings.push(Finding {
                path: path.clone(),
                line: None,
                message: format!("invalid template: {error}"),
            }),
        }
    }
    for (path, text) in pages {
        let Some((template_path, _)) = applicable(path, files) else {
            continue;
        };
        let Some(template) = templates.get(&template_path) else {
            continue;
        };
        let Ok(page) = crate::markdown::parse_page(text, &crate::LinkConfig::default()) else {
            continue;
        };
        let mut offsets = vec![0];
        offsets.extend(text.match_indices('\n').map(|(offset, _)| offset + 1));
        let body_start = offsets
            .get(page.body_start_line - 1)
            .copied()
            .unwrap_or(text.len());
        let body = &text[body_start..];
        let mut headings = Vec::new();
        let mut depth = 0_usize;
        for (event, range) in Parser::new_ext(body, Options::all()).into_offset_iter() {
            match event {
                Event::Start(tag) => {
                    if let Tag::Heading { level, .. } = tag
                        && depth == 0
                    {
                        let line =
                            offsets.partition_point(|offset| *offset <= body_start + range.start);
                        if let Some(heading) =
                            page.headings.iter().find(|heading| heading.line == line)
                        {
                            headings.push((
                                level as u8,
                                heading.text.clone(),
                                body_start + range.start,
                                body_start + range.end,
                            ));
                        }
                    }
                    depth += 1;
                }
                Event::End(_) => depth -= 1,
                _ => {}
            }
        }
        let mut report = |offset: usize, message: String| {
            findings.push(Finding {
                path: path.clone(),
                line: Some(offsets.partition_point(|start| *start <= offset)),
                message: format!("{template_path}: {message}"),
            })
        };
        let first_section = headings
            .iter()
            .position(|heading| heading.0 >= template.structure.level.unwrap_or(1))
            .unwrap_or(headings.len());
        check_sections(
            text,
            &headings[first_section..],
            body_start,
            text.len(),
            &template.structure,
            &template.sections,
            &template.preamble,
            0,
            &offsets,
            &mut report,
        );
    }
}

type Heading = (u8, String, usize, usize);

#[allow(clippy::too_many_arguments)]
fn check_sections(
    text: &str,
    headings: &[Heading],
    start: usize,
    end: usize,
    structure: &Structure,
    sections: &[Section],
    own_rules: &Rules,
    parent: u8,
    offsets: &[usize],
    report: &mut dyn FnMut(usize, String),
) {
    let level = structure.level.unwrap_or(parent + 1);
    let own_end = headings.first().map_or(end, |heading| heading.2);
    check_content(
        &text[start..if own_rules.include_subsections {
            end
        } else {
            own_end
        }],
        start,
        own_rules,
        offsets,
        report,
    );
    let mut seen = HashSet::new();
    let mut previous = None;
    let mut index = 0;
    while index < headings.len() {
        let (actual_level, name, heading_start, content_start) = &headings[index];
        let next = (index + 1..headings.len())
            .find(|next| headings[*next].0 <= *actual_level)
            .unwrap_or(headings.len());
        let section_end = headings.get(next).map_or(end, |heading| heading.2);
        if let Some((position, section)) = sections
            .iter()
            .enumerate()
            .find(|(_, section)| section.heading == *name)
        {
            if *actual_level != level {
                report(
                    *heading_start,
                    format!("section {name:?} requires heading level {level}"),
                );
            }
            if !seen.insert(position) {
                report(*heading_start, format!("duplicate section {name:?}"));
            }
            if matches!(structure.order, Order::Enforced)
                && previous.is_some_and(|previous| position < previous)
            {
                report(*heading_start, format!("section {name:?} is out of order"));
            }
            previous = Some(position);
            let mut section_report = |offset, message| {
                report(
                    offset,
                    format!(
                        "section {name:?}: {message}{}",
                        if section.instructions.is_empty() {
                            String::new()
                        } else {
                            format!("; guidance: {}", section.instructions)
                        }
                    ),
                )
            };
            check_sections(
                text,
                &headings[index + 1..next],
                *content_start,
                section_end,
                &section.structure,
                &section.sections,
                &section.rules,
                *actual_level,
                offsets,
                &mut section_report,
            );
        } else if !structure.additional_sections {
            report(*heading_start, format!("unexpected section {name:?}"));
        }
        index = next;
    }
    for (position, section) in sections.iter().enumerate() {
        if section.rules.required && !seen.contains(&position) {
            report(
                start,
                format!(
                    "required section {:?} is missing; guidance: {}",
                    section.heading, section.instructions
                ),
            );
        }
    }
}

fn check_content(
    text: &str,
    offset: usize,
    rules: &Rules,
    offsets: &[usize],
    report: &mut dyn FnMut(usize, String),
) {
    let mut rendered = String::new();
    let mut paragraphs = 0;
    let mut items = 0;
    let mut depth = 0_usize;
    for (event, _) in Parser::new_ext(text, Options::all()).into_offset_iter() {
        match event {
            Event::Start(tag) => {
                if matches!(tag, Tag::Paragraph) {
                    paragraphs += 1;
                }
                if matches!(tag, Tag::Item) {
                    items += 1;
                }
                if depth == 0
                    && let Some(content) = &rules.content
                {
                    let allowed = matches!(
                        (content, &tag),
                        (Content::Paragraphs, Tag::Paragraph)
                            | (Content::List, Tag::List(_))
                            | (Content::Table, Tag::Table(_))
                            | (Content::Code, Tag::CodeBlock(_))
                            | (Content::Blockquotes, Tag::BlockQuote(_))
                    );
                    if !allowed {
                        report(offset, "content block type is not allowed".into());
                    }
                }
                depth += 1;
            }
            Event::End(tag) => {
                depth -= 1;
                if matches!(
                    tag,
                    pulldown_cmark::TagEnd::Paragraph
                        | pulldown_cmark::TagEnd::Heading(_)
                        | pulldown_cmark::TagEnd::Item
                        | pulldown_cmark::TagEnd::CodeBlock
                        | pulldown_cmark::TagEnd::TableCell
                ) {
                    rendered.push(' ');
                }
            }
            Event::Text(value) | Event::Code(value) => rendered.push_str(&value),
            Event::SoftBreak | Event::HardBreak => rendered.push(' '),
            _ if depth == 0 && rules.content.is_some() => {
                report(offset, "content block type is not allowed".into())
            }
            _ => {}
        }
    }
    if rules.nonempty && rendered.trim().is_empty() {
        report(offset, "content must not be empty".into());
    }
    for (name, value, bounds) in [
        ("paragraphs", paragraphs, &rules.paragraphs),
        ("words", rendered.split_whitespace().count(), &rules.words),
        (
            "characters",
            rendered.trim().chars().count(),
            &rules.characters,
        ),
        ("list_items", items, &rules.list_items),
    ] {
        if bounds.minimum.is_some_and(|minimum| value < minimum)
            || bounds.maximum.is_some_and(|maximum| value > maximum)
        {
            report(
                offset,
                format!("{name} count {value} is outside configured bounds"),
            );
        }
    }
    if let Some(list) = &rules.list {
        crate::structure::validate_list(text, offset, offsets, list, &mut |line, message| {
            report(offsets[line.saturating_sub(1)], message)
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PEOPLE: &str = "instructions: Write established facts.\nstructure: {level: 2, order: enforced, additional_sections: false}\nsections:\n- heading: Summary\n  instructions: One concise paragraph.\n  rules:\n    required: true\n    content: paragraphs\n    paragraphs: {minimum: 1, maximum: 1}\n    words: {maximum: 5}\n- heading: Timeline\n  rules:\n    required: true\n    list: {minimum_items: 1, date_order: descending}\n";

    fn check(text: &str, template: &str) -> Vec<Finding> {
        let mut findings = Vec::new();
        validate_corpus(
            &HashMap::from([("people/a.md".into(), text.into())]),
            &HashMap::from([("people/template.yaml".into(), template.into())]),
            &mut findings,
        );
        findings
    }

    #[test]
    fn people_structure_and_length_rules() {
        let good =
            "# Alice\n\n## Summary\nA **good** person.\n\n## Timeline\n- 2024-01-01 Met Alice.\n";
        assert!(check(good, PEOPLE).is_empty(), "{:?}", check(good, PEOPLE));
        for bad in [
            good.replace("A **good** person.", "One.\n\nTwo."),
            good.replace("A **good** person.", "One two three four five six."),
            good.replace("## Summary", "### Summary"),
            good.replace("## Summary", "## Other"),
            format!("{good}\n## Extra\n"),
            good.replace("2024-01-01", "2024-02-30"),
            good.replace("A **good** person.", "- One item"),
        ] {
            assert!(!check(&bad, PEOPLE).is_empty(), "{bad}");
        }
        let reversed = "## Timeline\n- 2024-01-01 Met Alice\n## Summary\nOne.\n";
        assert!(!check(reversed, PEOPLE).is_empty());
    }

    #[test]
    fn closest_template_replaces_parent_and_discovery_preserves_guidance() {
        let files = HashMap::from([
            ("template.yaml".into(), PEOPLE.into()),
            (
                "people/template.yaml".into(),
                "instructions: Different.\nstructure: {additional_sections: true}".into(),
            ),
        ]);
        let discovery = discovery("people/new.md", &files).unwrap().unwrap();
        assert_eq!(discovery["path"], "people/template.yaml");
        assert_eq!(discovery["definition"]["instructions"], "Different.");
        let mut findings = Vec::new();
        validate_corpus(
            &HashMap::from([("people/new.md".into(), "# Any\n".into())]),
            &files,
            &mut findings,
        );
        assert!(findings.is_empty());
        assert!(
            super::discovery("new.md", &HashMap::new())
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn recursive_sections_and_own_content_limits() {
        let template = "structure: {level: 2}\nsections:\n- heading: Overview\n  rules: {required: true, words: {maximum: 1}}\n  sections:\n  - heading: Details\n    rules: {required: true, nonempty: true}\n";
        let text = "## Overview\nOne\n### Details\nMany more words here.\n";
        assert!(check(text, template).is_empty());
        assert!(
            !check(
                text,
                &template.replace(
                    "required: true, words",
                    "include_subsections: true, required: true, words"
                )
            )
            .is_empty()
        );
        assert!(!check("## Overview\nOne\n", template).is_empty());
        for template in [
            "sections: [{heading: A}, {heading: A}]",
            "sections: [{heading: A, rules: {words: {minimum: 4, maximum: 1}}}]",
            "structure: {level: 7}",
            "unknown: true",
        ] {
            assert!(parse(template).is_err());
        }
    }

    #[test]
    fn inline_markup_does_not_inflate_lengths() {
        let template = "structure: {level: 2}\nsections:\n- heading: Summary\n  rules: {required: true, words: {maximum: 1}, characters: {maximum: 6}}\n";
        assert!(check("## Summary\nfoo**bar**\n", template).is_empty());
        assert!(!check("## Summary\nfoo**bars**\n", template).is_empty());
    }
}
