//! End-to-end repository, daemon, MCP, and sidecar behavior tests.

use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use anyhow::Result;
use async_trait::async_trait;
use fs2::FileExt;
use mdstore::{
    ApplyEditsRequest, ApplyStatus, Config, EditOperation, InputType, PushState, RerankResult,
    RetrievalProvider, Store, serve, serve_listener, short_hash, tool_names,
};
use reqwest::StatusCode;
use sha2::{Digest, Sha256};

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

    fn embedding_provider_identity(&self) -> String {
        "fake-provider".into()
    }
}

struct CountingProvider {
    calls: Arc<AtomicUsize>,
}

#[async_trait]
impl RetrievalProvider for CountingProvider {
    async fn embed(&self, input_type: InputType, input: &[String]) -> Result<Vec<Vec<f32>>> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        FakeProvider.embed(input_type, input).await
    }

    async fn rerank(
        &self,
        query: &str,
        documents: &[String],
        top_n: usize,
    ) -> Result<Vec<RerankResult>> {
        FakeProvider.rerank(query, documents, top_n).await
    }

    fn model(&self) -> &str {
        "fake-embed"
    }

    fn dimensions(&self) -> usize {
        2
    }

    fn embedding_provider_identity(&self) -> String {
        "fake-provider".into()
    }
}

struct BlockingProvider {
    started: Arc<tokio::sync::Notify>,
    release: Arc<tokio::sync::Notify>,
}

struct BlockingFailProvider {
    started: Arc<tokio::sync::Notify>,
    release: Arc<tokio::sync::Notify>,
}

struct LogWriter {
    output: Arc<Mutex<Vec<u8>>>,
    buffer: Vec<u8>,
    completed: Arc<tokio::sync::Notify>,
}

impl Write for LogWriter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.buffer.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl Drop for LogWriter {
    fn drop(&mut self) {
        self.output.lock().unwrap().extend_from_slice(&self.buffer);
        self.completed.notify_one();
    }
}

#[async_trait]
impl RetrievalProvider for BlockingFailProvider {
    async fn embed(&self, _input_type: InputType, _input: &[String]) -> Result<Vec<Vec<f32>>> {
        self.started.notify_one();
        self.release.notified().await;
        anyhow::bail!("expected provider failure")
    }

    async fn rerank(
        &self,
        query: &str,
        documents: &[String],
        top_n: usize,
    ) -> Result<Vec<RerankResult>> {
        FakeProvider.rerank(query, documents, top_n).await
    }

    fn model(&self) -> &str {
        "fake-embed"
    }

    fn dimensions(&self) -> usize {
        2
    }

    fn embedding_provider_identity(&self) -> String {
        "fake-provider".into()
    }
}

#[async_trait]
impl RetrievalProvider for BlockingProvider {
    async fn embed(&self, input_type: InputType, input: &[String]) -> Result<Vec<Vec<f32>>> {
        self.started.notify_one();
        self.release.notified().await;
        FakeProvider.embed(input_type, input).await
    }

    async fn rerank(
        &self,
        query: &str,
        documents: &[String],
        top_n: usize,
    ) -> Result<Vec<RerankResult>> {
        FakeProvider.rerank(query, documents, top_n).await
    }

    fn model(&self) -> &str {
        "fake-embed"
    }

    fn dimensions(&self) -> usize {
        2
    }

    fn embedding_provider_identity(&self) -> String {
        "fake-provider".into()
    }
}

struct RecoveringProvider {
    failures: AtomicUsize,
    calls: AtomicUsize,
}

#[async_trait]
impl RetrievalProvider for RecoveringProvider {
    async fn embed(&self, input_type: InputType, input: &[String]) -> Result<Vec<Vec<f32>>> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if self
            .failures
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
            .is_ok()
        {
            anyhow::bail!("temporary embedding outage");
        }
        FakeProvider.embed(input_type, input).await
    }

    async fn rerank(
        &self,
        query: &str,
        documents: &[String],
        top_n: usize,
    ) -> Result<Vec<RerankResult>> {
        FakeProvider.rerank(query, documents, top_n).await
    }

    fn model(&self) -> &str {
        "fake-embed"
    }
    fn dimensions(&self) -> usize {
        2
    }
    fn embedding_provider_identity(&self) -> String {
        "fake-provider".into()
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
        command(&root, &["config", "commit.gpgsign", "false"]);
        fs::write(root.join("config.yaml"), config_yaml()).unwrap();
        fs::write(root.join("template.yaml"), template_yaml()).unwrap();
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
        self.store_with_provider(Arc::new(FakeProvider))
    }

    fn store_with_provider(&self, provider: Arc<dyn RetrievalProvider>) -> Arc<Store> {
        Store::open_with_provider(&self.root, provider).unwrap()
    }
}

async fn start_daemon(
    store: Arc<Store>,
    bearer_token: Option<String>,
) -> (String, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        serve_listener(listener, store, bearer_token).await.unwrap();
    });
    (format!("http://{address}"), server)
}

fn command(root: &Path, arguments: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(root)
        .args(arguments)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {arguments:?} failed: {stderr}",
        stderr = String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

fn config_yaml() -> &'static str {
    r#"documents:
  include: ["**/*.md"]
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

fn template_yaml() -> String {
    format!(
        "frontmatter: {}\nstructure: {{additional_sections: true}}\nmetadata:\n  display_name: /name\nlinks:\n  markdown: true\n  wiki:\n    - '\\[\\[(?P<target>[^\\]|#]+)(?:#[^\\]|]+)?(?:\\|[^\\]]+)?\\]\\]'\nrelations:\n  - name: mentions\n    reciprocal: mentions\n    selector:\n      kind: markdown_links\n",
        serde_json::from_str::<serde_json::Value>(schema()).unwrap()
    )
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
    assert!(command(&repository.root, &["status", "--porcelain"]).is_empty());

    let replay = store.apply_edits(&request).unwrap();
    assert!(matches!(replay.status, ApplyStatus::AlreadyApplied));
}

#[test]
fn superseded_replacement_requires_fresh_edits() {
    let repository = Repository::new();
    let store = repository.store();
    let first = ApplyEditsRequest {
        edit_summary: "first Alice edit".into(),
        edits: vec![EditOperation::Replace {
            path: "alice.md".into(),
            anchor: format!("6:{}", short_hash("Alice profile.")),
            content: "Alice first.".into(),
        }],
    };
    store.apply_edits(&first).unwrap();
    store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "second Alice edit".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice first.")),
                content: "Alice current.".into(),
            }],
        })
        .unwrap();

    let error = store.apply_edits(&first).unwrap_err().to_string();
    assert!(error.contains("partially superseded"));
}

#[test]
fn changed_postimage_requires_fresh_edits() {
    let repository = Repository::new();
    let store = repository.store();
    let request = ApplyEditsRequest {
        edit_summary: "append Alice note".into(),
        edits: vec![EditOperation::InsertAfter {
            path: "alice.md".into(),
            anchor: format!("6:{}", short_hash("Alice profile.")),
            content: "Remember this.".into(),
        }],
    };
    store.apply_edits(&request).unwrap();

    let path = repository.root.join("alice.md");
    let shifted = fs::read_to_string(&path).unwrap().replace(
        "# Notes\n\n",
        "# Notes\n\none\ntwo\nthree\nfour\nfive\nsix\n",
    );
    fs::write(&path, shifted).unwrap();
    command(&repository.root, &["add", "alice.md"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "shift Alice note"],
    );

    let error = store.apply_edits(&request).unwrap_err().to_string();
    assert!(error.contains("partially superseded"));
    assert_eq!(
        fs::read_to_string(path)
            .unwrap()
            .matches("Remember this.")
            .count(),
        1
    );
}

#[test]
fn recreated_page_requires_fresh_edits() {
    let repository = Repository::new();
    let store = repository.store();
    let request = ApplyEditsRequest {
        edit_summary: "create Carol".into(),
        edits: vec![EditOperation::CreatePage {
            path: "carol.md".into(),
            content: page("Carol"),
        }],
    };
    store.apply_edits(&request).unwrap();

    fs::write(repository.root.join("carol.md"), page("Replacement")).unwrap();
    command(&repository.root, &["add", "carol.md"]);
    command(&repository.root, &["commit", "-q", "-m", "recreate Carol"]);

    let error = store.apply_edits(&request).unwrap_err().to_string();
    assert!(error.contains("partially superseded"));
    assert!(
        fs::read_to_string(repository.root.join("carol.md"))
            .unwrap()
            .contains("Replacement profile.")
    );
}

