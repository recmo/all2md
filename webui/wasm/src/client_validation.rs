//! Portable validation of edits over a server-validated, source-light baseline.
use crate::snapshot::{SNAPSHOT_VERSION, ValidationSnapshot};
use mdstore::validation::{self as markdown, Config, Finding, Templates, source_hash};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub(crate) enum ClientEdit {
    DeletePage {
        path: String,
        base: String,
    },
    ReplacePage {
        path: String,
        base: String,
        content: String,
    },
    CreatePage {
        path: String,
        content: String,
    },
}

#[derive(Debug, Deserialize)]
pub(crate) struct ClientValidationInput {
    pub snapshot: ValidationSnapshot,
    pub edits: Vec<ClientEdit>,
    #[serde(default)]
    pub sources: HashMap<String, String>,
}

#[derive(Debug, Default, Serialize)]
pub(crate) struct ClientValidationResult {
    pub valid: bool,
    pub findings: Vec<Finding>,
    pub needs: Vec<String>,
    pub server_required: Option<String>,
    pub restart_required: bool,
}

pub(crate) fn validate(input: ClientValidationInput) -> ClientValidationResult {
    match check(input) {
        Ok(result) => result,
        Err(findings) => ClientValidationResult {
            findings,
            ..Default::default()
        },
    }
}
fn finding(path: &str, message: impl Into<String>) -> Vec<Finding> {
    vec![Finding {
        source: None,
        path: path.into(),
        line: None,
        message: message.into(),
    }]
}
fn parse_config(files: &HashMap<String, String>) -> Result<Config, Vec<Finding>> {
    Config::from_yaml(
        files
            .get("config.yaml")
            .ok_or_else(|| finding("config.yaml", "Missing configuration"))?,
    )
    .map_err(|error| finding("config.yaml", format!("invalid configuration: {error:#}")))
}

