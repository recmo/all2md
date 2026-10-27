//! Canonical app records, shared by view evaluation and app validation.
use anyhow::{Result, anyhow};
use mdstore::validation::{ParsedPage, Templates, is_template, parse_page};
use serde_json::{Value, json};
use std::collections::HashMap;

pub(crate) fn document(path: &str, text: &str, page: &ParsedPage, templates: &Templates) -> Value {
    json!({
        "path": path,
        "title": page.headings.iter().find(|h| h.level == 1).map(|h| h.text.as_str()).unwrap_or(path.rsplit('/').next().unwrap_or(path)),
        "frontmatter": page.frontmatter,
        "template": templates.template_path(path),
        "text": text
    })
}

pub(crate) fn project(sources: &HashMap<String, String>) -> Result<Vec<Value>> {
    let templates =
        Templates::compile(sources).map_err(|errors| anyhow!("Invalid schemas: {errors:?}"))?;
    let mut paths: Vec<_> = sources
        .keys()
        .filter(|path| path.ends_with(".md") && !is_template(path))
        .collect();
    paths.sort();
    paths
        .into_iter()
        .filter_map(
            |path| match parse_page(&sources[path], &Default::default()) {
                Ok(page) if page.frontmatter["mdstore"] == "app" => None,
                Ok(page) => Some(Ok(document(path, &sources[path], &page, &templates))),
                Err(error) => Some(Err(anyhow!("{path}: {error}"))),
            },
        )
        .collect()
}