#[test]
fn reverted_request_can_be_applied_again() {
    let repository = Repository::new();
    let store = repository.store();
    let request = ApplyEditsRequest {
        edit_summary: "apply Alice edit".into(),
        edits: vec![EditOperation::Replace {
            path: "alice.md".into(),
            anchor: format!("6:{}", short_hash("Alice profile.")),
            content: "Alice changed.".into(),
        }],
    };
    store.apply_edits(&request).unwrap();

    fs::write(repository.root.join("alice.md"), page("Alice")).unwrap();
    command(&repository.root, &["add", "alice.md"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "revert Alice edit"],
    );

    let response = store.apply_edits(&request).unwrap();
    assert!(matches!(response.status, ApplyStatus::Accepted));
    assert!(
        fs::read_to_string(repository.root.join("alice.md"))
            .unwrap()
            .contains("Alice changed.")
    );
    assert_eq!(
        command(&repository.root, &["log", "-1", "--pretty=%B"]).trim(),
        "apply Alice edit"
    );
}

#[test]
fn partially_reverted_batch_requires_fresh_atomic_edits() {
    let repository = Repository::new();
    let store = repository.store();
    let request = ApplyEditsRequest {
        edit_summary: "change Alice and Bob".into(),
        edits: vec![
            EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "Alice changed.".into(),
            },
            EditOperation::Replace {
                path: "bob.md".into(),
                anchor: format!("6:{}", short_hash("Bob profile.")),
                content: "Bob changed.".into(),
            },
        ],
    };
    store.apply_edits(&request).unwrap();

    fs::write(repository.root.join("alice.md"), page("Alice")).unwrap();
    command(&repository.root, &["add", "alice.md"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "revert only Alice"],
    );

    let error = store.apply_edits(&request).unwrap_err().to_string();
    assert!(error.contains("partially superseded"));
    assert!(
        fs::read_to_string(repository.root.join("bob.md"))
            .unwrap()
            .contains("Bob changed.")
    );
}

#[test]
fn no_op_edit_reports_queued_pushes_without_network() {
    let repository = Repository::new();
    let config_path = repository.root.join("config.yaml");
    let config = fs::read_to_string(&config_path)
        .unwrap()
        .replace("  push: false", "  push: true");
    fs::write(&config_path, config).unwrap();
    command(&repository.root, &["add", "config.yaml"]);
    command(&repository.root, &["commit", "-q", "-m", "enable pushing"]);
    let remote = repository.root.join("remote.git");
    command(
        &repository.root,
        &["init", "--bare", remote.to_str().unwrap()],
    );
    command(
        &repository.root,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    command(&repository.root, &["push", "-u", "origin", "HEAD"]);
    let store = repository.store();
    command(
        &repository.root,
        &[
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "queued external commit",
        ],
    );

    let response = store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "retry queued push".into(),
            edits: vec![EditOperation::Replace {
                path: "bob.md".into(),
                anchor: format!("6:{}", short_hash("Bob profile.")),
                content: "Bob profile.".into(),
            }],
        })
        .unwrap();

    assert!(matches!(response.status, ApplyStatus::AlreadyApplied));
    assert_eq!(response.push, PushState::Queued);
    assert_eq!(store.push().unwrap(), PushState::Pushed);
    assert_eq!(
        command(&repository.root, &["rev-parse", "HEAD"]).trim(),
        command(&remote, &["rev-parse", "HEAD"]).trim()
    );
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
fn directory_templates_are_discoverable_read_only_and_atomic() {
    let repository = Repository::new();
    let template = "instructions: Keep notes concise.\nstructure: {level: 1}\nsections:\n- heading: Notes\n  rules: {required: true, content: paragraphs, paragraphs: {minimum: 1, maximum: 1}}\n";
    fs::write(repository.root.join("template.yaml"), template).unwrap();
    command(&repository.root, &["add", "template.yaml"]);
    command(&repository.root, &["commit", "-qm", "add template"]);
    let store = repository.store();
    let existing = store.get_page("alice.md", None).unwrap();
    assert!(existing.exists);
    assert_eq!(existing.template.unwrap()["path"], "template.yaml");
    let proposed = store.get_page("people/new.md", None).unwrap();
    assert!(!proposed.exists);
    assert_eq!(
        proposed.template.unwrap()["definition"]["instructions"],
        "Keep notes concise."
    );
    assert!(
        store
            .get_page("template.yaml", None)
            .unwrap()
            .content
            .contains("Keep notes concise.")
    );
    let head = command(&repository.root, &["rev-parse", "HEAD"]);
    for edits in [
        vec![EditOperation::Replace {
            path: "template.yaml".into(),
            anchor: format!("1:{}", short_hash("instructions: Keep notes concise.")),
            content: "instructions: Weaken rules".into(),
        }],
        vec![EditOperation::CreatePage {
            path: "people/template.yaml".into(),
            content: "{}".into(),
        }],
        vec![
            EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "One.\n\nTwo.".into(),
            },
            EditOperation::Replace {
                path: "bob.md".into(),
                anchor: format!("6:{}", short_hash("Bob profile.")),
                content: "Valid.".into(),
            },
        ],
    ] {
        assert!(
            store
                .apply_edits(&ApplyEditsRequest {
                    edit_summary: "invalid update".into(),
                    edits
                })
                .is_err()
        );
        assert_eq!(command(&repository.root, &["rev-parse", "HEAD"]), head);
    }
    assert_eq!(
        fs::read_to_string(repository.root.join("bob.md")).unwrap(),
        page("Bob")
    );
    drop(store);
    assert!(
        repository
            .store()
            .get_page("new.md", None)
            .unwrap()
            .template
            .is_some()
    );
}

#[test]
fn invalid_templates_block_startup_even_without_matching_pages() {
    let repository = Repository::new();
    fs::create_dir(repository.root.join("empty")).unwrap();
    fs::write(
        repository.root.join("empty/template.yaml"),
        "sections: [{heading: A, rules: {words: {minimum: 5, maximum: 2}}}]",
    )
    .unwrap();
    command(&repository.root, &["add", "empty/template.yaml"]);
    command(&repository.root, &["commit", "-qm", "invalid template"]);
    assert!(Store::open_with_provider(&repository.root, Arc::new(FakeProvider)).is_err());
}

#[test]
fn template_recovery_and_invalid_external_activation() {
    let repository = Repository::new();
    fs::create_dir(repository.root.join("stray")).unwrap();
    fs::write(
        repository.root.join("stray/template.yaml"),
        "unknown: untracked",
    )
    .unwrap();
    let store = repository.store();
    assert!(!repository.root.join("stray/template.yaml").exists());
    assert!(store.get_page("alice.md", None).unwrap().template.is_some());
    fs::write(
        repository.root.join("template.yaml"),
        "sections: [{heading: Missing, rules: {required: true}}]",
    )
    .unwrap();
    command(&repository.root, &["add", "template.yaml"]);
    command(
        &repository.root,
        &["commit", "-qm", "incompatible template"],
    );
    assert!(store.push().is_err());
    assert!(store.get_page("alice.md", None).unwrap().template.is_some());
}

#[test]
fn structured_sections_validate_activation_and_atomic_batches() {
    let repository = Repository::new();
    let template = format!(
        "{}sections:\n- heading: Notes\n  rules:\n    required: true\n    list: {{minimum_items: 1, date_order: descending}}\n",
        template_yaml()
    );
    fs::write(repository.root.join("template.yaml"), template).unwrap();
    command(&repository.root, &["add", "template.yaml"]);
    command(
        &repository.root,
        &["commit", "-qm", "require dated entries"],
    );
    assert!(Store::open_with_provider(&repository.root, Arc::new(FakeProvider)).is_err());
    for name in ["Alice", "Bob"] {
        fs::write(
            repository.root.join(format!("{}.md", name.to_lowercase())),
            page(name).replace(&format!("{name} profile."), "- 2024-01-01 Created"),
        )
        .unwrap();
    }
    command(&repository.root, &["add", "."]);
    command(&repository.root, &["commit", "-qm", "author valid entries"]);
    let store = repository.store();
    let head = command(&repository.root, &["rev-parse", "HEAD"]);
    assert!(
        store
            .apply_edits(&ApplyEditsRequest {
                edit_summary: "invalid dated batch".into(),
                edits: [
                    ("alice.md", "- 2024-02-01 Valid"),
                    ("bob.md", "- 2024-02-30 Invalid")
                ]
                .iter()
                .map(|(path, content)| EditOperation::Replace {
                    path: (*path).into(),
                    anchor: format!("6:{}", short_hash("- 2024-01-01 Created")),
                    content: (*content).into(),
                })
                .collect(),
            })
            .is_err()
    );
    assert_eq!(command(&repository.root, &["rev-parse", "HEAD"]), head);
}

