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
    Ok(serde_json::to_string(&snapshot)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ordinary_reads_build_a_usable_validation_baseline() {
        let input = serde_json::json!({"revision":"r1", "sources":{
            "config.yaml":"documents:\n  include: ['**/*.md']\n",
            "schema.md":"```starlark\n```\n",
            "a.md":"[B](b.md)\n", "b.md":"# B\n"
        }});
        let snapshot: serde_json::Value =
            serde_json::from_str(&build_snapshot(&input.to_string()).unwrap()).unwrap();
        assert_eq!(snapshot["revision"], "r1");
        assert_eq!(snapshot["documents"].as_object().unwrap().len(), 2);
        assert_eq!(snapshot["files"].as_object().unwrap().len(), 2);
        let result: serde_json::Value = serde_json::from_str(&validate(&serde_json::json!({
            "snapshot": snapshot, "edits":[{"op":"replace_page", "path":"a.md", "base":"[B](b.md)\n", "content":"[Missing](missing.md)\n"}]
        }).to_string()).unwrap()).unwrap();
        assert_eq!(result["valid"], false);
        assert!(
            result["findings"]
                .as_array()
                .unwrap()
                .iter()
                .any(|f| f["message"].as_str().unwrap().contains("dangling"))
        );
    }
}
