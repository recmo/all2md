use std::{collections::HashMap, ops::Range, path::Path};

use anyhow::{Context, Result};
use pulldown_cmark::{Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use regex::Regex;
use serde::Serialize;

use crate::config::{Config, RelationLinkSyntax, RelationSelector};

#[derive(Debug, Clone, Serialize)]
/// Parsed structural and authored-link information for one page.
pub(crate) struct ParsedPage {
    /// Metadata projected using the page's directory template.
    pub metadata: serde_json::Value,
    /// YAML frontmatter converted to JSON.
    pub frontmatter: serde_json::Value,
    /// First body line after frontmatter, one-based.
    pub body_start_line: usize,
    /// Parsed headings in source order.
    pub headings: Vec<Heading>,
    /// Fenced and indented code block ranges.
    pub code_blocks: Vec<SourceRange>,
    /// Source lines that begin or end structural blocks.
    pub structural_boundaries: Vec<usize>,
    /// Authored Markdown and configured wiki links.
    pub links: Vec<RawLink>,
}

#[derive(Debug, Clone, Copy, Serialize)]
/// Inclusive one-based source line range.
pub(crate) struct SourceRange {
    /// First line.
    pub start_line: usize,
    /// Last line.
    pub end_line: usize,
}

#[derive(Debug, Clone, Serialize)]
/// A parsed Markdown heading.
pub(crate) struct Heading {
    /// Heading depth from one through six.
    pub level: u8,
    /// Plain heading text.
    pub text: String,
    /// One-based source line.
    pub line: usize,
}

#[derive(Debug, Clone, Serialize)]
/// An authored link before corpus target resolution.
pub(crate) struct RawLink {
    /// Raw authored target.
    pub target: String,
    /// One-based source line.
    pub line: usize,
    /// Syntax that produced the link.
    pub syntax: LinkSyntax,
    /// Containing heading breadcrumb.
    pub sections: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
/// Supported authored link syntax.
pub(crate) enum LinkSyntax {
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

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
/// A resolved, typed relation edge between pages.
pub struct Edge {
    /// Source page path.
    pub source: String,
    /// Configured relation name.
    pub relation: String,
    /// Target page path.
    pub target: String,
}

#[derive(Debug, Clone, Serialize)]
/// A structured corpus validation finding.
pub struct Finding {
    /// Page or configuration resource path.
    pub path: String,
    /// Human-readable validation message.
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    /// Optional one-based source line.
    pub line: Option<usize>,
}

/// Parses one Markdown page according to configured link syntaxes.
pub(crate) fn parse_page(text: &str, links: &crate::config::LinkConfig) -> Result<ParsedPage> {
    let (frontmatter, body_start_line, body) = parse_frontmatter(text)?;
    let mut headings = Vec::new();
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
    let wiki_links = scan_wiki_links(body, body_start_line, &headings, &wiki, &wiki_exclusions);
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
            matches.push((range, target.as_str().trim().to_owned(), line));
        }
    }
    matches.sort_by_key(|(range, _, _)| range.start);
    let mut heading_index = 0;
    let mut heading_stack = Vec::new();
    matches
        .into_iter()
        .map(|(range, target, line)| {
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

fn parse_frontmatter(text: &str) -> Result<(serde_json::Value, usize, &str)> {
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
            return Ok((json, line_number + 1, &text[offset..]));
        }
        yaml.push_str(line);
        offset += line.len();
    }
    anyhow::bail!("unterminated YAML frontmatter")
}

/// Successful parsed corpus and relation graph, or all validation findings.
pub(crate) type CorpusValidation = Result<(HashMap<String, ParsedPage>, Vec<Edge>), Vec<Finding>>;

struct CompiledSchema {
    matcher: globset::GlobMatcher,
    validator: jsonschema::Validator,
}

/// Parses and validates the complete corpus and its configured resources.
pub(crate) fn validate_corpus(
    config: &Config,
    pages: &HashMap<String, String>,
    extra_files: &HashMap<String, String>,
) -> CorpusValidation {
    let mut findings = Vec::new();
    let schemas = compile_schemas(config, extra_files, &mut findings);
    crate::template::validate_corpus(pages, extra_files, &mut findings);
    let mut parsed = HashMap::new();
    let templates = crate::template::compile_policies(config, extra_files);
    let mut policies = HashMap::new();
    for (path, text) in pages {
        let Ok(policy) = crate::template::page_policy(config, path, extra_files, &templates) else {
            continue;
        };
        policies.insert(path.clone(), policy);
        let config = &policies[path];
        match parse_page(text, &config.links) {
            Ok(mut page) => {
                page.metadata = project_metadata(config, &page.frontmatter);
                validate_schema(&schemas, path, &page.frontmatter, &mut findings);
                validate_sections(config, path, &page, &mut findings);
                crate::structure::validate(config, path, text, &page, &mut findings);
                crate::markdown_style::validate(&config.markdown, path, text, &page, &mut findings);
                parsed.insert(path.clone(), page);
            }
            Err(error) => findings.push(Finding {
                path: path.clone(),
                message: error.to_string(),
                line: None,
            }),
        }
    }
    let resolver = TargetResolver::new(pages.keys());
    let mut resolved_links: HashMap<String, Vec<ResolvedLink>> = HashMap::new();
    for (path, page) in &parsed {
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
                    path: path.clone(),
                    message,
                    line: Some(link.line),
                }),
            }
        }
    }
    let mut edges = Vec::new();
    for (source, config) in &policies {
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
                                    path: source.clone(),
                                    message: format!(
                                        "relation {} target at {target_pointer} must be a string",
                                        rule.name
                                    ),
                                    line: None,
                                });
                                continue;
                            };
                            match resolver.resolve(source, target, LinkSyntax::Wiki) {
                                Ok(Some(target)) => edges.push(Edge {
                                    source: source.clone(),
                                    relation: rule.name.clone(),
                                    target,
                                }),
                                Ok(None) => unreachable!(),
                                Err(message) => findings.push(Finding {
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

fn compile_schemas(
    config: &Config,
    extra_files: &HashMap<String, String>,
    findings: &mut Vec<Finding>,
) -> Vec<CompiledSchema> {
    let mut compiled = Vec::new();
    for rule in &config.schemas {
        let matcher = globset::Glob::new(&rule.include)
            .expect("schema glob was validated")
            .compile_matcher();
        let Some(schema_text) = extra_files.get(&rule.schema) else {
            findings.push(Finding {
                path: rule.schema.clone(),
                message: format!("schema file not found: {}", rule.schema),
                line: None,
            });
            continue;
        };
        let schema: serde_json::Value = match serde_json::from_str(schema_text) {
            Ok(value) => value,
            Err(error) => {
                findings.push(Finding {
                    path: rule.schema.clone(),
                    message: format!("invalid JSON schema: {error}"),
                    line: None,
                });
                continue;
            }
        };
        let validator = match jsonschema::validator_for(&schema) {
            Ok(value) => value,
            Err(error) => {
                findings.push(Finding {
                    path: rule.schema.clone(),
                    message: format!("invalid JSON schema: {error}"),
                    line: None,
                });
                continue;
            }
        };
        compiled.push(CompiledSchema { matcher, validator });
    }
    compiled
}

fn validate_schema(
    schemas: &[CompiledSchema],
    path: &str,
    frontmatter: &serde_json::Value,
    findings: &mut Vec<Finding>,
) {
    for schema in schemas {
        if !schema.matcher.is_match(path) {
            continue;
        }
        for error in schema.validator.iter_errors(frontmatter) {
            findings.push(Finding {
                path: path.into(),
                message: format!("frontmatter{}: {error}", error.instance_path),
                line: Some(1),
            });
        }
    }
}

fn validate_sections(config: &Config, path: &str, page: &ParsedPage, findings: &mut Vec<Finding>) {
    for rule in &config.sections {
        if rule.include.as_ref().is_some_and(|pattern| {
            !globset::Glob::new(pattern)
                .expect("section glob was validated")
                .compile_matcher()
                .is_match(path)
        }) {
            continue;
        }
        let count = page
            .headings
            .iter()
            .filter(|heading| heading.text == rule.heading)
            .count();
        let minimum = rule.minimum();
        if count < minimum {
            findings.push(Finding {
                path: path.into(),
                message: format!(
                    "section {:?} occurs {count} times; minimum is {minimum}",
                    rule.heading
                ),
                line: None,
            });
        }
        if let Some(maximum) = rule.maximum
            && count > maximum
        {
            findings.push(Finding {
                path: path.into(),
                message: format!(
                    "section {:?} occurs {count} times; maximum is {maximum}",
                    rule.heading
                ),
                line: None,
            });
        }
    }
}

fn validate_reciprocals(
    policies: &HashMap<String, Config>,
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

struct TargetResolver {
    exact: std::collections::HashSet<String>,
    stems: HashMap<String, Vec<String>>,
}

impl TargetResolver {
    fn new<'a>(paths: impl Iterator<Item = &'a String>) -> Self {
        let mut exact = std::collections::HashSet::new();
        let mut stems: HashMap<String, Vec<String>> = HashMap::new();
        for path in paths {
            exact.insert(path.clone());
            let without_extension = path.strip_suffix(".md").unwrap_or(path);
            stems
                .entry(without_extension.into())
                .or_default()
                .push(path.clone());
            if let Some(name) = Path::new(without_extension)
                .file_name()
                .and_then(|v| v.to_str())
                .filter(|name| *name != without_extension)
            {
                stems.entry(name.into()).or_default().push(path.clone());
            }
        }
        Self { exact, stems }
    }

    fn resolve(
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
        if target.starts_with('/') {
            return Err(format!("absolute internal link is not allowed: {target}"));
        }
        let candidate = if matches!(syntax, LinkSyntax::Markdown) {
            let parent = Path::new(source).parent().unwrap_or_else(|| Path::new(""));
            normalize_path(&parent.join(target))?
        } else {
            normalize_path(Path::new(target))?
        };
        let with_extension = if candidate.ends_with(".md") {
            candidate.clone()
        } else {
            format!("{candidate}.md")
        };
        if self.exact.contains(&with_extension) {
            return Ok(Some(with_extension));
        }
        let stem = candidate.strip_suffix(".md").unwrap_or(&candidate);
        match self.stems.get(stem).map(Vec::as_slice) {
            Some([only]) => Ok(Some(only.clone())),
            Some(many) => Err(format!("ambiguous internal target {raw:?}: {many:?}")),
            None => Err(format!("dangling internal target {raw:?}")),
        }
    }
}

fn decode_markdown_path(path: &str) -> Result<String, String> {
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

fn has_uri_scheme(target: &str) -> bool {
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

fn normalize_path(path: &Path) -> Result<String, String> {
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
    config: &Config,
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::LinkConfig;

    #[test]
    fn wiki_links_ignore_code_and_escaped_examples() {
        let page = parse_page(
            "# Links\n\n{{real}}\n\n[[not configured]]\n\n`{{inline}}`\n\n```md\n{{fenced}}\n```\n\n\\{{escaped}}\n",
            &LinkConfig {
                markdown: false,
                wiki: vec![r"\{\{(?P<target>[^}|#]+)(?:#[^}|]+)?(?:\|[^}]+)?\}\}".into()],
            },
        )
        .unwrap();
        assert_eq!(
            page.links
                .iter()
                .map(|link| link.target.as_str())
                .collect::<Vec<_>>(),
            ["real"]
        );
    }

    #[test]
    fn configured_wiki_links_take_precedence_over_markdown_parsing() {
        let config = Config::from_yaml(
            r#"documents:
  include: ["**/*.md"]
links:
  markdown: true
  wiki: ['\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]']
relations:
  - name: markdown
    selector: {kind: markdown_links, syntax: markdown}
  - name: wiki
    selector: {kind: markdown_links, syntax: wiki}
provider: {dimensions: 2}
"#,
        )
        .unwrap();
        let pages = HashMap::from([
            ("a.md".into(), "[[b#Part|Bee]]\n".into()),
            ("b.md".into(), "B\n".into()),
        ]);
        let (_, edges) = validate_corpus(&config, &pages, &HashMap::new()).unwrap();
        assert_eq!(
            edges,
            [Edge {
                source: "a.md".into(),
                relation: "wiki".into(),
                target: "b.md".into(),
            }]
        );
    }

    #[test]
    fn markdown_relation_selectors_keep_types_separate() {
        let config = Config::from_yaml(
            r#"documents:
  include: ["**/*.md"]
links: {markdown: true}
relations:
  - name: friend
    reciprocal: friend
    selector: {kind: markdown_links, section: Friends}
  - name: source
    reciprocal: source
    selector: {kind: markdown_links, section: Sources}
provider: {dimensions: 2}
"#,
        )
        .unwrap();
        let pages = HashMap::from([
            (
                "a.md".into(),
                "# Friends\n\n[B](b.md)\n\n# Sources\n\n[C](c.md)\n".into(),
            ),
            ("b.md".into(), "# Friends\n\n[A](a.md)\n".into()),
            ("c.md".into(), "# Sources\n\n[A](a.md)\n".into()),
        ]);
        let (_, edges) = validate_corpus(&config, &pages, &HashMap::new()).unwrap();
        assert_eq!(edges.len(), 4);
        assert!(edges.iter().all(|edge| {
            (edge.relation == "friend" && edge.source != "c.md" && edge.target != "c.md")
                || (edge.relation == "source" && edge.source != "b.md" && edge.target != "b.md")
        }));
    }

    #[test]
    fn required_sections_cannot_be_disabled_by_zero_minimum() {
        let config = Config::from_yaml(
            r#"documents:
  include: ["**/*.md"]
sections:
  - heading: Notes
    required: true
    minimum: 0
provider: {dimensions: 2}
"#,
        )
        .unwrap();
        let pages = HashMap::from([("page.md".into(), "# Other\n".into())]);
        let findings = validate_corpus(&config, &pages, &HashMap::new()).unwrap_err();
        assert!(
            findings
                .iter()
                .any(|finding| finding.message.contains("minimum is 1"))
        );
    }

    #[test]
    fn section_rules_select_heterogeneous_page_types() {
        let config = Config::from_yaml(
            r#"documents:
  include: ["**/*.md"]
sections:
  - include: "people/**"
    heading: Biography
    required: true
  - include: "tasks/**"
    heading: Status
    required: true
provider: {dimensions: 2}
"#,
        )
        .unwrap();
        let pages = HashMap::from([
            ("people/alice.md".into(), "# Biography\n\nPerson.\n".into()),
            ("tasks/one.md".into(), "# Status\n\nOpen.\n".into()),
        ]);
        assert!(validate_corpus(&config, &pages, &HashMap::new()).is_ok());

        let invalid = HashMap::from([
            ("people/alice.md".into(), "# Status\n\nActive.\n".into()),
            ("tasks/one.md".into(), "# Status\n\nOpen.\n".into()),
        ]);
        let findings = validate_corpus(&config, &invalid, &HashMap::new()).unwrap_err();
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].path, "people/alice.md");
    }

    #[test]
    fn non_page_markdown_destinations_are_not_internal_links() {
        let paths = [
            "notes/page.md".to_owned(),
            "notes/other.md".to_owned(),
            "notes/other note.md".to_owned(),
            "notes/café.md".to_owned(),
            "notes/hash#tag.md".to_owned(),
            "notes/wiki%20name.md".to_owned(),
        ];
        let resolver = TargetResolver::new(paths.iter());
        for target in [
            "../assets/paper.pdf",
            "ftp://example.com/file",
            "urn:isbn:9780140328721",
            "//cdn.example.com/file",
        ] {
            assert_eq!(
                resolver.resolve("notes/page.md", target, LinkSyntax::Markdown),
                Ok(None),
                "resolved {target:?} as a page"
            );
        }
        assert_eq!(
            resolver.resolve("notes/page.md", "other.md?view=1", LinkSyntax::Markdown),
            Ok(Some("notes/other.md".into()))
        );
        assert_eq!(
            resolver.resolve(
                "notes/page.md",
                "other%20note.md?view=1#details",
                LinkSyntax::Markdown
            ),
            Ok(Some("notes/other note.md".into()))
        );
        assert_eq!(
            resolver.resolve("notes/page.md", "caf%C3%A9.md", LinkSyntax::Markdown),
            Ok(Some("notes/café.md".into()))
        );
        assert_eq!(
            resolver.resolve("notes/page.md", "hash%23tag.md", LinkSyntax::Markdown),
            Ok(Some("notes/hash#tag.md".into()))
        );
        assert_eq!(
            resolver.resolve("notes/page.md", "wiki%20name", LinkSyntax::Wiki),
            Ok(Some("notes/wiki%20name.md".into()))
        );

        let config =
            Config::from_yaml("documents:\n  include: ['**/*.md']\nprovider:\n  dimensions: 2\n")
                .unwrap();
        let pages = HashMap::from([
            (
                "notes/page.md".into(),
                "[Other](other%20note.md?view=1#details)\n".into(),
            ),
            ("notes/other note.md".into(), "Other.\n".into()),
        ]);
        assert!(validate_corpus(&config, &pages, &HashMap::new()).is_ok());
    }

    #[test]
    fn root_page_stems_resolve_once() {
        let paths = ["page.md".to_owned()];
        let resolver = TargetResolver::new(paths.iter());

        assert_eq!(
            resolver.resolve("source.md", "page", LinkSyntax::Wiki),
            Ok(Some("page.md".into()))
        );
    }

    #[test]
    fn markdown_link_decoding_rejects_invalid_or_escaping_paths() {
        let paths = ["notes/page.md".to_owned()];
        let resolver = TargetResolver::new(paths.iter());
        for target in [
            "bad%.md",
            "bad%GG.md",
            "bad%00.md",
            "bad%2Fname.md",
            "bad%5Cname.md",
            "bad%FF.md",
            "%2e%2e/%2e%2e/outside.md",
        ] {
            assert!(
                resolver
                    .resolve("notes/page.md", target, LinkSyntax::Markdown)
                    .is_err(),
                "accepted {target:?}"
            );
        }
    }

    #[test]
    fn schema_resources_are_compiled_once_per_validation() {
        let config = Config::from_yaml(
            "documents:\n  include: ['**/*.md']\nschemas:\n  - include: '**/*.md'\n    schema: .mdstore/schemas/page.json\nprovider:\n  dimensions: 2\n",
        )
        .unwrap();
        let pages = HashMap::from([
            ("one.md".into(), "---\nvalue: one\n---\n".into()),
            ("two.md".into(), "---\nvalue: two\n---\n".into()),
        ]);
        let missing = validate_corpus(&config, &pages, &HashMap::new()).unwrap_err();
        assert_eq!(missing.len(), 1);
        assert_eq!(missing[0].path, ".mdstore/schemas/page.json");

        let extra = HashMap::from([(
            ".mdstore/schemas/page.json".into(),
            r#"{"type":"object","required":["required"]}"#.into(),
        )]);
        let invalid_pages = validate_corpus(&config, &pages, &extra).unwrap_err();
        assert_eq!(invalid_pages.len(), 2);
        assert!(invalid_pages.iter().any(|finding| finding.path == "one.md"));
        assert!(invalid_pages.iter().any(|finding| finding.path == "two.md"));
    }
}
