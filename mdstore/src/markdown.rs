use std::{collections::HashMap, path::Path};

use anyhow::{Context, Result};
use pulldown_cmark::{Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use regex::Regex;
use serde::Serialize;

use crate::config::{Config, RelationLinkSyntax, RelationSelector};

#[derive(Debug, Clone, Serialize)]
pub struct ParsedPage {
    pub frontmatter: serde_json::Value,
    pub body_start_line: usize,
    pub headings: Vec<Heading>,
    pub links: Vec<RawLink>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Heading {
    pub level: u8,
    pub text: String,
    pub line: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct RawLink {
    pub target: String,
    pub line: usize,
    pub syntax: LinkSyntax,
    pub sections: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LinkSyntax {
    Markdown,
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
pub struct Edge {
    pub source: String,
    pub relation: String,
    pub target: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Finding {
    pub path: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line: Option<usize>,
}

pub fn parse_page(text: &str, links: &crate::config::LinkConfig) -> Result<ParsedPage> {
    let (frontmatter, body_start_line, body) = parse_frontmatter(text)?;
    let mut headings = Vec::new();
    let mut markdown_links = Vec::new();
    let mut heading: Option<(u8, String, usize)> = None;
    let mut heading_stack = Vec::new();
    let mut code_depth = 0_usize;
    let wiki = links
        .wiki
        .then(|| Regex::new(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]"))
        .transpose()?;
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
            Event::Start(Tag::CodeBlock(_)) => code_depth += 1,
            Event::Text(value) => {
                if let Some((_, text, _)) = &mut heading {
                    text.push_str(&value);
                }
                if let Some(wiki) = &wiki
                    && code_depth == 0
                {
                    let raw = &body[range.clone()];
                    for capture in wiki.captures_iter(raw) {
                        let matched = capture.get(0).expect("full match");
                        let absolute = range.start + matched.start();
                        if absolute > 0 && body.as_bytes()[absolute - 1] == b'\\' {
                            continue;
                        }
                        let line = body_start_line
                            + body[..absolute]
                                .bytes()
                                .filter(|byte| *byte == b'\n')
                                .count();
                        markdown_links.push(RawLink {
                            target: capture[1].trim().into(),
                            line,
                            syntax: LinkSyntax::Wiki,
                            sections: heading_stack.clone(),
                        });
                    }
                }
            }
            Event::Code(value) => {
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
                markdown_links.push(RawLink {
                    target: dest_url.to_string(),
                    line,
                    syntax: LinkSyntax::Markdown,
                    sections: heading_stack.clone(),
                });
            }
            Event::End(TagEnd::CodeBlock) => code_depth = code_depth.saturating_sub(1),
            _ => {}
        }
    }
    Ok(ParsedPage {
        frontmatter,
        body_start_line,
        headings,
        links: markdown_links,
    })
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

pub type CorpusValidation = Result<(HashMap<String, ParsedPage>, Vec<Edge>), Vec<Finding>>;

pub fn validate_corpus(
    config: &Config,
    pages: &HashMap<String, String>,
    extra_files: &HashMap<String, String>,
) -> CorpusValidation {
    let mut findings = Vec::new();
    let mut parsed = HashMap::new();
    for (path, text) in pages {
        match parse_page(text, &config.links) {
            Ok(page) => {
                validate_schema(config, path, &page.frontmatter, extra_files, &mut findings);
                validate_sections(config, path, &page, &mut findings);
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
    for rule in &config.relations {
        match &rule.selector {
            RelationSelector::MarkdownLinks {
                include,
                section,
                syntax,
            } => {
                for (source, links) in &resolved_links {
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
                for (source, page) in &parsed {
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
                        let Some(target) = item.pointer(target_pointer).and_then(|v| v.as_str())
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
    validate_reciprocals(config, &edges, &mut findings);
    if findings.is_empty() {
        Ok((parsed, edges))
    } else {
        Err(findings)
    }
}

fn validate_schema(
    config: &Config,
    path: &str,
    frontmatter: &serde_json::Value,
    extra_files: &HashMap<String, String>,
    findings: &mut Vec<Finding>,
) {
    for rule in &config.schemas {
        let Ok(glob) = globset::Glob::new(&rule.include).map(|glob| glob.compile_matcher()) else {
            continue;
        };
        if !glob.is_match(path) {
            continue;
        }
        let schema_text = extra_files.get(&rule.schema).cloned();
        let Some(schema_text) = schema_text else {
            findings.push(Finding {
                path: path.into(),
                message: format!("schema file not found: {}", rule.schema),
                line: None,
            });
            continue;
        };
        let schema: serde_json::Value = match serde_json::from_str(&schema_text) {
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
        for error in validator.iter_errors(frontmatter) {
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
        let count = page
            .headings
            .iter()
            .filter(|heading| heading.text == rule.heading)
            .count();
        let minimum = rule.minimum.unwrap_or(usize::from(rule.required));
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

fn validate_reciprocals(config: &Config, edges: &[Edge], findings: &mut Vec<Finding>) {
    let set: std::collections::HashSet<_> = edges.iter().cloned().collect();
    for edge in edges {
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
                    "missing reciprocal {} edge from {} to {} for {} edge",
                    reciprocal, edge.target, edge.source, edge.relation
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
        let raw = raw.split('#').next().unwrap_or_default();
        if raw.is_empty() {
            return Ok(None);
        }
        if matches!(syntax, LinkSyntax::Markdown)
            && (raw.starts_with("http://")
                || raw.starts_with("https://")
                || raw.starts_with("mailto:"))
        {
            return Ok(None);
        }
        if raw.starts_with('/') {
            return Err(format!("absolute internal link is not allowed: {raw}"));
        }
        let candidate = if matches!(syntax, LinkSyntax::Markdown) {
            let parent = Path::new(source).parent().unwrap_or_else(|| Path::new(""));
            normalize_path(&parent.join(raw))?
        } else {
            normalize_path(Path::new(raw))?
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

pub fn project_metadata(config: &Config, frontmatter: &serde_json::Value) -> serde_json::Value {
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
            "# Links\n\n[[real]]\n\n`[[inline]]`\n\n```md\n[[fenced]]\n```\n\n\\[[escaped]]\n",
            &LinkConfig {
                markdown: true,
                wiki: true,
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
    fn markdown_relation_selectors_keep_types_separate() {
        let config = Config::from_yaml(
            r#"documents:
  include: ["**/*.md"]
links: {markdown: true, wiki: false}
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
}