#[test]
fn markdown_style_activation_and_edits_are_atomic() {
    let repository = Repository::new();
    fs::write(
        repository.root.join("template.yaml"),
        format!("{}markdown: {{max_line_length: 5}}\n", template_yaml()),
    )
    .unwrap();
    command(&repository.root, &["add", "template.yaml"]);
    command(&repository.root, &["commit", "-qm", "invalid style policy"]);
    assert!(Store::open_with_provider(&repository.root, Arc::new(FakeProvider)).is_err());
    fs::write(
        repository.root.join("template.yaml"),
        format!(
            "{}markdown: {{no_trailing_whitespace: true, closed_fences: true}}\n",
            template_yaml()
        ),
    )
    .unwrap();
    command(&repository.root, &["add", "template.yaml"]);
    command(&repository.root, &["commit", "-qm", "valid style policy"]);
    let store = repository.store();
    let head = command(&repository.root, &["rev-parse", "HEAD"]);
    let error = store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "invalid multi-page style".into(),
            edits: [
                ("alice.md", "Alice profile.", "Valid."),
                ("bob.md", "Bob profile.", "Invalid. "),
            ]
            .iter()
            .map(|(path, old, content)| EditOperation::Replace {
                path: (*path).into(),
                anchor: format!("6:{}", short_hash(old)),
                content: (*content).into(),
            })
            .collect(),
        })
        .unwrap_err();
    let findings = &error
        .downcast_ref::<mdstore::ValidationError>()
        .unwrap()
        .findings;
    assert_eq!(findings.len(), 1);
    assert_eq!(findings[0].path, "bob.md");
    assert_eq!(findings[0].line, Some(6));
    assert_eq!(command(&repository.root, &["rev-parse", "HEAD"]), head);
}

#[test]
fn edit_interface_preserves_untracked_non_markdown_files() {
    let repository = Repository::new();
    let store = repository.store();
    let path = repository.root.join("local.json");
    fs::write(&path, "local untracked bytes\n").unwrap();
    let head = command(&repository.root, &["rev-parse", "HEAD"]);

    let error = store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "create config resource".into(),
            edits: vec![EditOperation::CreatePage {
                path: "local.json".into(),
                content: "{}\n".into(),
            }],
        })
        .unwrap_err();

    assert!(error.to_string().contains("read-only"));
    assert_eq!(fs::read_to_string(path).unwrap(), "local untracked bytes\n");
    assert_eq!(command(&repository.root, &["rev-parse", "HEAD"]), head);
}

#[test]
fn configuration_activation_reselects_the_tracked_corpus() {
    let repository = Repository::new();
    let store = repository.store();
    let config = config_yaml().replace("documents:", "documents:\n  exclude: ['bob.md']");
    fs::write(repository.root.join("config.yaml"), config).unwrap();
    command(&repository.root, &["add", "config.yaml"]);
    command(
        &repository.root,
        &["commit", "-qm", "administrative corpus selection"],
    );
    store.push().unwrap();
    assert_eq!(store.status().unwrap().pages, 1);
    assert!(store.get_page("bob.md", None).is_err());
}

#[test]
fn configured_markdown_under_mdstore_is_editable_content() {
    let repository = Repository::new();
    fs::create_dir(repository.root.join(".mdstore")).unwrap();
    fs::write(repository.root.join(".mdstore/note.md"), page("Internal")).unwrap();
    command(&repository.root, &["add", ".mdstore/note.md"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "add internal note"],
    );
    let store = repository.store();

    assert!(
        store
            .get_page(".mdstore/note.md", None)
            .unwrap()
            .content
            .contains("Internal profile.")
    );
    store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "update internal note".into(),
            edits: vec![EditOperation::Replace {
                path: ".mdstore/note.md".into(),
                anchor: format!("6:{}", short_hash("Internal profile.")),
                content: "Internal updated.".into(),
            }],
        })
        .unwrap();

    assert!(
        fs::read_to_string(repository.root.join(".mdstore/note.md"))
            .unwrap()
            .contains("Internal updated.")
    );
}

#[test]
fn rejects_edits_to_tracked_markdown_outside_the_configured_corpus() {
    let repository = Repository::new();
    fs::write(repository.root.join("excluded.md"), "private\n").unwrap();
    let config = fs::read_to_string(repository.root.join("config.yaml"))
        .unwrap()
        .replace(
            "  include: [\"**/*.md\"]",
            "  include: [\"**/*.md\"]\n  exclude: [\"excluded.md\"]",
        );
    fs::write(repository.root.join("config.yaml"), config).unwrap();
    command(&repository.root, &["add", "config.yaml", "excluded.md"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "add excluded markdown"],
    );
    let store = repository.store();
    let request = ApplyEditsRequest {
        edit_summary: "remove excluded file".into(),
        edits: vec![EditOperation::RemovePage {
            path: "excluded.md".into(),
            anchor: format!("1:{}", short_hash("private")),
        }],
    };

    let error = store.apply_edits(&request).unwrap_err().to_string();
    assert!(error.contains("outside configured document globs"));
    assert!(repository.root.join("excluded.md").is_file());
}

#[cfg(unix)]
#[test]
fn rejects_tracked_markdown_symlinks() {
    use std::os::unix::fs::symlink;

    let repository = Repository::new();
    let external = tempfile::NamedTempFile::new().unwrap();
    fs::write(external.path(), page("Outside")).unwrap();
    symlink(external.path(), repository.root.join("linked.md")).unwrap();
    command(&repository.root, &["add", "linked.md"]);
    command(&repository.root, &["commit", "-q", "-m", "add linked page"]);
    let opened = Store::open_with_provider(&repository.root, Arc::new(FakeProvider));
    assert!(format!("{:#}", opened.err().unwrap()).contains("may not traverse a symlink"));
}

#[test]
fn startup_verifies_repository_ownership_before_recovery() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path();
    command(root, &["init", "-q", "-b", "main"]);
    command(root, &["config", "user.name", "mdstore test"]);
    command(root, &["config", "user.email", "mdstore@example.invalid"]);
    command(root, &["config", "commit.gpgsign", "false"]);
    fs::write(root.join("unrelated.txt"), "committed\n").unwrap();
    command(root, &["add", "unrelated.txt"]);
    command(root, &["commit", "-q", "-m", "initial"]);
    fs::write(root.join("unrelated.txt"), "dirty\n").unwrap();

    let error = Store::open(root).err().unwrap().to_string();

    assert!(error.contains("config.yaml must be tracked"));
    assert_eq!(
        fs::read_to_string(root.join("unrelated.txt")).unwrap(),
        "dirty\n"
    );
}

#[test]
fn reopened_state_reads_canonical_blobs_not_smudged_worktree_text() {
    let repository = Repository::new();
    fs::write(
        repository.root.join(".gitattributes"),
        "*.md filter=lowercase\n",
    )
    .unwrap();
    command(
        &repository.root,
        &["config", "filter.lowercase.clean", "tr a-z A-Z"],
    );
    command(
        &repository.root,
        &["config", "filter.lowercase.smudge", "tr A-Z a-z"],
    );
    command(
        &repository.root,
        &["config", "filter.lowercase.required", "true"],
    );
    command(&repository.root, &["add", ".gitattributes"]);
    command(&repository.root, &["commit", "-q", "-m", "add filters"]);
    let store = Store::open(&repository.root).unwrap();
    store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "preserve mixed case".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "MiXeD profile.".into(),
            }],
        })
        .unwrap();
    drop(store);

    let reopened = Store::open(&repository.root).unwrap();
    assert!(
        reopened
            .get_page("alice.md", None)
            .unwrap()
            .content
            .contains("MiXeD profile.")
    );
}

#[test]
fn rejects_tracked_embedding_sidecars() {
    let repository = Repository::new();
    fs::write(
        repository.root.join("alice.mdstore"),
        b"tracked derived state",
    )
    .unwrap();
    command(&repository.root, &["add", "-f", "alice.mdstore"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "track forbidden sidecar"],
    );
    let opened = Store::open_with_provider(&repository.root, Arc::new(FakeProvider));
    assert!(format!("{:#}", opened.err().unwrap()).contains("must be ignored and untracked"));
}

#[test]
fn rejects_unignored_sidecar_paths_before_committing_a_new_page() {
    let repository = Repository::new();
    fs::write(
        repository.root.join(".gitignore"),
        "alice.mdstore\nbob.mdstore\n!.mdstore/\n",
    )
    .unwrap();
    command(&repository.root, &["add", ".gitignore"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "ignore existing sidecars only"],
    );
    let store = repository.store();
    let before = command(&repository.root, &["rev-parse", "HEAD"]);
    let error = store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "create Charlie".into(),
            edits: vec![EditOperation::CreatePage {
                path: "charlie.md".into(),
                content: page("Charlie"),
            }],
        })
        .unwrap_err()
        .to_string();

    assert!(error.contains("charlie.mdstore"));
    assert_eq!(command(&repository.root, &["rev-parse", "HEAD"]), before);
    assert!(!repository.root.join("charlie.md").exists());
    assert_eq!(store.status().unwrap().pages, 2);
}

