use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::Arc,
};

use anyhow::Result;
use async_trait::async_trait;
use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use http_body_util::BodyExt;
use mdstore::{
    ApplyEditsRequest, Config, Store,
    hashline::{EditOperation, short_hash},
    provider::{InputType, RerankResult, RetrievalProvider},
};
use tower::ServiceExt;

struct FakeProvider;

#[async_trait]
impl RetrievalProvider for FakeProvider {
    async fn embed(&self, _input_type: InputType, input: &[String]) -> Result<Vec<Vec<f32>>> {
        Ok(input
            .iter()
            .map(|value| {
                let lowered = value.to_lowercase();
                vec![
                    if lowered.contains("alice") { 1.0 } else { 0.0 },
                    if lowered.contains("bob") { 1.0 } else { 0.0 },
                ]
            })
            .collect())
    }

    async fn rerank(
        &self,
        query: &str,
        documents: &[String],
        top_n: usize,
    ) -> Result<Vec<RerankResult>> {
        let query = query.to_lowercase();
        let mut output: Vec<_> = documents
            .iter()
            .enumerate()
            .map(|(index, document)| RerankResult {
                index,
                relevance_score: if document.to_lowercase().contains(&query) {
                    1.0
                } else {
                    0.1
                },
            })
            .collect();
        output.sort_by(|a, b| b.relevance_score.total_cmp(&a.relevance_score));
        output.truncate(top_n);
        Ok(output)
    }

    fn model(&self) -> &str {
        "fake-embed"
    }

    fn dimensions(&self) -> usize {
        2
    }
}

struct Repository {
    _temporary: tempfile::TempDir,
    root: PathBuf,
}

impl Repository {
    fn new() -> Self {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().to_path_buf();
        command(&root, &["init", "-q"]);
        command(&root, &["config", "user.name", "mdstore tests"]);
        command(&root, &["config", "user.email", "mdstore@example.invalid"]);
        fs::create_dir_all(root.join(".mdstore")).unwrap();
        fs::write(root.join(".mdstore/config.yaml"), config_yaml()).unwrap();
        fs::write(root.join(".mdstore/schema.json"), schema()).unwrap();
        fs::write(root.join(".gitignore"), "*.mdstore\n!.mdstore/\n").unwrap();
        fs::write(root.join("alice.md"), page("Alice")).unwrap();
        fs::write(root.join("bob.md"), page("Bob")).unwrap();
        command(&root, &["add", "."]);
        command(&root, &["commit", "-q", "-m", "initial corpus"]);
        Self {
            _temporary: temporary,
            root,
        }
    }

    fn store(&self) -> Arc<Store> {
        let config = Config::load(&self.root).unwrap();
        let git_dir = self.root.join(".git");
        Store::open_with_provider(self.root.clone(), config, Arc::new(FakeProvider), git_dir)
            .unwrap()
    }
}

fn command(root: &Path, arguments: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(root)
        .args(arguments)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {:?} failed: {}",
        arguments,
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

fn config_yaml() -> &'static str {
    r#"documents:
  include: ["**/*.md"]
schemas:
  - include: "**/*.md"
    schema: ".mdstore/schema.json"
metadata:
  display_name: /name
links:
  markdown: true
  wiki: true
relations:
  - name: mentions
    reciprocal: mentions
    selector:
      kind: markdown_links
chunking:
  target_tokens: 40
  overlap_percent: 10
  max_chars: 500
  context_pointers: [/name]
provider:
  embedding_model: fake-embed
  rerank_model: fake-rerank
  dimensions: 2
git:
  push: false
server:
  listen: 127.0.0.1:3131
"#
}

fn schema() -> &'static str {
    r#"{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["name"],
  "properties": {"name": {"type": "string"}},
  "additionalProperties": false
}"#
}

fn page(name: &str) -> String {
    format!("---\nname: {name}\n---\n# Notes\n\n{name} profile.\n")
}

#[test]
fn reciprocal_links_are_caller_authored_and_atomic() {
    let repository = Repository::new();
    let store = repository.store();
    let notes = format!("5:{}", short_hash("# Notes"));
    let one_sided = ApplyEditsRequest {
        edit_summary: "add one-sided link".into(),
        edits: vec![EditOperation::InsertAfter {
            path: "alice.md".into(),
            anchor: notes.clone(),
            content: "\n[Bob](bob.md)".into(),
        }],
    };
    assert!(store.apply_edits(&one_sided).is_err());
    assert!(
        !fs::read_to_string(repository.root.join("alice.md"))
            .unwrap()
            .contains("bob.md")
    );
    assert_eq!(
        command(&repository.root, &["rev-list", "--count", "HEAD"]).trim(),
        "1"
    );

    let request = ApplyEditsRequest {
        edit_summary: "link Alice and Bob".into(),
        edits: vec![
            EditOperation::InsertAfter {
                path: "alice.md".into(),
                anchor: notes.clone(),
                content: "\n[Bob](bob.md)".into(),
            },
            EditOperation::InsertAfter {
                path: "bob.md".into(),
                anchor: notes,
                content: "\n[Alice](alice.md)".into(),
            },
        ],
    };
    let response = store.apply_edits(&request).unwrap();
    assert_eq!(response.touched_paths, ["alice.md", "bob.md"]);
    assert!(
        fs::read_to_string(repository.root.join("alice.md"))
            .unwrap()
            .contains("[Bob](bob.md)")
    );
    assert_eq!(
        command(&repository.root, &["log", "-1", "--pretty=%B"]).trim(),
        "link Alice and Bob"
    );

    let replay = store.apply_edits(&request).unwrap();
    assert!(matches!(
        replay.status,
        mdstore::store::ApplyStatus::AlreadyApplied
    ));
}

