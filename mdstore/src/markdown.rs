use std::{collections::HashMap, ops::Range, path::Path};

use anyhow::{Context, Result};
use pulldown_cmark::{Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::config::{RelationLinkSyntax, RelationSelector};
use crate::template::{Template, Templates};

#[derive(Debug, Clone, Serialize, Deserialize)]
/// Parsed structural and authored-link information for one page.
pub struct ParsedPage {
    /// Metadata projected using the page's directory template.
    pub metadata: serde_json::Value,
    /// YAML frontmatter converted to JSON.
    pub frontmatter: serde_json::Value,
    /// First body line after frontmatter, one-based.
    pub body_start_line: usize,
    /// Parsed headings in source order.
    pub headings: Vec<Heading>,
    #[serde(skip)]
    pub(crate) events: Vec<(Event<'static>, Range<usize>)>,
    #[serde(skip)]
    pub(crate) section_headings: Vec<(u8, String, usize, usize)>,
    #[serde(skip)]
    pub(crate) list_entries: Vec<ListEntry>,
    /// Fenced and indented code block ranges.
    pub code_blocks: Vec<SourceRange>,
    /// Source lines that begin or end structural blocks.
    pub structural_boundaries: Vec<usize>,
    /// Authored Markdown and configured wiki links.
    pub links: Vec<RawLink>,
}

#[derive(Debug, Clone)]
pub(crate) struct ListEntry {
    pub range: Range<usize>,
    pub line: usize,
    pub timestamp: Option<chrono::DateTime<chrono::FixedOffset>>,
    pub text_start: usize,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
/// Inclusive one-based source line range.
pub struct SourceRange {
    /// First line.
    pub start_line: usize,
    /// Last line.
    pub end_line: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
/// A parsed Markdown heading.
pub struct Heading {
    /// Heading depth from one through six.
    pub level: u8,
    /// Plain heading text.
    pub text: String,
    /// One-based source line.
    pub line: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
/// An authored link before corpus target resolution.
pub struct RawLink {
    /// Raw authored target.
    pub target: String,
    /// Byte range of a configured wiki target in the complete document source.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_range: Option<Range<usize>>,
    /// One-based source line.
    pub line: usize,
    /// Syntax that produced the link.
    pub syntax: LinkSyntax,
    /// Containing heading breadcrumb.
    pub sections: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
/// Supported authored link syntax.
pub enum LinkSyntax {
    /// Standard Markdown link.
    Markdown,
    /// Repository-configured wiki link.
    Wiki,
}

impl RelationLinkSyntax {
    fn matches(self, syntax: LinkSyntax) -> bool {
        matches!(
            (self, syntax),
            (Self::Markdown, LinkSyntax::Markdown) | (Self::Wiki, LinkSyntax::Wiki)
        )
    }
}

struct ResolvedLink {
    target: String,
    syntax: LinkSyntax,
    sections: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
/// A resolved, typed relation edge between pages.
pub struct Edge {
    /// Source page path.
    pub source: String,
    /// Configured relation name.
    pub relation: String,
    /// Target page path.
    pub target: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
/// A structured corpus validation finding.
pub struct Finding {
    /// Schema source responsible for this finding, when known.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<FindingSource>,
    /// Page or configuration resource path.
    pub path: String,
    /// Human-readable validation message.
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    /// Optional one-based source line.
    pub line: Option<usize>,
}

/// A one-based source location in a schema document.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FindingSource {
    pub path: String,
    pub line: usize,
}

/// Parses one Markdown page according to configured link syntaxes.
pub fn parse_page(text: &str, links: &crate::config::LinkConfig) -> Result<ParsedPage> {
    let (frontmatter, body_start_line, body) = parse_frontmatter(text)?;
    let mut headings = Vec::new();
    let mut events = Vec::new();
    let mut section_headings = Vec::new();
    let mut list_entries = Vec::new();
    let mut depth = 0;
    let mut section_span = None;
    let body_offset = text.len() - body.len();
    let mut code_blocks = Vec::new();
    let mut structural_boundaries = Vec::new();
    let mut markdown_links = Vec::new();
    let mut wiki_exclusions = Vec::new();
    let mut heading: Option<(u8, String, usize)> = None;
    let mut heading_stack = Vec::new();
    let mut code_depth = 0_usize;
    let wiki = links
        .wiki
        .iter()
        .map(|wiki| Regex::new(wiki))
        .collect::<Result<Vec<_>, _>>()?;
    let parser = Parser::new_ext(body, Options::all()).into_offset_iter();
    for (event, range) in parser {
        let line = body_start_line
            + body[..range.start]
                .bytes()
                .filter(|byte| *byte == b'\n')
                .count();
        events.push((
            event.clone().into_static(),
            body_offset + range.start..body_offset + range.end,
        ));
        match &event {
            Event::Start(tag) => {
                if matches!(tag, Tag::Heading { .. }) && depth == 0 {
                    section_span = Some((body_offset + range.start, body_offset + range.end));
                }
                if matches!(tag, Tag::Item) && depth == 1 {
                    let raw = &body[range.clone()];
                    let marker = raw.find(char::is_whitespace).unwrap_or(raw.len());
                    let content = raw[marker..].trim_start();
                    let stamp = content.split_whitespace().next().unwrap_or("");
                    let timestamp = chrono::DateTime::parse_from_rfc3339(stamp).ok();
                    let narrative = if timestamp.is_some() {
                        content[stamp.len()..]
                            .trim_start()
                            .trim_start_matches('—')
                            .trim_start()
                    } else {
                        content
                    };
                    list_entries.push(ListEntry {
                        range: body_offset + range.start..body_offset + range.end,
                        line,
                        timestamp,
                        text_start: body_offset + range.end - narrative.len(),
                    });
                }
                depth += 1;
            }
            Event::End(_) => depth -= 1,
            _ => {}
        }
        match event {
            Event::Start(Tag::Heading { level, .. }) => {
                heading = Some((heading_level(level), String::new(), line));
            }
            Event::Start(Tag::CodeBlock(_)) => {
                code_depth += 1;
                wiki_exclusions.push(range.clone());
                let end_offset = range.end.saturating_sub(1);
                code_blocks.push(SourceRange {
                    start_line: line,
                    end_line: body_start_line
                        + body[..end_offset]
                            .bytes()
                            .filter(|byte| *byte == b'\n')
                            .count(),
                });
            }
            Event::Start(Tag::List(_) | Tag::Table(_)) => {
                let end_offset = range.end.saturating_sub(1);
                let end_line = body_start_line
                    + body.as_bytes()[..end_offset]
                        .iter()
                        .filter(|byte| **byte == b'\n')
                        .count();
                structural_boundaries.extend([line, end_line + 1]);
            }
            Event::Text(value) => {
                if let Some((_, text, _)) = &mut heading {
                    text.push_str(&value);
                }
                if code_depth > 0 {
                    wiki_exclusions.push(range.clone());
                }
            }
            Event::Code(value) => {
                wiki_exclusions.push(range.clone());
                if let Some((_, text, _)) = &mut heading {
                    text.push_str(&value);
                }
            }
            Event::End(TagEnd::Heading(_)) => {
                if let Some((level, text, line)) = heading.take() {
                    heading_stack.truncate(usize::from(level.saturating_sub(1)));
                    heading_stack.push(text.trim().into());
                    if let Some((start, end)) = section_span.take() {
                        section_headings.push((level, text.trim().into(), start, end));
                    }
                    headings.push(Heading {
                        level,
                        text: text.trim().into(),
                        line,
                    });
                }
            }
            Event::Start(Tag::Link { dest_url, .. }) if links.markdown => {
                markdown_links.push((
                    RawLink {
                        target: dest_url.to_string(),
                        target_range: None,
                        line,
                        syntax: LinkSyntax::Markdown,
                        sections: heading_stack.clone(),
                    },
                    range.clone(),
                ));
            }
            Event::Html(_) | Event::InlineHtml(_) => wiki_exclusions.push(range.clone()),
            Event::End(TagEnd::CodeBlock) => {
                wiki_exclusions.push(range.clone());
                code_depth = code_depth.saturating_sub(1);
            }
            _ => {}
        }
    }
    let mut wiki_links = scan_wiki_links(body, body_start_line, &headings, &wiki, &wiki_exclusions);
    let body_offset = text.len() - body.len();
    for (link, _) in &mut wiki_links {
        if let Some(range) = &mut link.target_range {
            range.start += body_offset;
            range.end += body_offset;
        }
    }
    let wiki_ranges: Vec<Range<usize>> =
        wiki_links.iter().map(|(_, range)| range.clone()).collect();
    let mut links: Vec<RawLink> = markdown_links
        .into_iter()
        .filter(|(_, markdown)| {
            !wiki_ranges
                .iter()
                .any(|wiki| wiki.start <= markdown.start && wiki.end >= markdown.end)
        })
        .map(|(link, _)| link)
        .collect();
    links.extend(wiki_links.into_iter().map(|(link, _)| link));
    links.sort_by_key(|link| link.line);
    structural_boundaries.sort_unstable();
    structural_boundaries.dedup();
    Ok(ParsedPage {
        metadata: serde_json::json!({}),
        frontmatter,
        body_start_line,
        headings,
        events,
        section_headings,
        list_entries,
        code_blocks,
        structural_boundaries,
        links,
    })
}

fn scan_wiki_links(
    body: &str,
    body_start_line: usize,
    headings: &[Heading],
    patterns: &[Regex],
    exclusions: &[Range<usize>],
) -> Vec<(RawLink, Range<usize>)> {
    let mut matches = Vec::new();
    for pattern in patterns {
        for capture in pattern.captures_iter(body) {
            let matched = capture.get(0).expect("full match");
            let range = matched.range();
            let Some(target) = capture.name("target") else {
                continue;
            };
            if is_escaped(body, range.start)
                || exclusions
                    .iter()
                    .any(|excluded| excluded.start < range.end && range.start < excluded.end)
            {
                continue;
            }
            let line = body_start_line
                + body[..range.start]
                    .bytes()
                    .filter(|byte| *byte == b'\n')
                    .count();
            let trimmed = target.as_str().trim();
            let start = target.start() + target.as_str().len() - target.as_str().trim_start().len();
            matches.push((
                range,
                trimmed.to_owned(),
                line,
                start..start + trimmed.len(),
            ));
        }
    }
    matches.sort_by_key(|(range, _, _, _)| range.start);
    let mut heading_index = 0;
    let mut heading_stack = Vec::new();
    matches
        .into_iter()
        .map(|(range, target, line, target_range)| {
            while let Some(heading) = headings.get(heading_index)
                && heading.line < line
            {
                heading_stack.truncate(usize::from(heading.level.saturating_sub(1)));
                heading_stack.push(heading.text.clone());
                heading_index += 1;
            }
            (
                RawLink {
                    target,
                    target_range: Some(target_range),
                    line,
                    syntax: LinkSyntax::Wiki,
                    sections: heading_stack.clone(),
                },
                range,
            )
        })
        .collect()
}

fn is_escaped(text: &str, offset: usize) -> bool {
    text.as_bytes()[..offset]
        .iter()
        .rev()
        .take_while(|byte| **byte == b'\\')
        .count()
        % 2
        == 1
}

fn heading_level(level: HeadingLevel) -> u8 {
    match level {
        HeadingLevel::H1 => 1,
        HeadingLevel::H2 => 2,
        HeadingLevel::H3 => 3,
        HeadingLevel::H4 => 4,
        HeadingLevel::H5 => 5,
        HeadingLevel::H6 => 6,
    }
}

/// Parses YAML frontmatter and returns its value, one-based body line, and body source.
pub fn parse_frontmatter(text: &str) -> Result<(serde_json::Value, usize, &str)> {
    if !text.starts_with("---\n") && !text.starts_with("---\r\n") {
        return Ok((serde_json::json!({}), 1, text));
    }
    let mut offset = 0;
    let mut lines = text.split_inclusive('\n');
    let first = lines.next().unwrap_or_default();
    offset += first.len();
    let mut yaml = String::new();
    for (line_number, line) in (2..).zip(lines) {
        if line.trim_end_matches(['\r', '\n']) == "---" {
            offset += line.len();
            let value: serde_yaml::Value =
                serde_yaml::from_str(&yaml).context("parse YAML frontmatter")?;
            let json = serde_json::to_value(value).context("convert YAML frontmatter")?;
            if !json.is_object() {
                anyhow::bail!("frontmatter must be a YAML mapping");
            }
            return Ok((json, line_number + 1, &text[offset..]));
        }
        yaml.push_str(line);
        offset += line.len();
    }
    anyhow::bail!("unterminated YAML frontmatter")
}

/// Successful parsed corpus and relation graph, or all validation findings.
pub type CorpusValidation = Result<(HashMap<String, ParsedPage>, Vec<Edge>), Vec<Finding>>;

/// Parses and validates the complete corpus and its configured resources.
pub fn validate_corpus(pages: &HashMap<String, String>, templates: &Templates) -> CorpusValidation {
    validate_with_baseline(pages, templates, None)
}

/// A successfully validated baseline; never constructed from unvalidated client input.
#[derive(Debug)]
pub struct ValidationBaseline<'a> {
    /// Original source (or opaque identity markers for unchanged documents).
    pub pages: &'a HashMap<String, String>,
    /// Previously validated parsed facts.
    pub parsed: &'a HashMap<String, ParsedPage>,
    /// Previously validated relation edges.
    pub edges: &'a [Edge],
    /// Templates used for the baseline.
    pub templates: &'a Templates,
}

/// Validates a proposed corpus while reusing unchanged baseline facts.
pub fn validate_incremental(
    pages: &HashMap<String, String>,
    templates: &Templates,
    baseline: ValidationBaseline<'_>,
) -> CorpusValidation {
    validate_with_baseline(pages, templates, Some(baseline))
}

fn validate_with_baseline(
    pages: &HashMap<String, String>,
    templates: &Templates,
    baseline: Option<ValidationBaseline<'_>>,
) -> CorpusValidation {
    let mut findings = Vec::new();
    let mut parsed = HashMap::new();
    let mut policies = HashMap::new();
    let mut affected = std::collections::HashSet::new();
    // Inventory changes can add or remove the exact destination of an authored link.
    // Inventory changes therefore re-resolve all authored targets, without reparsing.
    let inventory_changed = baseline.as_ref().is_none_or(|base| {
        base.pages.len() != pages.len() || base.pages.keys().any(|path| !pages.contains_key(path))
    });
    for (path, text) in pages {
        let config = templates.policy(path);
        policies.insert(path.clone(), config);
        if let Some(base) = &baseline
            && base.pages.get(path) == Some(text)
            && templates.same_policy(base.templates, path)
            && let Some(page) = base.parsed.get(path)
        {
            parsed.insert(path.clone(), page.clone());
            if inventory_changed {
                affected.insert(path.clone());
            }
            continue;
        }
        affected.insert(path.clone());
        match parse_page(text, &config.links) {
            Ok(mut page) => {
                page.metadata = project_metadata(config, &page.frontmatter);
                if page.metadata.get("title").is_none() {
                    let title = page
                        .headings
                        .iter()
                        .find(|heading| heading.level == 1)
                        .map(|heading| heading.text.as_str())
                        .unwrap_or(path);
                    page.metadata["title"] = serde_json::json!(title);
                }
                templates.validate_page(path, text, &page, &mut findings);
                parsed.insert(path.clone(), page);
            }
            Err(error) => findings.push(Finding {
                source: None,
                path: path.clone(),
                message: error.to_string(),
                line: None,
            }),
        }
    }
    let resolver = TargetResolver::new(pages.keys());
    let mut resolved_links: HashMap<String, Vec<ResolvedLink>> = HashMap::new();
    for (path, page) in &parsed {
        if !affected.contains(path) {
            continue;
        }
        for link in &page.links {
            match resolver.resolve(path, &link.target, link.syntax) {
                Ok(Some(target)) => {
                    resolved_links
                        .entry(path.clone())
                        .or_default()
                        .push(ResolvedLink {
                            target,
                            syntax: link.syntax,
                            sections: link.sections.clone(),
                        })
                }
                Ok(None) => {}
                Err(message) => findings.push(Finding {
                    source: None,
                    path: path.clone(),
                    message,
                    line: Some(link.line),
                }),
            }
        }
    }
    let mut edges: Vec<Edge> = baseline.as_ref().map_or_else(Vec::new, |base| {
        base.edges
            .iter()
            .filter(|edge| pages.contains_key(&edge.source) && !affected.contains(&edge.source))
            .cloned()
            .collect()
    });
    for (source, config) in &policies {
        if !affected.contains(source) {
            continue;
        }
        for rule in &config.relations {
            match &rule.selector {
                RelationSelector::MarkdownLinks {
                    include,
                    section,
                    syntax,
                } => {
                    if let Some(links) = resolved_links.get(source) {
                        let included = include.as_ref().is_none_or(|pattern| {
                            globset::Glob::new(pattern)
                                .is_ok_and(|glob| glob.compile_matcher().is_match(source))
                        });
                        if !included {
                            continue;
                        }
                        for link in links {
                            if section
                                .as_ref()
                                .is_some_and(|section| !link.sections.contains(section))
                                || syntax.is_some_and(|syntax| !syntax.matches(link.syntax))
                            {
                                continue;
                            }
                            edges.push(Edge {
                                source: source.clone(),
                                relation: rule.name.clone(),
                                target: link.target.clone(),
                            });
                        }
                    }
                }
                RelationSelector::Frontmatter {
                    array_pointer,
                    target_pointer,
                    type_pointer,
                    type_value,
                } => {
                    if let Some(page) = parsed.get(source) {
                        let Some(items) = page
                            .frontmatter
                            .pointer(array_pointer)
                            .and_then(|v| v.as_array())
                        else {
                            continue;
                        };
                        for item in items {
                            if let (Some(pointer), Some(expected)) = (type_pointer, type_value)
                                && item.pointer(pointer) != Some(expected)
                            {
                                continue;
                            }
                            let Some(target) =
                                item.pointer(target_pointer).and_then(|v| v.as_str())
                            else {
                                findings.push(Finding {
                                    source: None,
                                    path: source.clone(),
                                    message: format!(
                                        "relation {} target at {target_pointer} must be a string",
                                        rule.name
                                    ),
                                    line: None,
                                });
                                continue;
                            };
                            match resolver.resolve_reference(target) {
                                Ok(Some(target)) => edges.push(Edge {
                                    source: source.clone(),
                                    relation: rule.name.clone(),
                                    target,
                                }),
                                Ok(None) => unreachable!(),
                                Err(message) => findings.push(Finding {
                                    source: None,
                                    path: source.clone(),
                                    message,
                                    line: None,
                                }),
                            }
                        }
                    }
                }
            }
        }
    }
    validate_reciprocals(&policies, &edges, &mut findings);
    if findings.is_empty() {
        Ok((parsed, edges))
    } else {
        Err(findings)
    }
}

fn validate_reciprocals(
    policies: &HashMap<String, &Template>,
    edges: &[Edge],
    findings: &mut Vec<Finding>,
) {
    let set: std::collections::HashSet<_> = edges.iter().cloned().collect();
    for edge in edges {
        let config = &policies[&edge.source];
        let reciprocal = config
            .relations
            .iter()
            .find(|rule| rule.name == edge.relation)
            .and_then(|rule| rule.reciprocal.as_ref());
        let Some(reciprocal) = reciprocal else {
            continue;
        };
        let reverse = Edge {
            source: edge.target.clone(),
            relation: reciprocal.clone(),
            target: edge.source.clone(),
        };
        if !set.contains(&reverse) {
            findings.push(Finding {
                source: None,
                path: edge.source.clone(),
                message: format!(
                    "missing reciprocal {reciprocal} edge from {} to {} for {} edge",
                    edge.target, edge.source, edge.relation
                ),
                line: None,
            });
        }
    }
}

pub(crate) struct TargetResolver {
    exact: std::collections::HashSet<String>,
}

impl TargetResolver {
    pub(crate) fn new<'a>(paths: impl Iterator<Item = &'a String>) -> Self {
        Self {
            exact: paths.cloned().collect(),
        }
    }

    // Frontmatter relation fields store repository paths, not authored links.
    fn resolve_reference(&self, raw: &str) -> Result<Option<String>, String> {
        self.resolve("", raw, LinkSyntax::Wiki)
    }

    pub(crate) fn resolve(
        &self,
        source: &str,
        raw: &str,
        syntax: LinkSyntax,
    ) -> Result<Option<String>, String> {
        let raw = raw.split(['#', '?']).next().unwrap_or_default();
        if raw.is_empty() {
            return Ok(None);
        }
        let decoded;
        let target = if matches!(syntax, LinkSyntax::Markdown) {
            if raw.starts_with("//") || has_uri_scheme(raw) {
                return Ok(None);
            }
            decoded = decode_markdown_path(raw)?;
            if Path::new(&decoded)
                .extension()
                .is_some_and(|extension| !extension.eq_ignore_ascii_case("md"))
            {
                return Ok(None);
            }
            decoded.as_str()
        } else {
            raw
        };
        let candidate = if let Some(root_path) = target.strip_prefix('/') {
            normalize_path(Path::new(root_path))?
        } else {
            let parent = Path::new(source).parent().unwrap_or_else(|| Path::new(""));
            normalize_path(&parent.join(target))?
        };
        let with_extension = if candidate.ends_with(".md") {
            candidate
        } else {
            format!("{candidate}.md")
        };
        if self.exact.contains(&with_extension) {
            Ok(Some(with_extension))
        } else {
            Err(format!("dangling internal target {raw:?}"))
        }
    }
}

pub(crate) fn decode_markdown_path(path: &str) -> Result<String, String> {
    let mut output = String::with_capacity(path.len());
    for (index, component) in path.split('/').enumerate() {
        if index > 0 {
            output.push('/');
        }
        let bytes = component.as_bytes();
        let mut decoded = Vec::with_capacity(bytes.len());
        let mut cursor = 0;
        while cursor < bytes.len() {
            if bytes[cursor] != b'%' {
                decoded.push(bytes[cursor]);
                cursor += 1;
                continue;
            }
            let Some(pair) = bytes.get(cursor + 1..cursor + 3) else {
                return Err(format!("invalid percent escape in Markdown link {path:?}"));
            };
            let Some(high) = hex_value(pair[0]) else {
                return Err(format!("invalid percent escape in Markdown link {path:?}"));
            };
            let Some(low) = hex_value(pair[1]) else {
                return Err(format!("invalid percent escape in Markdown link {path:?}"));
            };
            let value = high << 4 | low;
            if matches!(value, 0 | b'/' | b'\\') {
                return Err(format!(
                    "encoded path separator or NUL in Markdown link {path:?}"
                ));
            }
            decoded.push(value);
            cursor += 3;
        }
        output.push_str(
            std::str::from_utf8(&decoded)
                .map_err(|_| format!("invalid UTF-8 in Markdown link {path:?}"))?,
        );
    }
    Ok(output)
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

pub(crate) fn has_uri_scheme(target: &str) -> bool {
    let Some((scheme, _)) = target.split_once(':') else {
        return false;
    };
    scheme
        .chars()
        .next()
        .is_some_and(|character| character.is_ascii_alphabetic())
        && scheme.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '+' | '-' | '.')
        })
}

pub(crate) fn normalize_path(path: &Path) -> Result<String, String> {
    let mut parts = Vec::new();
    for part in path.components() {
        match part {
            std::path::Component::Normal(value) => parts.push(value.to_string_lossy().into_owned()),
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                if parts.pop().is_none() {
                    return Err(format!("link escapes repository: {}", path.display()));
                }
            }
            _ => return Err(format!("invalid repository link: {}", path.display())),
        }
    }
    Ok(parts.join("/"))
}

/// Projects configured frontmatter fields into search result metadata.
#[must_use]
pub(crate) fn project_metadata(
    config: &Template,
    frontmatter: &serde_json::Value,
) -> serde_json::Value {
    let mut output = serde_json::Map::new();
    for (name, pointer) in &config.metadata {
        if let Some(value) = frontmatter.pointer(pointer) {
            output.insert(name.clone(), value.clone());
        }
    }
    serde_json::Value::Object(output)
}

/// A resolved schema-defined reference with an editable source location.
#[derive(Debug, Serialize)]
pub struct DocumentReference {
    /// Resolved repository-relative target.
    pub target: String,
    /// Authored target, including any fragment or query.
    pub raw: String,
    /// Byte range for a wiki-link target, excluding its delimiters and label.
    pub range: Option<Range<usize>>,
    /// JSON pointer for a frontmatter relation target.
    pub pointer: Option<String>,
}

/// Resolves configured wiki and frontmatter references using validation policies.
/// Standard Markdown destinations remain available through the Markdown parser.
pub fn document_references(
    sources: &HashMap<String, String>,
    templates: &Templates,
) -> Result<HashMap<String, Vec<DocumentReference>>> {
    let resolver = TargetResolver::new(
        sources
            .keys()
            .filter(|path| path.ends_with(".md") && !crate::template::is_template(path)),
    );
    let mut references = HashMap::new();
    for (path, text) in sources.iter().filter(|(path, _)| path.ends_with(".md")) {
        let policy = templates.policy(path);
        let page = parse_page(text, &policy.links)?;
        let mut found = Vec::new();
        let mut add = |raw: &str, range, pointer: Option<String>| -> Result<()> {
            if let Some(target) = (if pointer.is_some() {
                resolver.resolve_reference(raw)
            } else {
                resolver.resolve(path, raw, LinkSyntax::Wiki)
            })
            .map_err(anyhow::Error::msg)
            .with_context(|| format!("{path}: cannot resolve reference"))?
            {
                found.push(DocumentReference {
                    target,
                    raw: raw.into(),
                    range,
                    pointer,
                });
            }
            Ok(())
        };
        for link in &page.links {
            if let Some(range) = &link.target_range {
                add(&link.target, Some(range.clone()), None)?;
            }
        }
        for rule in &policy.relations {
            if let RelationSelector::Frontmatter {
                array_pointer,
                target_pointer,
                type_pointer,
                type_value,
            } = &rule.selector
            {
                if let Some(items) = page
                    .frontmatter
                    .pointer(array_pointer)
                    .and_then(|v| v.as_array())
                {
                    for (index, item) in items.iter().enumerate() {
                        if let (Some(pointer), Some(expected)) = (type_pointer, type_value) {
                            if item.pointer(pointer) != Some(expected) {
                                continue;
                            }
                        }
                        if let Some(raw) = item.pointer(target_pointer).and_then(|v| v.as_str()) {
                            add(
                                raw,
                                None,
                                Some(format!("{array_pointer}/{index}{target_pointer}")),
                            )?;
                        }
                    }
                }
            }
        }
        references.insert(path.clone(), found);
    }
    Ok(references)
}