#[tokio::test]
async fn reindex_checks_sidecar_ignore_rules_before_provider_calls() {
    let repository = Repository::new();
    let calls = Arc::new(AtomicUsize::new(0));
    let store = repository.store_with_provider(Arc::new(CountingProvider {
        calls: calls.clone(),
    }));
    fs::write(repository.root.join(".gitignore"), "!.mdstore/\n").unwrap();

    assert!(store.reindex_missing().await.is_err());
    assert_eq!(calls.load(Ordering::SeqCst), 0);
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

#[tokio::test]
async fn reindex_removes_sidecars_without_a_current_page() {
    let repository = Repository::new();
    let store = repository.store();
    store.reindex().await.unwrap();
    let sidecar = repository.root.join("alice.mdstore");
    assert!(sidecar.is_file());

    fs::remove_file(repository.root.join("alice.md")).unwrap();
    command(&repository.root, &["add", "-A", "alice.md"]);
    command(&repository.root, &["commit", "-q", "-m", "remove Alice"]);
    store.push().unwrap();
    store.reindex_missing().await.unwrap();

    assert!(!sidecar.exists());
}

#[test]
fn schemas_are_repository_configuration_not_rust_fields() {
    let repository = Repository::new();
    fs::create_dir(repository.root.join("projects")).unwrap();
    fs::write(repository.root.join("projects/template.yaml"), "structure: {additional_sections: true}\nfrontmatter: {type: object, required: [code]}\nmetadata: {project_code: /code}\n").unwrap();
    fs::write(
        repository.root.join("projects/a.md"),
        "---\ncode: A\n---\n# Project\n",
    )
    .unwrap();
    command(&repository.root, &["add", "."]);
    command(
        &repository.root,
        &["commit", "-qm", "different folder schema"],
    );
    let store = repository.store();
    assert_eq!(
        store.get_page("alice.md", None).unwrap().metadata["display_name"],
        "Alice"
    );
    assert_eq!(
        store.get_page("projects/a.md", None).unwrap().metadata["project_code"],
        "A"
    );
}

#[test]
fn mcp_allowlist_is_exact() {
    assert_eq!(tool_names(), ["search", "get_page", "apply_edits"]);
}

#[test]
fn configuration_resources_are_hashline_readable() {
    let repository = Repository::new();
    let store = repository.store();
    for path in ["config.yaml", "template.yaml"] {
        let response = store.get_page(path, Some((1, 2))).unwrap();
        assert!(response.content.starts_with("1:"));
        assert_eq!(response.metadata, serde_json::json!({}));
        let text = fs::read_to_string(repository.root.join(path)).unwrap();
        let first = text.lines().next().unwrap();
        assert!(
            store
                .apply_edits(&ApplyEditsRequest {
                    edit_summary: "weaken configuration".into(),
                    edits: vec![EditOperation::Replace {
                        path: path.into(),
                        anchor: format!("1:{}", short_hash(first)),
                        content: "{}".into()
                    }],
                })
                .unwrap_err()
                .to_string()
                .contains("read-only")
        );
    }
    assert!(!repository.root.join(".mdstore").exists());
}

#[test]
fn dirty_and_staged_configuration_do_not_leak_into_the_published_state() {
    let repository = Repository::new();
    let store = repository.store();
    let original = store.get_page("template.yaml", None).unwrap().content;

    fs::write(repository.root.join("template.yaml"), "not valid JSON\n").unwrap();
    assert!(store.validate().is_ok());
    assert_eq!(
        store.get_page("template.yaml", None).unwrap().content,
        original
    );

    command(&repository.root, &["add", "template.yaml"]);
    assert!(store.validate().is_ok());
    assert_eq!(
        store.get_page("template.yaml", None).unwrap().content,
        original
    );
}

#[test]
fn injected_provider_open_uses_committed_configuration() {
    let repository = Repository::new();
    fs::write(
        repository.root.join("config.yaml"),
        config_yaml().replace("dimensions: 2", "dimensions: 99"),
    )
    .unwrap();

    let store = Store::open_with_provider(&repository.root, Arc::new(FakeProvider)).unwrap();

    assert_eq!(store.config().provider.dimensions, 2);
}

#[tokio::test]
async fn mcp_lists_only_three_tools_and_enforces_authentication() {
    let repository = Repository::new();
    let store = repository.store();
    let (base_url, server) = start_daemon(store.clone(), Some("secret".into())).await;
    let client = reqwest::Client::new();
    let body = serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"});
    let unauthorized = client
        .post(format!("{base_url}/mcp"))
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);

    let health = client
        .get(format!("{base_url}/health"))
        .send()
        .await
        .unwrap();
    assert_eq!(health.status(), StatusCode::UNAUTHORIZED);

    let authorized = client
        .post(format!("{base_url}/mcp"))
        .bearer_auth("secret")
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(authorized.status(), StatusCode::OK);
    let bytes = authorized.bytes().await.unwrap();
    let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    let names: Vec<&str> = value["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["search", "get_page", "apply_edits"]);
    let apply = value["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .find(|tool| tool["name"] == "apply_edits")
        .unwrap();
    let variants = apply["inputSchema"]["properties"]["edits"]["items"]["oneOf"]
        .as_array()
        .unwrap();
    assert_eq!(variants.len(), 6);
    let replace = variants
        .iter()
        .find(|schema| schema["properties"]["op"]["const"] == "replace")
        .unwrap();
    assert_eq!(
        replace["required"],
        serde_json::json!(["op", "path", "anchor", "content"])
    );

    let health = client
        .get(format!("{base_url}/health"))
        .bearer_auth("secret")
        .send()
        .await
        .unwrap();
    assert_eq!(health.status(), StatusCode::OK);
    server.abort();

    let (base_url, server) = start_daemon(store, None).await;
    for (path, body) in [
        (
            "/mcp",
            serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        ),
        ("/cli", serde_json::json!({"command": "validate"})),
    ] {
        let response = client
            .post(format!("{base_url}{path}"))
            .header("origin", "https://attacker.example")
            .json(&body)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
    }
    server.abort();
}

#[tokio::test]
async fn mcp_validates_protocol_and_json_rpc_envelopes() {
    let repository = Repository::new();
    let (base_url, server) = start_daemon(repository.store(), None).await;
    let client = reqwest::Client::new();

    let unsupported = client
        .post(format!("{base_url}/mcp"))
        .header("mcp-protocol-version", "2099-01-01")
        .json(&serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        .send()
        .await
        .unwrap();
    assert_eq!(unsupported.status(), StatusCode::BAD_REQUEST);

    let malformed = client
        .post(format!("{base_url}/mcp"))
        .header("content-type", "application/json")
        .body("{")
        .send()
        .await
        .unwrap();
    assert_eq!(malformed.status(), StatusCode::OK);
    let malformed: serde_json::Value = malformed.json().await.unwrap();
    assert_eq!(malformed["error"]["code"], -32700);

    for body in [
        serde_json::json!({"id": 1, "method": "tools/list"}),
        serde_json::json!({"jsonrpc": "1.0", "id": 1, "method": "tools/list"}),
        serde_json::json!({"jsonrpc": "2.0", "id": true, "method": "tools/list"}),
        serde_json::json!({"jsonrpc": "2.0", "id": null, "method": "tools/list"}),
        serde_json::json!({"jsonrpc": "2.0", "id": 1.5, "method": "tools/list"}),
        serde_json::json!({"jsonrpc": "2.0", "id": 1}),
        serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": true}),
    ] {
        let response = client
            .post(format!("{base_url}/mcp"))
            .json(&body)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let value: serde_json::Value = response.json().await.unwrap();
        assert_eq!(value["error"]["code"], -32600);
    }

    for requested in ["2025-03-26", "2025-06-18"] {
        let response = client
            .post(format!("{base_url}/mcp"))
            .json(&serde_json::json!({
                "jsonrpc": "2.0",
                "id": "initialize",
                "method": "initialize",
                "params": {
                    "protocolVersion": requested,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"}
                }
            }))
            .send()
            .await
            .unwrap();
        let value: serde_json::Value = response.json().await.unwrap();
        assert_eq!(value["result"]["protocolVersion"], requested);
    }

    let notification = client
        .post(format!("{base_url}/mcp"))
        .header("mcp-protocol-version", "2025-06-18")
        .json(&serde_json::json!({
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(notification.status(), StatusCode::ACCEPTED);
    assert!(notification.bytes().await.unwrap().is_empty());
    server.abort();
}

#[tokio::test]
async fn mcp_apply_errors_preserve_structured_validation_findings() {
    let repository = Repository::new();
    let (base_url, server) = start_daemon(repository.store(), None).await;
    let body = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "apply_edits",
            "arguments": {
                "edit_summary": "create invalid page",
                "edits": [{
                    "op": "create_page",
                    "path": "invalid.md",
                    "content": "# Missing frontmatter\n"
                }]
            }
        }
    });
    let response = reqwest::Client::new()
        .post(format!("{base_url}/mcp"))
        .json(&body)
        .send()
        .await
        .unwrap();
    let value: serde_json::Value = response.json().await.unwrap();
    assert_eq!(value["result"]["isError"], true);
    let findings = value["result"]["structuredContent"]["validation_findings"]
        .as_array()
        .unwrap();
    assert!(
        findings
            .iter()
            .any(|finding| finding["path"] == "invalid.md")
    );
    server.abort();
}

#[tokio::test]
async fn non_loopback_listener_requires_a_token() {
    let repository = Repository::new();
    let store = repository.store();
    let error = serve(store, "0.0.0.0:0".parse().unwrap(), None)
        .await
        .unwrap_err();
    assert!(error.to_string().contains("bearer token"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cli_status_uses_the_running_daemon() {
    let repository = Repository::new();
    let store = repository.store();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { serve_listener(listener, store, None).await.unwrap() });
    fs::write(repository.root.join("alice.md"), "uncommitted local text\n").unwrap();
    let root = repository.root.clone();
    let output = tokio::task::spawn_blocking(move || {
        Command::new(env!("CARGO_BIN_EXE_mdstore"))
            .args([
                "--root",
                root.to_str().unwrap(),
                "--daemon-url",
                &format!("http://{address}"),
                "status",
            ])
            .output()
            .unwrap()
    })
    .await
    .unwrap();
    server.abort();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let status: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(status["pages"], 2);
    assert_eq!(
        fs::read_to_string(repository.root.join("alice.md")).unwrap(),
        "uncommitted local text\n"
    );
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
fn edit_interface_cannot_change_server_configuration() {
    let repository = Repository::new();
    let store = repository.store();
    let config = fs::read_to_string(repository.root.join("config.yaml")).unwrap();
    let line = config
        .lines()
        .position(|line| line == "  listen: 127.0.0.1:3131")
        .unwrap()
        + 1;
    let error = store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "change live server address".into(),
            edits: vec![EditOperation::Replace {
                path: "config.yaml".into(),
                anchor: format!("{line}:{}", short_hash("  listen: 127.0.0.1:3131")),
                content: "  listen: 127.0.0.1:4141".into(),
            }],
        })
        .unwrap_err();
    assert!(error.to_string().contains("read-only"));
    assert_eq!(
        fs::read_to_string(repository.root.join("config.yaml")).unwrap(),
        config
    );
}

#[test]
fn external_server_configuration_change_requires_restart() {
    let repository = Repository::new();
    let store = repository.store();
    let config = fs::read_to_string(repository.root.join("config.yaml")).unwrap();
    fs::write(
        repository.root.join("config.yaml"),
        config.replace("127.0.0.1:3131", "127.0.0.1:4141"),
    )
    .unwrap();
    command(&repository.root, &["add", "config.yaml"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "change server address"],
    );
    let error = store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "write after server change".into(),
            edits: vec![EditOperation::Replace {
                path: "bob.md".into(),
                anchor: format!("6:{}", short_hash("Bob profile.")),
                content: "Bob updated.".into(),
            }],
        })
        .unwrap_err();
    assert!(error.to_string().contains("restart required"));
}

#[test]
fn manual_push_uses_the_repository_write_lock() {
    let repository = Repository::new();
    let store = repository.store();
    let lock_path = repository.root.join(".git/mdstore/write.lock");
    fs::create_dir_all(lock_path.parent().unwrap()).unwrap();
    let lock = fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(lock_path)
        .unwrap();
    lock.lock_exclusive().unwrap();

    let (sender, receiver) = std::sync::mpsc::channel();
    let handle = std::thread::spawn(move || sender.send(store.push()).unwrap());
    assert!(
        receiver
            .recv_timeout(std::time::Duration::from_millis(100))
            .is_err()
    );
    FileExt::unlock(&lock).unwrap();
    assert!(matches!(
        receiver
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap()
            .unwrap(),
        PushState::Disabled
    ));
    handle.join().unwrap();
}

#[test]
fn manual_push_refreshes_external_configuration_before_selecting_push_settings() {
    let repository = Repository::new();
    let store = repository.store();
    let schema = format!("\n{}", template_yaml());
    fs::write(repository.root.join("template.yaml"), &schema).unwrap();
    command(&repository.root, &["add", "template.yaml"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "external schema formatting"],
    );

    assert!(matches!(store.push().unwrap(), PushState::Disabled));
    assert!(
        store
            .get_page("template.yaml", None)
            .unwrap()
            .content
            .lines()
            .next()
            .unwrap()
            .ends_with('|')
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

    fs::write(repository.root.join("alice.md"), page("Alice")).unwrap();
    command(&repository.root, &["add", "alice.md"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "repair external edit"],
    );
    store.apply_edits(&request).unwrap();
    assert!(store.status().unwrap().blocked.is_none());
}

#[test]
fn external_failures_never_replace_a_divergence_block() {
    let repository = Repository::new();
    fs::create_dir_all(repository.root.join(".git/mdstore")).unwrap();
    fs::write(
        repository.root.join(".git/mdstore/blocked"),
        "remote history diverged",
    )
    .unwrap();
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
    assert_eq!(
        store.status().unwrap().blocked.as_deref(),
        Some("remote history diverged")
    );

    fs::write(repository.root.join("alice.md"), page("Alice")).unwrap();
    command(&repository.root, &["add", "alice.md"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "repair external edit"],
    );
    let error = store.apply_edits(&request).unwrap_err();
    assert!(error.to_string().contains("remote history diverged"));
    assert_eq!(
        store.status().unwrap().blocked.as_deref(),
        Some("remote history diverged")
    );
}

#[test]
fn fresh_hashlines_cover_middle_edits() {
    let repository = Repository::new();
    let mut text = "---\nname: Alice\n---\n# Notes\n".to_owned();
    for number in 1..=50 {
        text.push_str(&format!("line {number}\n"));
    }
    fs::write(repository.root.join("alice.md"), &text).unwrap();
    command(&repository.root, &["add", "alice.md"]);
    command(&repository.root, &["commit", "-q", "-m", "long page"]);
    let store = repository.store();
    let line = text.lines().position(|value| value == "line 30").unwrap() + 1;
    let response = store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "edit middle line".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("{line}:{}", short_hash("line 30")),
                content: "changed middle".into(),
            }],
        })
        .unwrap();
    let fresh = &response.fresh_hashlines["alice.md"];
    assert!(fresh.contains(&format!("{line}:")));
    assert!(fresh.contains("changed middle"));
    assert!(!fresh.lines().any(|line| line.starts_with("1:")));
}

