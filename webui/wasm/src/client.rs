//! JSON entry points for the web UI workers.

use anyhow::{Result, anyhow};

/// Validates a JSON edit request against a source-light validation snapshot.
/// Findings and missing source requirements are returned as JSON.
pub fn validate(input: &str) -> Result<String> {
    let input = serde_json::from_str(input)
        .map_err(|error| anyhow!("Invalid validation input: {error}"))?;
    Ok(serde_json::to_string(&crate::client_validation::validate(
        input,
    ))?)
}

/// Evaluates a Starlark app definition or action over a JSON document list.
pub fn evaluate_app(input: &str) -> Result<String> {
    #[derive(serde::Deserialize)]
    struct Input {
        path: String,
        source: String,
        sources: std::collections::HashMap<String, String>,
        action: Option<String>,
        event: Option<serde_json::Value>,
    }
    let input: Input = serde_json::from_str(input)?;
    let documents = crate::app_documents::project(&input.sources)?;
    let result = crate::apps::evaluate(crate::apps::Input {
        path: input.path,
        source: input.source,
        documents: documents.clone(),
        action: input.action,
        event: input.event,
    })?;
    Ok(serde_json::to_string(
        &serde_json::json!({"result": result, "documents": documents}),
    )?)
}

/// Builds the browser's validation baseline from ordinary document reads.
pub fn build_snapshot(input: &str) -> Result<String> {
    use crate::snapshot::{SNAPSHOT_VERSION, SnapshotDocument, ValidationSnapshot};
    use mdstore::validation::{
        Templates, is_config_resource_path, is_template, source_hash, validate_corpus,
    };
    use std::collections::HashMap;
    #[derive(serde::Deserialize)]
    struct Input {
        revision: String,
        sources: HashMap<String, String>,
    }
    let input: Input = serde_json::from_str(input)?;
    let (files, pages): (HashMap<_, _>, HashMap<_, _>) = input
        .sources
        .into_iter()
        .partition(|(path, _)| is_config_resource_path(path) || is_template(path));
    let templates =
        Templates::compile(&files).map_err(|e| anyhow!("Invalid baseline templates: {e:?}"))?;
    let (parsed, edges) = validate_corpus(&pages, &templates)
        .map_err(|e| anyhow!("Invalid baseline documents: {e:?}"))?;
    let property_schemas: HashMap<_, _> = pages
        .keys()
        .filter_map(|path| {
            let schema_path = templates.template_path(path)?;
            let schema = templates.frontmatter_schema(path)?.clone();
            Some((schema_path.trim_start_matches('/').to_owned(), schema))
        })
        .collect();
    let snapshot = ValidationSnapshot {
        version: SNAPSHOT_VERSION,
        revision: input.revision,
        files,
        edges,
        documents: parsed
            .into_iter()
            .map(|(path, parsed)| {
                let hash = source_hash(&pages[&path]);
                (path, SnapshotDocument { hash, parsed })
            })
            .collect(),
    };
    let mut output = serde_json::to_value(snapshot)?;
    output["property_schemas"] = serde_json::to_value(property_schemas)?;
    Ok(serde_json::to_string(&output)?)
}

/// Projects schema references and converts byte offsets for JavaScript strings.
pub fn document_references(input: &str) -> Result<String> {
    let sources: std::collections::HashMap<String, String> = serde_json::from_str(input)?;
    let templates = mdstore::validation::Templates::compile(&sources)
        .map_err(|e| anyhow!("Invalid schemas: {e:?}"))?;
    let mut references = mdstore::validation::document_references(&sources, &templates)?;
    for (path, refs) in &mut references {
        for reference in refs {
            if let Some(range) = &mut reference.range {
                let text = &sources[path];
                range.start = text[..range.start].encode_utf16().count();
                range.end = text[..range.end].encode_utf16().count();
            }
        }
    }
    Ok(serde_json::to_string(&references)?)
}