#[test]
fn rejects_mixed_configuration_and_content_batch() {
    let repository = Repository::new();
    let store = repository.store();
    let request = ApplyEditsRequest {
        edit_summary: "mixed batch".into(),
        edits: vec![
            EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "Changed.".into(),
            },
            EditOperation::CreatePage {
                path: ".mdstore/other.json".into(),
                content: "{}".into(),
            },
        ],
    };
    assert!(store.apply_edits(&request).is_err());
    assert_eq!(
        command(&repository.root, &["rev-list", "--count", "HEAD"]).trim(),
        "1"
    );
}

#[test]
fn configuration_activation_reselects_the_tracked_corpus() {
    let repository = Repository::new();
    let store = repository.store();
    let config = fs::read_to_string(repository.root.join(".mdstore/config.yaml")).unwrap();
    let anchor = format!("2:{}", short_hash("  include: [\"**/*.md\"]"));
    let request = ApplyEditsRequest {
        edit_summary: "exclude Bob from the corpus".into(),
        edits: vec![EditOperation::InsertAfter {
            path: ".mdstore/config.yaml".into(),
            anchor,
            content: "  exclude: [\"bob.md\"]".into(),
        }],
    };
    assert!(config.contains("include:"));
    store.apply_edits(&request).unwrap();
    assert_eq!(store.status().unwrap().pages, 1);
    assert!(store.get_page("bob.md", None).is_err());
}

#[tokio::test]
async fn rebuilds_adjacent_sidecars_and_searches() {
    let repository = Repository::new();
    let store = repository.store();
    store.reindex().await.unwrap();
    assert!(repository.root.join("alice.mdstore").is_file());
    assert!(repository.root.join("bob.mdstore").is_file());
    let status = store.status().unwrap();
    assert_eq!(status.vectors_ready, status.vectors_total);
    let response = store
        .search("Alice", &["person Alice".into()])
        .await
        .unwrap();
    assert_eq!(response.results[0].path, "alice.md");
    assert!(
        response.results[0]
            .matched_arms
            .iter()
            .any(|arm| arm.starts_with("exact:"))
    );
}

#[test]
fn schemas_are_repository_configuration_not_rust_fields() {
    let first = Config::from_yaml(config_yaml()).unwrap();
    let alternate = Config::from_yaml(
        &config_yaml()
            .replace("display_name: /name", "project_code: /code")
            .replace("schema.json", "project-schema.json"),
    )
    .unwrap();
    assert!(first.metadata.contains_key("display_name"));
    assert!(alternate.metadata.contains_key("project_code"));
}

#[test]
fn mcp_allowlist_is_exact() {
    assert_eq!(
        mdstore::mcp::tool_names(),
        ["search", "get_page", "apply_edits"]
    );
}

#[test]
fn configuration_resources_are_hashline_readable() {
    let repository = Repository::new();
    let store = repository.store();
    let response = store
        .get_page(".mdstore/config.yaml", Some((1, 2)))
        .unwrap();
    assert!(response.content.contains("1:"));
    assert!(response.content.contains("documents:"));
    assert_eq!(response.metadata, serde_json::json!({}));
}

#[tokio::test]
async fn mcp_lists_only_three_tools_and_enforces_authentication() {
    let repository = Repository::new();
    let store = repository.store();
    let app = mdstore::mcp::router(store, Some("secret".into()));
    let body = serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"});
    let unauthorized = app
        .clone()
        .oneshot(
            Request::post("/mcp")
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);

    let authorized = app
        .oneshot(
            Request::post("/mcp")
                .header("content-type", "application/json")
                .header("authorization", "Bearer secret")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(authorized.status(), StatusCode::OK);
    let bytes = authorized.into_body().collect().await.unwrap().to_bytes();
    let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    let names: Vec<&str> = value["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["search", "get_page", "apply_edits"]);
}

#[tokio::test]
async fn non_loopback_listener_requires_a_token() {
    let repository = Repository::new();
    let store = repository.store();
    let error = mdstore::mcp::serve(store, "0.0.0.0:0".parse().unwrap(), None)
        .await
        .unwrap_err();
    assert!(error.to_string().contains("bearer token"));
}

#[test]
fn valid_external_commit_is_loaded_before_the_next_write() {
    let repository = Repository::new();
    let store = repository.store();
    fs::write(
        repository.root.join("alice.md"),
        page("Alice").replace("Alice profile.", "Alice external profile."),
    )
    .unwrap();
    command(&repository.root, &["add", "alice.md"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "external valid edit"],
    );
    let request = ApplyEditsRequest {
        edit_summary: "edit Bob after external commit".into(),
        edits: vec![EditOperation::Replace {
            path: "bob.md".into(),
            anchor: format!("6:{}", short_hash("Bob profile.")),
            content: "Bob updated.".into(),
        }],
    };
    store.apply_edits(&request).unwrap();
    assert!(
        store
            .get_page("alice.md", None)
            .unwrap()
            .content
            .contains("external profile")
    );
}

#[test]
fn invalid_external_commit_blocks_writes() {
    let repository = Repository::new();
    let store = repository.store();
    fs::write(
        repository.root.join("alice.md"),
        "# Missing configured metadata\n",
    )
    .unwrap();
    command(&repository.root, &["add", "alice.md"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "external invalid edit"],
    );
    let request = ApplyEditsRequest {
        edit_summary: "attempt write".into(),
        edits: vec![EditOperation::Replace {
            path: "bob.md".into(),
            anchor: format!("6:{}", short_hash("Bob profile.")),
            content: "Bob updated.".into(),
        }],
    };
    assert!(store.apply_edits(&request).is_err());
    assert!(store.status().unwrap().blocked.is_some());
    assert!(
        !fs::read_to_string(repository.root.join("bob.md"))
            .unwrap()
            .contains("updated")
    );
}