#[test]
fn edit_interface_cannot_remove_templates() {
    let repository = Repository::new();
    let store = repository.store();
    let schema = fs::read_to_string(repository.root.join("template.yaml")).unwrap();
    let count = schema.lines().count();
    let first = schema.lines().next().unwrap();
    let last = schema.lines().last().unwrap();
    let request = ApplyEditsRequest {
        edit_summary: "remove active schema".into(),
        edits: vec![EditOperation::RemovePage {
            path: "template.yaml".into(),
            anchor: format!("1:{}..{count}:{}", short_hash(first), short_hash(last)),
        }],
    };
    assert!(store.apply_edits(&request).is_err());
    assert!(repository.root.join("template.yaml").is_file());
}

#[test]
fn a_late_materialization_failure_rolls_back_earlier_paths() {
    let repository = Repository::new();
    fs::create_dir(repository.root.join("blocked.md")).unwrap();
    let store = repository.store();
    let request = ApplyEditsRequest {
        edit_summary: "failing two page edit".into(),
        edits: vec![
            EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "Alice changed.".into(),
            },
            EditOperation::CreatePage {
                path: "blocked.md".into(),
                content: page("Blocked"),
            },
        ],
    };
    assert!(store.apply_edits(&request).is_err());
    assert!(
        fs::read_to_string(repository.root.join("alice.md"))
            .unwrap()
            .contains("Alice profile.")
    );
    assert!(command(&repository.root, &["status", "--porcelain"]).is_empty());
}

