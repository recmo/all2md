use std::{
    collections::{HashMap, HashSet},
    ops::Range,
    path::Path,
};

use crate::config::validate_json_pointer;
use anyhow::{Context, Result, bail};
use pulldown_cmark::{Event, Tag};
use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::{SectionListRule, markdown::Finding};

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Template {
    frontmatter: Option<serde_json::Value>,
    filename: Option<FilenameRule>,
    pub(crate) markdown: crate::MarkdownConfig,
    pub(crate) links: crate::LinkConfig,
    pub(crate) relations: Vec<crate::RelationRule>,
    pub(crate) metadata: std::collections::BTreeMap<String, String>,
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
    dated_list: Option<DatedList>,
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

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FilenameRule {
    pattern: String,
    #[serde(default)]
    serial_scope: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct DatedList {
    order: String,
    min_items: usize,
    allow_equal_timestamps: bool,
}

pub(crate) fn is_template(path: &str) -> bool {
    Path::new(path)
        .file_name()
        .is_some_and(|name| name == "template.md")
}

pub(crate) struct Templates {
    entries: HashMap<String, CompiledTemplate>,
    default: Template,
}

struct CompiledTemplate {
    template: Template,
    schema: Option<jsonschema::Validator>,
    definition: serde_json::Value,
    script: crate::template_script::Script,
    markdown_source: String,
}

impl Templates {
    pub(crate) fn compile(files: &HashMap<String, String>) -> Result<Self, Vec<Finding>> {
        let mut entries = HashMap::new();
        let mut findings = Vec::new();
        for (path, text) in files.iter().filter(|(path, _)| is_template(path)) {
            let result = crate::template_script::Script::compile(path, text).and_then(
                |(script, definition)| compile_definition(definition, script, text.clone()),
            );
            match result {
                Ok(template) => {
                    entries.insert(path.clone(), template);
                }
                Err(error) => findings.push(Finding {
                    path: path.clone(),
                    line: None,
                    message: format!("invalid template: {error}"),
                }),
            }
        }
        if findings.is_empty() {
            Ok(Self {
                entries,
                default: Template::default(),
            })
        } else {
            Err(findings)
        }
    }

    fn applicable(&self, path: &str) -> Option<(String, &CompiledTemplate)> {
        let mut directory = Path::new(path).parent()?;
        loop {
            let candidate = directory.join("template.md").to_string_lossy().into_owned();
            if let Some(template) = self.entries.get(&candidate) {
                return Some((candidate, template));
            }
            directory = directory.parent()?;
        }
    }

    pub(crate) fn policy(&self, path: &str) -> &Template {
        self.applicable(path)
            .map_or(&self.default, |(_, entry)| &entry.template)
    }

    pub(crate) fn discovery(&self, path: &str) -> Option<serde_json::Value> {
        self.applicable(path).map(|(path, entry)| {
            serde_json::json!({
                "path": path, "definition": entry.definition, "content": entry.markdown_source
            })
        })
    }

    pub(crate) fn validate_page(
        &self,
        path: &str,
        text: &str,
        page: &crate::markdown::ParsedPage,
        findings: &mut Vec<Finding>,
    ) {
        let Some((template_path, entry)) = self.applicable(path) else {
            return;
        };
        let initial = findings.len();
        validate_page(path, text, page, &template_path, entry, findings);
        if initial == findings.len() {
            let doc = crate::template_script::document(path, text, page);
            if let Err(error) = entry.script.check(None, Some(&doc), false) {
                findings.push(crate::template_script::finding(
                    path,
                    text,
                    &template_path,
                    &error,
                ));
            }
        }
    }

    pub(crate) fn validate_changes(
        &self,
        before: &HashMap<String, String>,
        after: &HashMap<String, String>,
    ) -> Result<(), Vec<Finding>> {
        let mut paths: std::collections::BTreeSet<_> = before.keys().collect();
        paths.extend(after.keys());
        let mut findings = Vec::new();
        for path in paths {
            if before.get(path) == after.get(path) {
                continue;
            }
            let Some((template_path, entry)) = self.applicable(path) else {
                continue;
            };
            let script = &entry.script;
            if !script.has_change_checks {
                continue;
            }
            let parse = |pages: &HashMap<String, String>| {
                pages
                    .get(path)
                    .map(|text| crate::markdown::parse_page(text, &entry.template.links))
                    .transpose()
            };
            let result = (|| {
                let old = parse(before)?;
                let new = parse(after)?;
                let old = old
                    .as_ref()
                    .map(|page| crate::template_script::document(path, &before[path], page));
                let new = new
                    .as_ref()
                    .map(|page| crate::template_script::document(path, &after[path], page));
                script.check(old.as_ref(), new.as_ref(), true)
            })();
            if let Err(error) = result {
                let text = after
                    .get(path)
                    .or_else(|| before.get(path))
                    .map_or("", String::as_str);
                findings.push(crate::template_script::finding(
                    path,
                    text,
                    &template_path,
                    &error,
                ));
            }
        }
        if findings.is_empty() {
            Ok(())
        } else {
            Err(findings)
        }
    }

    /// Allocation is called under the repository lock, after receipt recovery.
    pub(crate) fn allocate_path(
        &self,
        requested: &str,
        existing: &HashSet<String>,
    ) -> Result<String> {
        if !requested.contains(['{', '}']) {
            return Ok(requested.to_owned());
        }
        let placeholder = Regex::new(r"\{serial(?::0?([1-9][0-9]?))?\}").expect("constant pattern");
        let captures = placeholder
            .captures(requested)
            .context("unknown path placeholder")?;
        let marker = captures.get(0).expect("whole match");
        let prefix = &requested[..marker.start()];
        let suffix = &requested[marker.end()..];
        if prefix.contains(['{', '}']) || suffix.contains(['{', '}']) {
            bail!("only one serial placeholder is allowed");
        }
        let width: usize = captures
            .get(1)
            .map_or(Ok(1), |value| value.as_str().parse())?;
        if width > 12 {
            bail!("serial padding must not exceed 12 digits");
        }
        let probe = format!("{prefix}{:0width$}{suffix}", 1);
        let policy = self.policy(&probe);
        let rule = policy
            .filename
            .as_ref()
            .context("serial allocation requires a filename declaration")?;
        let pattern = Regex::new(&rule.pattern)?;
        let target = pattern
            .captures(&probe)
            .context("path does not match template filename pattern")?;
        if target.get(0).is_none_or(|found| found.as_str() != probe) {
            bail!("path does not match template filename pattern");
        }
        let serial_capture = target
            .name("serial")
            .context("serial allocation requires a named serial capture")?;
        if serial_capture.start() != prefix.len()
            || serial_capture.end() != probe.len() - suffix.len()
        {
            bail!("serial placeholder must occupy the named serial capture");
        }
        let mut maximum = 0_u64;
        for path in existing {
            if let Some(found) = pattern.captures(path)
                && found.get(0).is_some_and(|matched| matched.as_str() == path)
                && rule.serial_scope.iter().all(|key| {
                    found.name(key).map(|v| v.as_str()) == target.name(key).map(|v| v.as_str())
                })
            {
                let serial: u64 = found
                    .name("serial")
                    .context("filename requires serial capture")?
                    .as_str()
                    .parse()?;
                maximum = maximum.max(serial);
            }
        }
        let serial = maximum.checked_add(1).context("serial counter exhausted")?;
        let path = format!("{prefix}{serial:0width$}{suffix}");
        let final_match = pattern
            .find(&path)
            .context("allocated path violates filename pattern")?;
        if final_match.start() != 0 || final_match.end() != path.len() || existing.contains(&path) {
            bail!("allocated path violates filename pattern or already exists");
        }
        Ok(path)
    }
}

struct LocalSchemaOnly;

impl jsonschema::Retrieve for LocalSchemaOnly {
    fn retrieve(
        &self,
        uri: &jsonschema::Uri<String>,
    ) -> Result<serde_json::Value, Box<dyn std::error::Error + Send + Sync>> {
        Err(format!("external schema references are forbidden: {uri}").into())
    }
}

fn compile_definition(
    definition: serde_json::Value,
    script: crate::template_script::Script,
    markdown_source: String,
) -> Result<CompiledTemplate> {
    let template: Template = serde_json::from_value(definition.clone())?;
    if let Some(rule) = &template.filename {
        let pattern = Regex::new(&rule.pattern)?;
        let names: HashSet<_> = pattern.capture_names().flatten().collect();
        if !rule.serial_scope.is_empty() && !names.contains("serial") {
            bail!("serial_scope requires a named serial capture");
        }
        for key in &rule.serial_scope {
            if key == "serial" || !names.contains(key.as_str()) {
                bail!("invalid serial scope capture {key}");
            }
        }
    }
    validate_definition(&template.structure, &template.sections, 0)?;
    validate_rules(&template.preamble)?;
    if template.markdown.max_line_length == Some(0) {
        bail!("markdown.max_line_length must be greater than zero");
    }
    for wiki in &template.links.wiki {
        let pattern =
            Regex::new(wiki).with_context(|| format!("invalid wiki-link pattern {wiki:?}"))?;
        if !pattern
            .capture_names()
            .flatten()
            .any(|name| name == "target")
        {
            bail!("wiki-link pattern must define a named target capture");
        }
    }
    for pointer in template.metadata.values() {
        validate_json_pointer(pointer, "metadata")?;
    }
    let mut relation_names = HashSet::new();
    for relation in &template.relations {
        if relation.name.trim().is_empty() {
            bail!("relation names must be non-empty");
        }
        if !relation_names.insert(relation.name.as_str()) {
            bail!("duplicate relation name: {}", relation.name);
        }
        relation.selector.validate()?;
    }
    for relation in &template.relations {
        if let Some(reciprocal) = &relation.reciprocal
            && !relation_names.contains(reciprocal.as_str())
        {
            bail!(
                "relation {} references unknown reciprocal relation {reciprocal}",
                relation.name
            );
        }
    }

    let schema = template
        .frontmatter
        .as_ref()
        .map(|schema| {
            jsonschema::options()
                .with_retriever(LocalSchemaOnly)
                .build(schema)
                .map_err(|error| anyhow::anyhow!("{error}"))
        })
        .transpose()?;
    Ok(CompiledTemplate {
        template,
        schema,
        definition,
        script,
        markdown_source,
    })
}

fn validate_rules(rules: &Rules) -> Result<()> {
    if let Some(list) = &rules.dated_list {
        if !matches!(list.order.as_str(), "ascending" | "descending") {
            bail!("dated_list requires ascending or descending order");
        }
        if rules.content != Some(Content::List) || rules.list.is_some() {
            bail!("dated_list requires list content and cannot be combined with list rules");
        }
    }
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

fn validate_page(
    path: &str,
    text: &str,
    page: &crate::markdown::ParsedPage,
    template_path: &str,
    entry: &CompiledTemplate,
    findings: &mut Vec<Finding>,
) {
    let template = &entry.template;
    if let Some(rule) = &template.filename {
        let pattern = Regex::new(&rule.pattern).expect("validated filename pattern");
        if !pattern
            .find(path)
            .is_some_and(|found| found.start() == 0 && found.end() == path.len())
        {
            findings.push(Finding {
                path: path.into(),
                line: None,
                message: format!(
                    "{template_path}: path does not match filename pattern {}",
                    rule.pattern
                ),
            });
        }
    }
    if let Some(validator) = &entry.schema {
        for error in validator.iter_errors(&page.frontmatter) {
            findings.push(Finding {
                path: path.to_owned(),
                line: Some(crate::template_script::field_line(
                    text,
                    error
                        .instance_path
                        .to_string()
                        .trim_start_matches('/')
                        .split('/')
                        .next()
                        .unwrap_or(""),
                )),
                message: format!(
                    "{template_path}{}: frontmatter{}: {error}",
                    entry.script.rule_context("frontmatter"),
                    error.instance_path
                ),
            });
        }
    }
    let mut offsets = vec![0];
    offsets.extend(text.match_indices('\n').map(|(offset, _)| offset + 1));
    let body_start = offsets
        .get(page.body_start_line - 1)
        .copied()
        .unwrap_or(text.len());
    let events = &page.events;
    let headings = &page.section_headings;
    let mut report = |offset: usize, message: String| {
        findings.push(Finding {
            path: path.to_owned(),
            line: Some(offsets.partition_point(|start| *start <= offset)),
            message: {
                let context = template
                    .sections
                    .iter()
                    .find(|section| message.contains(&format!("{:?}", section.heading)))
                    .map(|section| entry.script.rule_context(&section.heading))
                    .unwrap_or_default();
                format!("{template_path}{context}: {message}")
            },
        })
    };
    let first_section = headings
        .iter()
        .position(|heading| heading.0 >= template.structure.level.unwrap_or(1))
        .unwrap_or(headings.len());
    check_sections(
        text,
        events,
        &headings[first_section..],
        body_start,
        text.len(),
        &template.structure,
        &template.sections,
        &template.preamble,
        &page.list_entries,
        0,
        &offsets,
        &mut report,
    );
}

type Heading = (u8, String, usize, usize);

#[allow(clippy::too_many_arguments)]
fn check_sections(
    text: &str,
    events: &[(Event<'_>, Range<usize>)],
    headings: &[Heading],
    start: usize,
    end: usize,
    structure: &Structure,
    sections: &[Section],
    own_rules: &Rules,
    entries: &[crate::markdown::ListEntry],
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
        events,
        start,
        own_rules,
        entries,
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
            let mut section_report =
                |offset, message| report(offset, format!("section {name:?}: {message}"));
            check_sections(
                text,
                events,
                &headings[index + 1..next],
                *content_start,
                section_end,
                &section.structure,
                &section.sections,
                &section.rules,
                entries,
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
                format!("required section {:?} is missing", section.heading),
            );
        }
    }
}

fn check_content(
    text: &str,
    events: &[(Event<'_>, Range<usize>)],
    offset: usize,
    rules: &Rules,
    entries: &[crate::markdown::ListEntry],
    offsets: &[usize],
    report: &mut dyn FnMut(usize, String),
) {
    let mut rendered = String::new();
    let mut paragraphs = 0;
    let mut items = 0;
    let mut depth = 0_usize;
    for (event, _) in events
        .iter()
        .filter(|(_, range)| range.start >= offset && range.end <= offset + text.len())
    {
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
            Event::Text(value) | Event::Code(value) => rendered.push_str(value),
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
    if let Some(list) = &rules.dated_list {
        let list_rule = SectionListRule {
            ordered: Some(false),
            minimum_items: list.min_items,
            date_order: None,
            item_pattern: None,
        };
        crate::structure::validate_list(text, offset, offsets, &list_rule, &mut |line, message| {
            report(offsets[line.saturating_sub(1)], message)
        });
        validate_timestamps(text, offset, entries, list, report);
    }
    if let Some(list) = &rules.list {
        crate::structure::validate_list(text, offset, offsets, list, &mut |line, message| {
            report(offsets[line.saturating_sub(1)], message)
        });
    }
}

fn validate_timestamps(
    text: &str,
    offset: usize,
    entries: &[crate::markdown::ListEntry],
    rule: &DatedList,
    report: &mut dyn FnMut(usize, String),
) {
    let mut previous = None;
    for entry in entries
        .iter()
        .filter(|entry| entry.range.start >= offset && entry.range.end <= offset + text.len())
    {
        let Some(date) = entry.timestamp else {
            report(
                entry.range.start,
                "timeline entry must start with a literal RFC3339 timestamp".into(),
            );
            continue;
        };
        if previous.is_some_and(|prev| {
            (if rule.order == "ascending" {
                date < prev
            } else {
                date > prev
            }) || (!rule.allow_equal_timestamps && date == prev)
        }) {
            report(
                entry.range.start,
                "timeline timestamp is out of order".into(),
            );
        }
        previous = Some(date);
        if text[entry.text_start - offset..entry.range.end - offset]
            .trim()
            .is_empty()
        {
            report(
                entry.range.start,
                "timeline entry must explain what happened".into(),
            );
        }
    }
}

#[cfg(test)]
pub(crate) fn test_templates(text: &str) -> Result<Templates, Vec<Finding>> {
    Templates::compile(&HashMap::from([("template.md".into(), text.into())]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relation_names_and_reciprocals_are_closed() {
        for declarations in [
            "relation('related', selector={'kind': 'markdown_links'})\nrelation('related', selector={'kind': 'markdown_links'})",
            "relation('parent', selector={'kind': 'markdown_links'}, reciprocal='child')",
        ] {
            assert!(test_templates(&format!("```starlark\n{declarations}\n```\n")).is_err());
        }
    }

    #[test]
    fn wiki_link_patterns_define_their_grammar() {
        let config =
            |wiki: &str| test_templates(&format!("```starlark\nlinks(wiki=[{wiki:?}])\n```\n"));
        assert!(config(r"\{\{(?P<target>[^}]+)\}\}").is_ok());
        assert!(config("[").is_err());
        assert!(config(r"\{\{([^}]+)\}\}").is_err());
    }

    const PEOPLE: &str = r#"Write established facts.

```starlark
structure(level=2, order="enforced", additional_sections=False)
section("Summary", required=True, content="paragraphs", paragraphs={"minimum": 1, "maximum": 1}, words={"maximum": 5}, level=2)
section("Timeline", required=True, list={"minimum_items": 1, "date_order": "descending"}, level=2)
```
"#;

    fn check(text: &str, template: &str) -> Vec<Finding> {
        let templates = test_templates(template).unwrap();
        crate::markdown::validate_corpus(
            &HashMap::from([("people/a.md".into(), text.into())]),
            &templates,
        )
        .err()
        .unwrap_or_default()
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
            ("template.md".into(), PEOPLE.into()),
            (
                "people/template.md".into(),
                "Different.\n\n```starlark\nstructure(additional_sections=True)\n```\n".into(),
            ),
        ]);
        let templates = Templates::compile(&files).unwrap();
        let discovery = templates.discovery("people/new.md").unwrap();
        assert_eq!(discovery["path"], "people/template.md");
        assert!(
            discovery["content"]
                .as_str()
                .unwrap()
                .starts_with("Different.")
        );
        assert!(
            crate::markdown::validate_corpus(
                &HashMap::from([("people/new.md".into(), "# Any\n".into())]),
                &templates,
            )
            .is_ok()
        );
        assert!(
            Templates::compile(&HashMap::new())
                .unwrap()
                .discovery("new.md")
                .is_none()
        );
    }

    #[test]
    fn recursive_sections_and_own_content_limits() {
        let template = r#"```starlark
structure(level=2)
section("Overview", required=True, words={"maximum": 1}, level=2)
section("Details", required=True, nonempty=True, level=3, parent=["Overview"])
```
"#;
        let text = "## Overview\nOne\n### Details\nMany more words here.\n";
        assert!(check(text, template).is_empty());
        assert!(
            !check(
                text,
                &template.replace(
                    "required=True, words",
                    "include_subsections=True, required=True, words"
                )
            )
            .is_empty()
        );
        assert!(!check("## Overview\nOne\n", template).is_empty());
        for template in [
            "```starlark\nstructure(additional_sections=False)\nsection(\"A\", level=1)\nsection(\"A\", level=1)\n```\n",
            r#"```starlark
structure(additional_sections=False)
section("A", words={"minimum": 4, "maximum": 1}, level=1)
```
"#,
            "```starlark\nstructure(level=7)\n```\n",
            "```starlark\nunknown()\n```\n",
        ] {
            assert!(test_templates(template).is_err());
        }
    }

    #[test]
    fn inline_markup_does_not_inflate_lengths() {
        let template = r#"```starlark
structure(level=2)
section("Summary", required=True, words={"maximum": 1}, characters={"maximum": 6}, level=2)
```
"#;
        assert!(check("## Summary\nfoo**bar**\n", template).is_empty());
        assert!(!check("## Summary\nfoo**bars**\n", template).is_empty());
    }

    #[test]
    fn section_lengths_preserve_document_wide_reference_definitions() {
        let template = r#"```starlark
structure(level=2, additional_sections=True)
section("Summary", required=True, content="paragraphs", paragraphs={"minimum": 1, "maximum": 1}, words={"maximum": 1}, characters={"maximum": 5}, level=2)
```
"#;
        for text in [
            "## Summary\n[Alice][person]\n## Sources\n[person]: https://example.com\n",
            "[person]: https://example.com\n\n## Summary\n[Alice][person]\n",
            "## Summary\n[Alice][]\n## Sources\n[Alice]: https://example.com\n",
            "## Summary\n[Alice]\n## Sources\n[Alice]: https://example.com\n",
        ] {
            assert!(
                check(text, template).is_empty(),
                "{text:?}: {:?}",
                check(text, template)
            );
        }
        assert!(
            !check(
                "## Summary\n[Alicia][person]\n## Sources\n[person]: https://example.com\n",
                template
            )
            .is_empty()
        );
    }
}