fn check(input: ClientValidationInput) -> Result<ClientValidationResult, Vec<Finding>> {
    let snapshot = input.snapshot;
    if snapshot.version != SNAPSHOT_VERSION {
        return Ok(ClientValidationResult {
            server_required: Some(
                "Validation snapshot version changed; reconnect to refresh.".into(),
            ),
            ..Default::default()
        });
    }
    let old_config = parse_config(&snapshot.files)?;
    let old_templates = Templates::compile(&snapshot.files)?;
    let mut files = snapshot.files.clone();
    // Unchanged source is never parsed or passed to callbacks. Its parsed facts
    // come from the validated baseline; hashes are opaque equality markers only.
    let mut before: HashMap<String, String> = snapshot
        .documents
        .iter()
        .map(|(path, doc)| (path.clone(), doc.hash.clone()))
        .collect();
    let mut after = before.clone();
    let mut edited = std::collections::HashSet::new();
    for edit in &input.edits {
        let (path, base, content) = match edit {
            ClientEdit::ReplacePage {
                path,
                base,
                content,
            } => (path, Some(base), Some(content)),
            ClientEdit::CreatePage { path, content } => (path, None, Some(content)),
            ClientEdit::DeletePage { path, base } => (path, Some(base), None),
        };
        mdstore::validation::validate_repo_path(path)
            .map_err(|error| finding(path, error.to_string()))?;
        if !edited.insert(path.clone()) {
            return Err(finding(path, "Multiple edits for the same document"));
        }
        let resource = mdstore::validation::is_config_resource_path(path)
            || mdstore::validation::is_template(path);
        if (mdstore::validation::is_config_resource_path(path)
            && !old_config.server.allow_config_edits)
            || (mdstore::validation::is_template(path) && !old_config.server.allow_template_edits)
            || (!resource && !path.ends_with(".md"))
        {
            return Err(finding(
                path,
                "This file is read-only with the current server permissions.",
            ));
        }
        let existing_hash = if resource {
            snapshot.files.get(path).map(|text| source_hash(text))
        } else {
            snapshot.documents.get(path).map(|doc| doc.hash.clone())
        };
        match (base, existing_hash) {
            (Some(base), Some(hash)) if source_hash(base) == hash => {}
            (None, None) => {}
            _ => {
                return Err(finding(
                    path,
                    "document changed on the server; reconcile the draft before submitting",
                ));
            }
        }
        if resource {
            if let Some(content) = content {
                files.insert(path.clone(), content.clone());
            } else {
                files.remove(path);
            }
        } else {
            if let Some(base) = base {
                before.insert(path.clone(), base.clone());
            }
            if let Some(content) = content {
                after.insert(path.clone(), content.clone());
            } else {
                after.remove(path);
            }
        }
    }
    let config = parse_config(&files)?;
    let templates = Templates::compile(&files)?;
    // A broader include glob can introduce tracked files absent from this inventory.
    if serde_json::to_value(&config.documents).unwrap()
        != serde_json::to_value(&old_config.documents).unwrap()
    {
        return Ok(ClientValidationResult {
            server_required: Some(
                "Document selection changed; the server must validate the newly selected corpus."
                    .into(),
            ),
            ..Default::default()
        });
    }
    let (include, exclude) = config
        .document_globs()
        .map_err(|error| finding("config.yaml", error.to_string()))?;
    for path in after.keys() {
        if !include.is_match(path) || exclude.is_match(path) {
            return Err(finding(
                path,
                "edited page is outside configured document globs",
            ));
        }
    }
    // App collection functions can inspect any document source. Request the
    // baseline sources before evaluating an edited definition, even offline.
    let app_changed = edited.iter().any(|path| {
        after
            .get(path)
            .is_some_and(|text| crate::apps::is_app(text))
    });
    let mut needs = Vec::new();
    for (path, doc) in &snapshot.documents {
        if edited.contains(path) || (!app_changed && templates.same_policy(&old_templates, path)) {
            continue;
        }
        match input
            .sources
            .get(path)
            .filter(|source| source_hash(source) == doc.hash)
        {
            Some(source) => {
                before.insert(path.clone(), source.clone());
                after.insert(path.clone(), source.clone());
            }
            None => needs.push(path.clone()),
        }
    }
    if !needs.is_empty() {
        needs.sort();
        return Ok(ClientValidationResult {
            needs,
            ..Default::default()
        });
    }
    let parsed = snapshot
        .documents
        .into_iter()
        .map(|(path, doc)| (path, doc.parsed))
        .collect();
    let (proposed, _) = markdown::validate_incremental(
        &after,
        &templates,
        markdown::ValidationBaseline {
            pages: &before,
            parsed: &parsed,
            edges: &snapshot.edges,
            templates: &old_templates,
        },
    )?;
    // App edits execute collection queries over the proposed inventory. Actions
    // require an event and are checked when invoked, never with fabricated events.
    let mut findings = Vec::new();
    let mut app_paths: Vec<_> = proposed
        .iter()
        .filter(|(path, page)| page.frontmatter["mdstore"] == "app" && edited.contains(*path))
        .map(|(path, _)| path)
        .collect();
    app_paths.sort();
    if !app_paths.is_empty() {
        let mut paths: Vec<_> = proposed
            .keys()
            .filter(|path| {
                !mdstore::validation::is_template(path)
                    && proposed[*path].frontmatter["mdstore"] != "app"
            })
            .collect();
        paths.sort();
        let documents: Vec<_> = paths
            .into_iter()
            .map(|path| {
                crate::app_documents::document(path, &after[path], &proposed[path], &templates)
            })
            .collect();
        for path in app_paths {
            if let Err(error) =
                crate::apps::validate_definition(path, &after[path], documents.clone())
            {
                findings.push(Finding {
                    source: None,
                    path: path.clone(),
                    message: format!("invalid app: {error}"),
                    line: None,
                });
            }
        }
    }
    if !findings.is_empty() {
        return Err(findings);
    }
    old_templates.validate_changes(&before, &after)?;
    templates.validate_changes(&before, &after)?;
    Ok(ClientValidationResult {
        valid: true,
        restart_required: config.server.listen != old_config.server.listen,
        ..Default::default()
    })
}