#[test]
fn persisted_write_blocks_survive_restart() {
    let repository = Repository::new();
    fs::create_dir_all(repository.root.join(".git/mdstore")).unwrap();
    fs::write(
        repository.root.join(".git/mdstore/blocked"),
        "remote history diverged",
    )
    .unwrap();
    let store = repository.store();
    assert_eq!(
        store.status().unwrap().blocked.as_deref(),
        Some("remote history diverged")
    );
    let request = ApplyEditsRequest {
        edit_summary: "blocked edit".into(),
        edits: vec![EditOperation::Replace {
            path: "alice.md".into(),
            anchor: format!("6:{}", short_hash("Alice profile.")),
            content: "Alice changed.".into(),
        }],
    };
    assert!(store.apply_edits(&request).is_err());
}

#[test]
fn pending_tree_receipts_recover_committed_requests() {
    let repository = Repository::new();
    let request = ApplyEditsRequest {
        edit_summary: "recover committed edit".into(),
        edits: vec![EditOperation::Replace {
            path: "bob.md".into(),
            anchor: format!("6:{}", short_hash("Bob profile.")),
            content: "Bob recovered.".into(),
        }],
    };
    let digest = format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&request).unwrap())
    );
    let base = command(&repository.root, &["rev-parse", "HEAD"])
        .trim()
        .to_owned();
    fs::write(
        repository.root.join("bob.md"),
        page("Bob").replace("Bob profile.", "Bob recovered."),
    )
    .unwrap();
    command(&repository.root, &["add", "bob.md"]);
    let tree = command(&repository.root, &["write-tree"]).trim().to_owned();
    let pending = repository
        .root
        .join(".git/mdstore/pending")
        .join(format!("{digest}.json"));
    fs::create_dir_all(pending.parent().unwrap()).unwrap();
    fs::write(
        pending,
        serde_json::to_vec(&serde_json::json!({
            "base_head": base,
            "tree": tree,
            "touched_paths": ["bob.md"],
            "preimages": {"bob.md": page("Bob")},
            "postimages": {
                "bob.md": page("Bob").replace("Bob profile.", "Bob recovered.")
            }
        }))
        .unwrap(),
    )
    .unwrap();
    command(
        &repository.root,
        &["commit", "-q", "-m", "recover committed edit"],
    );
    let store = repository.store();
    let response = store.apply_edits(&request).unwrap();
    assert!(matches!(response.status, ApplyStatus::AlreadyApplied));
    assert!(
        !repository
            .root
            .join(".git/mdstore/pending")
            .join(format!("{digest}.json"))
            .exists()
    );
}

#[tokio::test]
async fn startup_reindex_reuses_valid_sidecars() {
    let repository = Repository::new();
    let calls = Arc::new(AtomicUsize::new(0));
    let store = repository.store_with_provider(Arc::new(CountingProvider {
        calls: calls.clone(),
    }));
    store.reindex_missing().await.unwrap();
    let first = calls.load(Ordering::SeqCst);
    assert!(first > 0);
    store.reindex_missing().await.unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), first);
}

#[tokio::test]
async fn post_edit_reindex_reuses_still_valid_sidecars() {
    let repository = Repository::new();
    let calls = Arc::new(AtomicUsize::new(0));
    let store = repository.store_with_provider(Arc::new(CountingProvider {
        calls: calls.clone(),
    }));
    store.reindex_missing().await.unwrap();
    let embedded = calls.load(Ordering::SeqCst);
    store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "repeat unchanged document edit".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "Alice profile.".into(),
            }],
        })
        .unwrap();
    store.reindex_missing().await.unwrap();
    assert_eq!(calls.load(Ordering::SeqCst), embedded);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stale_reindex_work_is_discarded_after_an_edit() {
    let repository = Repository::new();
    let started = Arc::new(tokio::sync::Notify::new());
    let release = Arc::new(tokio::sync::Notify::new());
    let store = repository.store_with_provider(Arc::new(BlockingProvider {
        started: started.clone(),
        release: release.clone(),
    }));
    let reindex = {
        let store = store.clone();
        tokio::spawn(async move { store.reindex().await })
    };
    started.notified().await;
    store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "edit during reindex".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "Alice changed.".into(),
            }],
        })
        .unwrap();
    release.notify_waiters();
    reindex.await.unwrap().unwrap();
    assert_eq!(store.status().unwrap().vectors_ready, 0);
    assert!(!repository.root.join("alice.mdstore").exists());
}

#[cfg(unix)]
#[tokio::test]
async fn stalled_push_keeps_http_and_index_publication_responsive() {
    use std::os::unix::fs::PermissionsExt;

    let repository = Repository::new();
    let remote = tempfile::tempdir().unwrap();
    command(remote.path(), &["init", "--bare", "."]);
    command(
        &repository.root,
        &["remote", "add", "origin", remote.path().to_str().unwrap()],
    );
    let config_path = repository.root.join("config.yaml");
    let config = fs::read_to_string(&config_path).unwrap().replace(
        "  push: false",
        "  push: true\n  remote: origin\n  push_timeout_seconds: 5",
    );
    fs::write(&config_path, config).unwrap();
    command(&repository.root, &["add", "config.yaml"]);
    command(
        &repository.root,
        &["commit", "-q", "-m", "enable delayed push"],
    );
    command(&repository.root, &["push", "-u", "origin", "HEAD"]);

    let hook = remote.path().join("hooks/pre-receive");
    fs::write(&hook, "#!/bin/sh\n: > \"$0.started\"\nsleep 2\n").unwrap();
    let mut permissions = fs::metadata(&hook).unwrap().permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&hook, permissions).unwrap();
    let hook_started = remote.path().join("hooks/pre-receive.started");

    let store = repository.store();
    let (base_url, server) = start_daemon(store.clone(), None).await;
    let body = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "apply_edits",
            "arguments": {
                "edit_summary": "exercise delayed push",
                "edits": [{
                    "op": "replace",
                    "path": "alice.md",
                    "anchor": format!("6:{}", short_hash("Alice profile.")),
                    "content": "Alice updated."
                }]
            }
        }
    });
    let client = reqwest::Client::new();
    let apply_url = format!("{base_url}/mcp");
    let apply_client = client.clone();
    let apply = tokio::spawn(async move {
        apply_client
            .post(apply_url)
            .json(&body)
            .send()
            .await
            .unwrap()
    });
    tokio::time::timeout(Duration::from_secs(10), async {
        while !hook_started.exists() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("push must start without blocking the runtime");

    let health = tokio::time::timeout(
        Duration::from_millis(500),
        client.get(format!("{base_url}/health")).send(),
    )
    .await
    .expect("health must remain responsive during push")
    .unwrap();
    assert_eq!(health.status(), StatusCode::OK);
    let second_store = store.clone();
    tokio::time::timeout(
        Duration::from_millis(500),
        tokio::task::spawn_blocking(move || {
            second_store.apply_edits(&ApplyEditsRequest {
                edit_summary: "edit while push is stalled".into(),
                edits: vec![EditOperation::Replace {
                    path: "bob.md".into(),
                    anchor: format!("6:{}", short_hash("Bob profile.")),
                    content: "Bob updated concurrently.".into(),
                }],
            })
        }),
    )
    .await
    .expect("network push must not hold the edit lock")
    .unwrap()
    .unwrap();
    tokio::time::timeout(Duration::from_millis(500), store.reindex_missing())
        .await
        .expect("reindex must publish while push waits")
        .unwrap();

    let response = tokio::time::timeout(Duration::from_secs(5), apply)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    server.abort();
}

fn sync_repository() -> (Repository, tempfile::TempDir) {
    let repository = Repository::new();
    let remote = tempfile::tempdir().unwrap();
    command(remote.path(), &["init", "--bare", "."]);
    command(
        &repository.root,
        &["remote", "add", "origin", remote.path().to_str().unwrap()],
    );
    let config_path = repository.root.join("config.yaml");
    let config = fs::read_to_string(&config_path).unwrap().replace(
        "  push: false",
        "  push: true\n  remote: origin\n  push_timeout_seconds: 2",
    );
    fs::write(config_path, config).unwrap();
    command(&repository.root, &["add", "config.yaml"]);
    command(&repository.root, &["commit", "-qm", "enable sync"]);
    (repository, remote)
}

fn external_clone(repository: &Repository, remote: &Path, directory: &Path) -> PathBuf {
    let clone = directory.join("external");
    let branch = command(&repository.root, &["branch", "--show-current"]);
    command(
        directory,
        &[
            "clone",
            "-b",
            branch.trim(),
            remote.to_str().unwrap(),
            clone.to_str().unwrap(),
        ],
    );
    command(&clone, &["config", "user.name", "mdstore tests"]);
    command(&clone, &["config", "user.email", "mdstore@example.invalid"]);
    command(&clone, &["config", "commit.gpgsign", "false"]);
    clone
}

#[test]
fn incoming_commits_are_validated_before_live_activation() {
    let (repository, remote) = sync_repository();
    let store = repository.store();
    assert_eq!(store.push().unwrap(), PushState::Pushed);
    let temporary = tempfile::tempdir().unwrap();
    let external = external_clone(&repository, remote.path(), temporary.path());
    fs::write(
        external.join("alice.md"),
        "# Missing required frontmatter\n",
    )
    .unwrap();
    command(&external, &["commit", "-am", "invalid incoming page"]);
    command(&external, &["push"]);
    let accepted = command(&repository.root, &["rev-parse", "HEAD"]);
    assert!(store.push().is_err());
    assert_eq!(command(&repository.root, &["rev-parse", "HEAD"]), accepted);
    assert_eq!(
        fs::read_to_string(repository.root.join("alice.md")).unwrap(),
        page("Alice")
    );
    assert!(
        store
            .get_page("alice.md", None)
            .unwrap()
            .content
            .contains("Alice profile.")
    );
    assert!(store.status().unwrap().replication.last_error.is_some());
    assert!(store.status().unwrap().blocked.is_none());
    fs::write(external.join("alice.md"), page("Alice")).unwrap();
    fs::write(external.join(".gitignore"), "").unwrap();
    command(
        &external,
        &["commit", "-am", "invalid incoming ignore rules"],
    );
    command(&external, &["push"]);
    assert!(store.push().is_err());
    assert_eq!(command(&repository.root, &["rev-parse", "HEAD"]), accepted);
    assert!(
        fs::read_to_string(repository.root.join(".gitignore"))
            .unwrap()
            .contains("*.mdstore")
    );
    fs::write(external.join(".gitignore"), "*.mdstore\n!.mdstore/\n").unwrap();
    fs::write(
        external.join("alice.md"),
        page("Alice").replace("Alice profile.", "Alice from upstream."),
    )
    .unwrap();
    command(
        &external,
        &["commit", "-am", "valid incoming page and ignores"],
    );
    command(&external, &["push"]);
    assert_eq!(store.push().unwrap(), PushState::Pushed);
    assert!(
        store
            .get_page("alice.md", None)
            .unwrap()
            .content
            .contains("Alice from upstream.")
    );
    assert_eq!(
        command(&repository.root, &["rev-parse", "HEAD"]),
        command(&external, &["rev-parse", "HEAD"])
    );
    let status = store.status().unwrap().replication;
    assert_eq!(status.pending_commits, 0);
    assert!(status.last_success.is_some());
    assert!(status.last_error.is_none());
    drop(store);
    assert_eq!(
        repository
            .store()
            .status()
            .unwrap()
            .replication
            .last_success,
        status.last_success
    );
}

#[test]
fn synchronization_divergence_blocks_writes_without_rewriting_history() {
    let (repository, remote) = sync_repository();
    let store = repository.store();
    store.push().unwrap();
    let temporary = tempfile::tempdir().unwrap();
    let external = external_clone(&repository, remote.path(), temporary.path());
    fs::write(
        external.join("alice.md"),
        page("Alice").replace("Alice profile.", "Remote edit."),
    )
    .unwrap();
    command(&external, &["commit", "-am", "remote edit"]);
    command(&external, &["push"]);
    let request = ApplyEditsRequest {
        edit_summary: "local edit".into(),
        edits: vec![EditOperation::Replace {
            path: "bob.md".into(),
            anchor: format!("6:{}", short_hash("Bob profile.")),
            content: "Local edit.".into(),
        }],
    };
    assert_eq!(store.apply_edits(&request).unwrap().push, PushState::Queued);
    let local = command(&repository.root, &["rev-parse", "HEAD"]);
    assert_eq!(store.push().unwrap(), PushState::Diverged);
    assert_eq!(command(&repository.root, &["rev-parse", "HEAD"]), local);
    assert!(
        store
            .apply_edits(&ApplyEditsRequest {
                edit_summary: "blocked edit".into(),
                edits: vec![EditOperation::CreatePage {
                    path: "new.md".into(),
                    content: page("New")
                }]
            })
            .is_err()
    );
    drop(store);
    assert!(repository.store().status().unwrap().blocked.is_some());
}

#[test]
fn destination_changes_invalidate_replication_progress() {
    let (repository, remote) = sync_repository();
    let store = repository.store();
    store.push().unwrap();
    assert_eq!(store.status().unwrap().replication.pending_commits, 0);
    let unavailable = remote.path().join("missing.git");
    command(
        &repository.root,
        &["remote", "set-url", "origin", unavailable.to_str().unwrap()],
    );
    assert!(store.status().unwrap().replication.pending_commits > 0);
    assert!(store.status().unwrap().replication.last_success.is_none());
    assert!(store.push().is_err());
    assert!(store.status().unwrap().unpushed);
    command(
        &repository.root,
        &[
            "remote",
            "set-url",
            "origin",
            remote.path().to_str().unwrap(),
        ],
    );
    store.push().unwrap();
    let branch = command(&repository.root, &["branch", "--show-current"]);
    command(
        &repository.root,
        &[
            "config",
            &format!("branch.{}.remote", branch.trim()),
            "origin",
        ],
    );
    command(
        &repository.root,
        &[
            "config",
            &format!("branch.{}.merge", branch.trim()),
            "refs/heads/replacement",
        ],
    );
    assert!(store.status().unwrap().replication.pending_commits > 0);
    assert!(store.status().unwrap().replication.last_success.is_none());
    store.push().unwrap();
    assert_eq!(store.status().unwrap().replication.pending_commits, 0);
}

#[test]
fn staged_listener_change_activates_on_restart() {
    let (repository, remote) = sync_repository();
    let store = repository.store();
    store.push().unwrap();
    let temporary = tempfile::tempdir().unwrap();
    let external = external_clone(&repository, remote.path(), temporary.path());
    fs::write(
        external.join("config.yaml"),
        config_yaml()
            .replace("127.0.0.1:3131", "127.0.0.1:3132")
            .replace("push: false", "push: true\n  remote: origin"),
    )
    .unwrap();
    command(&external, &["commit", "-am", "change listener"]);
    command(&external, &["push"]);
    let before = command(&repository.root, &["rev-parse", "HEAD"]);
    assert!(
        store
            .push()
            .unwrap_err()
            .to_string()
            .contains("restart required")
    );
    assert_eq!(command(&repository.root, &["rev-parse", "HEAD"]), before);
    drop(store);
    let restarted = repository.store();
    assert_eq!(restarted.config().server.listen, "127.0.0.1:3132");
    assert_eq!(
        command(&repository.root, &["rev-parse", "HEAD"]),
        command(&external, &["rev-parse", "HEAD"])
    );
    assert_eq!(restarted.push().unwrap(), PushState::Pushed);
}

#[test]
fn staged_restart_never_overwrites_a_later_local_edit() {
    let (repository, remote) = sync_repository();
    let store = repository.store();
    store.push().unwrap();
    let temporary = tempfile::tempdir().unwrap();
    let external = external_clone(&repository, remote.path(), temporary.path());
    let config = fs::read_to_string(external.join("config.yaml"))
        .unwrap()
        .replace("127.0.0.1:3131", "127.0.0.1:3132");
    fs::write(external.join("config.yaml"), config).unwrap();
    command(&external, &["commit", "-am", "stage listener"]);
    command(&external, &["push"]);
    assert!(store.push().is_err());
    store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "later local edit".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "Preserve this edit.".into(),
            }],
        })
        .unwrap();
    let local = command(&repository.root, &["rev-parse", "HEAD"]);
    drop(store);
    let restarted = repository.store();
    assert_eq!(command(&repository.root, &["rev-parse", "HEAD"]), local);
    assert_eq!(restarted.config().server.listen, "127.0.0.1:3131");
    assert!(
        restarted
            .get_page("alice.md", None)
            .unwrap()
            .content
            .contains("Preserve this edit.")
    );
    assert_eq!(restarted.push().unwrap(), PushState::Diverged);
}

#[test]
fn operational_config_rejects_document_rules() {
    for rule in [
        "schemas: []",
        "sections: []",
        "markdown: {}",
        "relations: []",
        "links: {}",
        "metadata: {}",
    ] {
        assert!(Config::from_yaml(&format!("{}\n{rule}\n", config_yaml())).is_err());
    }
    let repository = Repository::new();
    let store = repository.store();
    for path in [
        "config.yaml",
        "template.yaml",
        "people/template.yaml",
        ".mdstore/config.yaml",
        "schema.json",
    ] {
        assert!(
            store
                .apply_edits(&ApplyEditsRequest {
                    edit_summary: "change policy".into(),
                    edits: vec![EditOperation::CreatePage {
                        path: path.into(),
                        content: "{}".into()
                    }]
                })
                .unwrap_err()
                .to_string()
                .contains("read-only")
        );
    }
}

#[tokio::test]
async fn background_sync_retries_an_outage_without_another_edit() {
    let (repository, remote) = sync_repository();
    let offline = remote.path().join("unavailable.git");
    command(
        &repository.root,
        &["remote", "set-url", "origin", offline.to_str().unwrap()],
    );
    let store = repository.store();
    let response = store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "offline edit".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "Offline edit.".into(),
            }],
        })
        .unwrap();
    assert_eq!(response.push, PushState::Queued);
    let (_, server) = start_daemon(store.clone(), None).await;
    tokio::time::timeout(Duration::from_secs(10), async {
        while store.status().unwrap().replication.last_error.is_none() {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    assert!(store.status().unwrap().blocked.is_none());
    command(
        remote.path(),
        &["init", "--bare", offline.to_str().unwrap()],
    );
    tokio::time::timeout(Duration::from_secs(15), async {
        while store.status().unwrap().replication.last_success.is_none() {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(store.status().unwrap().replication.pending_commits, 0);
    assert!(store.status().unwrap().replication.last_error.is_none());
    server.abort();
}

#[cfg(unix)]
#[tokio::test]
async fn blocking_sidecar_reads_do_not_stall_the_async_scheduler() {
    let repository = Repository::new();
    let store = repository.store();
    let sidecar = repository.root.join("alice.mdstore");
    let output = std::process::Command::new("mkfifo")
        .arg(&sidecar)
        .output()
        .unwrap();
    assert!(output.status.success());

    let (opened_tx, opened_rx) = tokio::sync::oneshot::channel();
    let writer = tokio::task::spawn_blocking(move || {
        let mut writer = fs::OpenOptions::new().write(true).open(sidecar).unwrap();
        opened_tx.send(()).unwrap();
        std::thread::sleep(Duration::from_secs(1));
        writer.write_all(b"invalid sidecar").unwrap();
    });
    let reindex = {
        let store = store.clone();
        tokio::spawn(async move { store.reindex_missing().await })
    };
    let started = Instant::now();
    tokio::time::timeout(Duration::from_millis(500), opened_rx)
        .await
        .expect("sidecar read must run outside the async scheduler")
        .unwrap();

    let ping = serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "ping"});
    let (base_url, server) = start_daemon(store, None).await;
    let response = tokio::time::timeout(
        Duration::from_millis(250),
        reqwest::Client::new()
            .post(format!("{base_url}/mcp"))
            .json(&ping)
            .send(),
    )
    .await
    .expect("ping must remain responsive during sidecar I/O")
    .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert!(started.elapsed() < Duration::from_millis(750));

    writer.await.unwrap();
    reindex.await.unwrap().unwrap();
    server.abort();
}

#[cfg(unix)]
#[tokio::test]
async fn cancelling_a_reindex_waiter_does_not_release_the_job_lock() {
    let repository = Repository::new();
    let calls = Arc::new(AtomicUsize::new(0));
    let store = repository.store_with_provider(Arc::new(CountingProvider {
        calls: calls.clone(),
    }));
    let sidecar = repository.root.join("alice.mdstore");
    let output = Command::new("mkfifo").arg(&sidecar).output().unwrap();
    assert!(output.status.success());

    let (opened_tx, opened_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let fifo = sidecar.clone();
    let writer = tokio::task::spawn_blocking(move || {
        let mut writer = fs::OpenOptions::new().write(true).open(fifo).unwrap();
        opened_tx.send(()).unwrap();
        release_rx.recv().unwrap();
        writer.write_all(b"invalid sidecar").unwrap();
    });
    let first = {
        let store = store.clone();
        tokio::spawn(async move { store.reindex_missing().await })
    };
    tokio::time::timeout(Duration::from_secs(5), opened_rx)
        .await
        .unwrap()
        .unwrap();
    first.abort();
    assert!(first.await.unwrap_err().is_cancelled());
    fs::remove_file(&sidecar).unwrap();
    store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "change Alice during reindex".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "Alice changed during reindex.".into(),
            }],
        })
        .unwrap();

    let second = {
        let store = store.clone();
        tokio::spawn(async move { store.reindex_missing().await })
    };
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(calls.load(Ordering::SeqCst), 0);

    release_tx.send(()).unwrap();
    writer.await.unwrap();
    tokio::time::timeout(Duration::from_secs(5), second)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(calls.load(Ordering::SeqCst) > 0);
    assert!(store.status().unwrap().vectors_ready > 0);
}

#[tokio::test(flavor = "current_thread")]
async fn detached_reindex_failure_is_logged() {
    let output = Arc::new(Mutex::new(Vec::new()));
    let writer = output.clone();
    let completed = Arc::new(tokio::sync::Notify::new());
    let writer_completed = completed.clone();
    let subscriber = tracing_subscriber::fmt()
        .without_time()
        .with_ansi(false)
        .with_writer(move || LogWriter {
            output: writer.clone(),
            buffer: Vec::new(),
            completed: writer_completed.clone(),
        })
        .finish();
    tracing::subscriber::set_global_default(subscriber).unwrap();
    let repository = Repository::new();
    let started = Arc::new(tokio::sync::Notify::new());
    let release = Arc::new(tokio::sync::Notify::new());
    let store = repository.store_with_provider(Arc::new(BlockingFailProvider {
        started: started.clone(),
        release: release.clone(),
    }));
    let waiter = tokio::spawn(async move { store.reindex_missing().await });
    started.notified().await;
    waiter.abort();
    assert!(waiter.await.unwrap_err().is_cancelled());
    release.notify_one();

    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            completed.notified().await;
            let logged = String::from_utf8(output.lock().unwrap().clone()).unwrap();
            if logged.contains("reindex failed") && logged.contains("expected provider failure") {
                break;
            }
        }
    })
    .await
    .expect("detached failure must be logged");
}

#[tokio::test]
async fn embedding_worker_recovers_without_another_edit() {
    let repository = Repository::new();
    let provider = Arc::new(RecoveringProvider {
        failures: AtomicUsize::new(1),
        calls: AtomicUsize::new(0),
    });
    let store = repository.store_with_provider(provider.clone());
    let (_, server) = start_daemon(store.clone(), None).await;
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let status = store.status().unwrap();
            if status.vectors_ready == status.vectors_total && status.vectors_total > 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("embedding worker must recover without an edit or explicit reindex");
    assert_eq!(provider.calls.load(Ordering::SeqCst), 3);
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(
        provider.calls.load(Ordering::SeqCst),
        3,
        "successful indexing returns to idle"
    );
    server.abort();
}

#[tokio::test]
async fn new_edits_interrupt_embedding_backoff() {
    let repository = Repository::new();
    let provider = Arc::new(RecoveringProvider {
        failures: AtomicUsize::new(3),
        calls: AtomicUsize::new(0),
    });
    let store = repository.store_with_provider(provider.clone());
    let (_, server) = start_daemon(store.clone(), None).await;
    tokio::time::timeout(Duration::from_secs(5), async {
        while provider.calls.load(Ordering::SeqCst) < 3 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "wake embedding retry".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash("Alice profile.")),
                content: "Alice updated during backoff.".into(),
            }],
        })
        .unwrap();
    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            let status = store.status().unwrap();
            if status.vectors_ready == status.vectors_total && status.vectors_total > 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("new edit must interrupt the 2–4 second retry delay");
    server.abort();
}

#[tokio::test]
async fn daemon_embedding_worker_coalesces_edits_and_observes_direct_store_writes() {
    let repository = Repository::new();
    let calls = Arc::new(AtomicUsize::new(0));
    let store = repository.store_with_provider(Arc::new(CountingProvider {
        calls: calls.clone(),
    }));
    let mut content = "Alice profile.".to_owned();
    for index in 0..12 {
        let next = format!("Alice revision {index}.");
        store
            .apply_edits(&ApplyEditsRequest {
                edit_summary: format!("revision {index}"),
                edits: vec![EditOperation::Replace {
                    path: "alice.md".into(),
                    anchor: format!("6:{}", short_hash(&content)),
                    content: next.clone(),
                }],
            })
            .unwrap();
        content = next;
    }
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    let (_, server) = start_daemon(store.clone(), None).await;
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let status = store.status().unwrap();
            if status.vectors_ready == status.vectors_total && status.vectors_total > 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        calls.load(Ordering::SeqCst),
        2,
        "only the latest two pages should be embedded"
    );

    store
        .apply_edits(&ApplyEditsRequest {
            edit_summary: "direct store edit while serving".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                anchor: format!("6:{}", short_hash(&content)),
                content: "Alice's final revision.".into(),
            }],
        })
        .unwrap();
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let status = store.status().unwrap();
            if status.vectors_ready == status.vectors_total && status.vectors_total > 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(
        calls.load(Ordering::SeqCst),
        3,
        "unchanged page must keep its vector"
    );
    server.abort();
}
