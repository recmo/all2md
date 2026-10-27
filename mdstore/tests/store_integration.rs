//! End-to-end repository, daemon, MCP, and sidecar behavior tests.

use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::Arc,
};

use anyhow::Result;
use async_trait::async_trait;
use mdstore::{
    ApplyEditsRequest, EditOperation, InputType, RerankResult, RetrievalProvider, Store,
    serve_listener, short_hash,
};
use reqwest::StatusCode;

fn complete_anchor(text: &str) -> String {
    let lines: Vec<_> = text.lines().collect();
    if lines.is_empty() {
        return "0:".into();
    }
    let anchor = |i: usize| {
        format!(
            "{}:{}",
            i + 1,
            short_hash(
                lines[i],
                i.checked_sub(1).map_or("", |j| lines[j]),
                lines.get(i + 1).copied().unwrap_or("")
            )
        )
    };
    if lines.len() == 1 {
        anchor(0)
    } else {
        format!("{}..{}", anchor(0), anchor(lines.len() - 1))
    }
}

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
        fs::write(root.join("schema.md"), profile_template()).unwrap();
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

async fn start_daemon(store: Arc<Store>) -> (String, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        serve_listener(listener, store).await.unwrap();
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

fn profile_template() -> &'static str {
    r#"```starlark
frontmatter(name=string(required=True))
metadata(display_name="/name")
links(wiki=[r"\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]"])
relation("mentions", selector={"kind": "markdown_links"}, reciprocal="mentions")
```
"#
}

fn page(name: &str) -> String {
    format!("---\nname: {name}\n---\n# Notes\n\n{name} profile.\n")
}

#[test]
fn exact_page_move_commits_atomically() {
    let repository = Repository::new();
    let store = repository.store();
    let base = fs::read_to_string(repository.root.join("alice.md")).unwrap();
    let request = ApplyEditsRequest {
        summary: "move Alice".into(),
        edits: vec![
            EditOperation::Replace {
                path: "alice.md".into(),
                anchor: Some(complete_anchor(&(base.clone()))),
                content: String::new(),
            },
            EditOperation::Replace {
                path: "archive/alice.md".into(),
                content: base.clone(),
                anchor: None,
            },
        ],
    };
    store.apply_edits(&request).unwrap();
    assert!(!repository.root.join("alice.md").exists());
    assert_eq!(
        fs::read_to_string(repository.root.join("archive/alice.md")).unwrap(),
        base
    );
    assert_eq!(
        command(&repository.root, &["rev-list", "--count", "HEAD"]).trim(),
        "2"
    );
    store.validate().unwrap();
}

#[test]
fn reciprocal_links_are_caller_authored_and_atomic() {
    let repository = Repository::new();
    let store = repository.store();
    let notes = format!("4:{}", short_hash("# Notes", "---", ""));
    let one_sided = ApplyEditsRequest {
        summary: "add one-sided link".into(),
        edits: vec![EditOperation::Append {
            path: "alice.md".into(),
            anchor: Some(notes.clone()),
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
        summary: "link Alice and Bob".into(),
        edits: vec![
            EditOperation::Append {
                path: "alice.md".into(),
                anchor: Some(notes.clone()),
                content: "\n[Bob](bob.md)".into(),
            },
            EditOperation::Append {
                path: "bob.md".into(),
                anchor: Some(notes),
                content: "\n[Alice](alice.md)".into(),
            },
        ],
    };
    let response = store.apply_edits(&request).unwrap();
    assert_eq!(response, ["alice.md", "bob.md"]);
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
    assert_eq!(replay, response);
}

#[test]
fn invalid_templates_block_startup_even_without_matching_pages() {
    let repository = Repository::new();
    fs::create_dir(repository.root.join("empty")).unwrap();
    fs::write(
        repository.root.join("empty/schema.md"),
        r#"```starlark
structure(additional_sections=False)
section("A", words={"minimum": 5, "maximum": 2}, level=1)
```
"#,
    )
    .unwrap();
    command(&repository.root, &["add", "empty/schema.md"]);
    command(&repository.root, &["commit", "-qm", "invalid template"]);
    assert!(Store::open_with_provider(&repository.root, Arc::new(FakeProvider)).is_err());
}

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

#[tokio::test]
async fn rebuilds_adjacent_sidecars_and_searches() {
    let repository = Repository::new();
    let store = repository.store();
    store.reindex().await.unwrap();
    assert!(repository.root.join("alice.mdstore").is_file());
    assert!(repository.root.join("bob.mdstore").is_file());
    let status = store.status().unwrap();
    assert_eq!(status.vectors_ready, status.vectors_total);
    let (url, server) = start_daemon(store.clone()).await;
    let response: serde_json::Value = reqwest::Client::new()
        .post(format!("{url}/mcp"))
        .json(
            &serde_json::json!({"jsonrpc":"2.0", "id":1, "method":"tools/call",
            "params":{"name":"search", "arguments":{"query":"Alice"}}}),
        )
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(response["result"]["isError"], false, "{response}");
    assert_eq!(response["result"]["content"], serde_json::json!([]));
    let response = &response["result"]["structuredContent"];
    assert_eq!(response.as_object().unwrap().len(), 2);
    let result = &response["results"][0];
    assert_eq!(result.as_object().unwrap().len(), 2);
    let path = result["path"].as_str().unwrap();
    assert!(path.starts_with("/alice.md:"), "{path}");
    let range = path.split_once(':').unwrap().1;
    let (start, end) = range.split_once('-').unwrap();
    let expected = store
        .get_page(
            "alice.md",
            Some((start.parse().unwrap(), end.parse().unwrap())),
        )
        .unwrap();
    assert_eq!(result["excerpt"], expected.content.unwrap());
    server.abort();
}

#[tokio::test]
async fn mcp_lists_only_three_tools() {
    let repository = Repository::new();
    let store = repository.store();
    let (base_url, server) = start_daemon(store.clone()).await;
    let client = reqwest::Client::new();
    let body = serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"});
    let response = client
        .post(format!("{base_url}/mcp"))
        .json(&body)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = response.bytes().await.unwrap();
    let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    let names: Vec<&str> = value["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["search", "read", "edit"]);
    let get_page = value["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .find(|tool| tool["name"] == "read")
        .unwrap();
    assert!(
        get_page["inputSchema"]["properties"]
            .get("include_template")
            .is_none()
    );
    assert_eq!(
        get_page["inputSchema"]["properties"]
            .as_object()
            .unwrap()
            .len(),
        1
    );
    assert!(
        get_page["inputSchema"]["properties"]
            .get("start_line")
            .is_none()
    );
    assert!(
        get_page["inputSchema"]["properties"]
            .get("end_line")
            .is_none()
    );
    let apply = value["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .find(|tool| tool["name"] == "edit")
        .unwrap();
    let variants =
        apply["inputSchema"]["properties"]["edits"]["additionalProperties"]["items"]["oneOf"]
            .as_array()
            .unwrap();
    assert_eq!(variants.len(), 2);
    let append = variants
        .iter()
        .find(|schema| schema["properties"]["op"]["const"] == "append")
        .unwrap();
    assert_eq!(append["required"], serde_json::json!(["op", "content"]));
    let replace = variants
        .iter()
        .find(|schema| schema["properties"]["op"]["const"] == "replace")
        .unwrap();
    assert_eq!(replace["required"], serde_json::json!(["op", "content"]));

    let health = client
        .get(format!("{base_url}/health"))
        .send()
        .await
        .unwrap();
    assert_eq!(health.status(), StatusCode::OK);
    server.abort();
}

#[tokio::test]
async fn mcp_validates_protocol_and_json_rpc_envelopes() {
    let repository = Repository::new();
    let (base_url, server) = start_daemon(repository.store()).await;
    let client = reqwest::Client::new();

    let unsupported = client
        .post(format!("{base_url}/mcp"))
        .header("mcp-protocol-version", "2099-01-01")
        .json(&serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        .send()
        .await
        .unwrap();
    assert_eq!(unsupported.status(), StatusCode::BAD_REQUEST);

    let legacy = client
        .post(format!("{base_url}/mcp"))
        .header("mcp-protocol-version", "2025-03-26")
        .json(&serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        .send()
        .await
        .unwrap();
    assert_eq!(legacy.status(), StatusCode::BAD_REQUEST);

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

    for requested in ["2025-03-26", "2025-06-18", "2099-01-01"] {
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
        assert_eq!(value["result"]["protocolVersion"], "2025-06-18");
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
async fn mcp_page_content_is_single_and_schema_is_a_path() {
    let repository = Repository::new();
    let source = page("Alice").replace('\n', "\r\n");
    fs::write(repository.root.join("alice.md"), &source).unwrap();
    command(&repository.root, &["add", "."]);
    command(&repository.root, &["commit", "-qm", "CRLF source"]);
    let (url, server) = start_daemon(repository.store()).await;
    let client = reqwest::Client::new();
    for (arguments, error) in [
        (serde_json::json!({"path": "/alice.md"}), false),
        (serde_json::json!({"path": "/alice.md:4-6"}), false),
        (serde_json::json!({"path": "/alice.md:5"}), false),
        (serde_json::json!({"path": "new.md"}), false),
        (
            serde_json::json!({"path": "/alice.md", "format": "raw"}),
            true,
        ),
        (
            serde_json::json!({"path": "/alice.md", "format": "hashlines"}),
            true,
        ),
        (
            serde_json::json!({"path": "/alice.md", "start_line": 1}),
            true,
        ),
        (
            serde_json::json!({"path": "/alice.md", "end_line": 1}),
            true,
        ),
        (
            serde_json::json!({"path": "/alice.md", "include_template": true}),
            true,
        ),
        (serde_json::json!({"path": "/alice.md:0"}), true),
        (serde_json::json!({"path": "/alice.md:6-4"}), true),
        (serde_json::json!({"path": "/alice.md:4-"}), true),
        (serde_json::json!({"path": "/alice.md:-1"}), true),
        (
            serde_json::json!({"path": "/alice.md:999999999999999999999999999999"}),
            true,
        ),
        (serde_json::json!({"path": "/:1"}), true),
    ] {
        let response: serde_json::Value = client
            .post(format!("{url}/mcp"))
            .json(
                &serde_json::json!({"jsonrpc":"2.0", "id":1, "method":"tools/call",
                "params":{"name":"read", "arguments":arguments}}),
            )
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        let result = &response["result"];
        assert_eq!(result["isError"], error, "{response}");
        assert!(result.get("structuredContent").is_none());
        if error {
            continue;
        }
        let blocks = result["content"].as_array().unwrap();
        assert_eq!(blocks.len(), 1);
        let text = blocks[0]["text"].as_str().unwrap();
        let requested = arguments["path"].as_str().unwrap();
        let path = requested.split(':').next().unwrap().trim_start_matches('/');
        assert!(text.starts_with(&format!("Path: /{path}\nSchema: /schema.md\n\n")));
        if path == "new.md" {
            assert!(text.ends_with("File does not exist."));
        } else {
            let lines: Vec<_> = text.split_once("\n\n").unwrap().1.lines().collect();
            if requested.ends_with(":5") {
                assert_eq!(lines.len(), 1);
                assert!(lines[0].starts_with("5:"));
            }
            if requested.ends_with(":4-6") {
                assert_eq!(lines.len(), 3);
                assert!(lines[0].starts_with("4:"));
            }
            for line in lines {
                assert_eq!(
                    line.split_once('|')
                        .unwrap()
                        .0
                        .split_once(':')
                        .unwrap()
                        .1
                        .len(),
                    4
                );
            }
        }
    }
    for path in ["alice.md", "new.md"] {
        let response = client
            .get(format!("{url}/path/{path}"))
            .send()
            .await
            .unwrap();
        assert_eq!(
            response.headers()["link"],
            "</path/schema.md>; rel=\"describedby\""
        );
        assert_eq!(response.headers()["cache-control"], "no-store");
        assert_eq!(
            response.headers()["content-type"],
            "text/plain; charset=utf-8"
        );
        if path == "alice.md" {
            assert_eq!(response.status(), StatusCode::OK);
            assert_eq!(response.text().await.unwrap(), source);
        } else {
            assert_eq!(response.status(), StatusCode::NOT_FOUND);
            assert_eq!(response.text().await.unwrap(), "");
        }
    }
    server.abort();
}

#[tokio::test]
async fn mcp_batch_page_reads_preserve_order_and_do_not_duplicate_content() {
    async fn get(
        client: &reqwest::Client,
        url: &str,
        arguments: serde_json::Value,
    ) -> serde_json::Value {
        let response: serde_json::Value = client
            .post(format!("{url}/mcp"))
            .json(
                &serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "read", "arguments": arguments}}),
            )
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        response["result"].clone()
    }
    let repository = Repository::new();
    let (url, server) = start_daemon(repository.store()).await;
    let client = reqwest::Client::new();

    let result = get(
        &client,
        &url,
        serde_json::json!({"path": ["/bob.md:6", "/alice.md:4-6", "/new.md", "/bob.md:4"]}),
    )
    .await;
    assert_eq!(result["isError"], false, "{result}");
    assert!(result.get("structuredContent").is_none());
    let blocks = result["content"].as_array().unwrap();
    assert_eq!(blocks.len(), 4);
    for (block, path) in blocks
        .iter()
        .zip(["bob.md", "alice.md", "new.md", "bob.md"])
    {
        assert_eq!(block["type"], "text");
        assert!(
            block["text"]
                .as_str()
                .unwrap()
                .starts_with(&format!("Path: /{path}\n"))
        );
    }
    assert!(blocks[0]["text"].as_str().unwrap().contains("6:"));
    assert!(
        blocks[0]["text"]
            .as_str()
            .unwrap()
            .ends_with("|Bob profile.")
    );
    assert_eq!(
        blocks[1]["text"]
            .as_str()
            .unwrap()
            .split_once("\n\n")
            .unwrap()
            .1
            .lines()
            .count(),
        3
    );
    assert!(
        blocks[2]["text"]
            .as_str()
            .unwrap()
            .ends_with("File does not exist.")
    );
    assert!(blocks[3]["text"].as_str().unwrap().ends_with("|# Notes"));

    let result = get(&client, &url, serde_json::json!({"path": ["/alice.md"]})).await;
    assert_eq!(result["isError"], false, "{result}");
    assert_eq!(result["content"].as_array().unwrap().len(), 1);
    assert!(result.get("structuredContent").is_none());

    let result = get(
        &client,
        &url,
        serde_json::json!({"path": ["/bob.md:6", "/", "/alice.md:4"]}),
    )
    .await;
    assert_eq!(result["isError"], false, "{result}");
    assert!(result.get("structuredContent").is_none());
    let blocks = result["content"].as_array().unwrap();
    assert_eq!(blocks.len(), 3);
    assert!(
        blocks[0]["text"]
            .as_str()
            .unwrap()
            .ends_with("|Bob profile.")
    );
    assert_eq!(
        blocks[1]["text"],
        "/alice.md\n/bob.md\n/config.yaml\n/schema.md"
    );
    assert!(blocks[2]["text"].as_str().unwrap().ends_with("|# Notes"));

    for arguments in [
        serde_json::json!({"path": []}),
        serde_json::json!({"path": ["/alice.md", 1]}),
        serde_json::json!({"path": [""]}),
        serde_json::json!({"path": ["/alice.md", "/bob.md:0"]}),
        serde_json::json!({"path": ["/alice.md", "/bob.md:6"], "format": "raw"}),
        serde_json::json!({"path": ["/alice.md", "/"], "format": "raw"}),
    ] {
        let result = get(&client, &url, arguments).await;
        assert_eq!(result["isError"], true, "{result}");
        assert!(result.get("structuredContent").is_none());
    }
    server.abort();
}

#[tokio::test]
async fn document_batches_validate_and_submit_through_mcp() {
    let repository = Repository::new();
    let store = repository.store();
    let (base_url, server) = start_daemon(store.clone()).await;
    let client = reqwest::Client::new();
    let initial_head = command(&repository.root, &["rev-parse", "HEAD"]);
    let original = store.get_page("alice.md", None).unwrap().text.unwrap();
    assert_eq!(original, page("Alice"));
    assert!(
        store
            .get_page("alice.md", Some((1, 2)))
            .unwrap()
            .text
            .is_none()
    );
    let changed = original.replace("Alice profile.", "Updated Alice profile.");
    let rendered = store.get_page("alice.md", None).unwrap().content.unwrap();
    let anchors: Vec<_> = rendered
        .lines()
        .map(|line| line.split('|').next().unwrap())
        .collect();
    let anchor = format!("{}..{}", anchors.first().unwrap(), anchors.last().unwrap());
    let batch = serde_json::json!({"summary": "Web edit", "edits": {"/alice.md": [{"op": "replace", "anchor": anchor, "content": changed}]}});
    let mut without_summary = batch.clone();
    without_summary["summary"] = serde_json::json!("");
    assert_eq!(
        command(&repository.root, &["rev-parse", "HEAD"]),
        initial_head
    );
    assert_eq!(
        fs::read_to_string(repository.root.join("alice.md")).unwrap(),
        original
    );
    let missing_summary_result: serde_json::Value = client
        .post(format!("{base_url}/mcp"))
        .json(
            &serde_json::json!({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
            "params": {"name": "edit", "arguments": without_summary}}),
        )
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(missing_summary_result["result"]["isError"], true);
    assert_eq!(
        command(&repository.root, &["rev-parse", "HEAD"]),
        initial_head
    );
    let invalid_request = ApplyEditsRequest {
        summary: "Invalid".into(),
        edits: vec![EditOperation::Replace {
            path: "alice.md".into(),
            anchor: Some(complete_anchor(&original)),
            content: "# Missing required name\n".into(),
        }],
    };
    let rejected = store.apply_edits(&invalid_request).unwrap_err();
    assert!(
        rejected
            .downcast_ref::<mdstore::ValidationError>()
            .is_some()
    );
    let call = serde_json::json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "edit", "arguments": batch}});
    for _ in 0..2 {
        let result: serde_json::Value = client
            .post(format!("{base_url}/mcp"))
            .header("origin", &base_url)
            .json(&call)
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(result["result"]["isError"], false, "{result}");
        assert_eq!(
            result["result"],
            serde_json::json!({
                "content": [], "structuredContent": {"success": true}, "isError": false
            })
        );
    }
    assert_eq!(
        fs::read_to_string(repository.root.join("alice.md")).unwrap(),
        changed
    );
    assert_eq!(
        command(&repository.root, &["rev-list", "--count", "HEAD"]).trim(),
        "2"
    );
    let mut stale = batch;
    stale["summary"] = serde_json::json!("Stale overwrite");
    let stale_call = serde_json::json!({"jsonrpc":"2.0", "id":2, "method":"tools/call", "params":{"name":"edit", "arguments":stale}});
    let rejected: serde_json::Value = client
        .post(format!("{base_url}/mcp"))
        .json(&stale_call)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(rejected["result"]["isError"], true);
    assert_eq!(
        command(&repository.root, &["rev-list", "--count", "HEAD"]).trim(),
        "2"
    );
    server.abort();
}

#[test]
fn whole_page_edits_preserve_exact_text_and_remain_atomic() {
    let repository = Repository::new();
    let store = repository.store();
    let original = page("Alice");
    let changed = original.trim_end().replace('\n', "\r\n");
    store
        .apply_edits(&ApplyEditsRequest {
            summary: "Exact text".into(),
            edits: vec![EditOperation::Replace {
                path: "alice.md".into(),
                content: changed.clone(),
                anchor: Some(complete_anchor(&(original))),
            }],
        })
        .unwrap();
    assert_eq!(
        store.get_page("alice.md", None).unwrap().text.unwrap(),
        changed
    );
    let one_sided = format!("{changed}\r\n[Bob](bob.md)\r\n");
    let request = ApplyEditsRequest {
        summary: "Reciprocal batch".into(),
        edits: vec![EditOperation::Replace {
            path: "alice.md".into(),
            content: one_sided,
            anchor: Some(complete_anchor(&(changed.clone()))),
        }],
    };
    assert!(store.apply_edits(&request).is_err());
    assert_eq!(
        store.get_page("alice.md", None).unwrap().text.unwrap(),
        changed
    );
    let mut batch = request;
    batch.edits.push(EditOperation::Replace {
        path: "bob.md".into(),
        content: format!("{}\n[Alice](alice.md)\n", page("Bob")),
        anchor: Some(complete_anchor(&(page("Bob")))),
    });
    store.apply_edits(&batch).unwrap();
}

#[tokio::test]
async fn mcp_directory_reads_return_only_plain_paths_and_replace_auxiliary_routes() {
    let repository = Repository::new();
    fs::create_dir_all(repository.root.join("notes/nested")).unwrap();
    fs::write(repository.root.join("notes/nested/carol.md"), page("Carol")).unwrap();
    command(&repository.root, &["add", "."]);
    command(&repository.root, &["commit", "-qm", "Nested page"]);
    let store = repository.store();
    let (url, server) = start_daemon(store.clone()).await;
    let client = reqwest::Client::new();
    for path in ["/", "/notes/", "notes/nested/"] {
        let response: serde_json::Value = client.post(format!("{url}/mcp"))
            .json(&serde_json::json!({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"read", "arguments":{"path":path}}}))
            .send().await.unwrap().json().await.unwrap();
        assert_eq!(response["result"]["isError"], false, "{response}");
        assert!(response["result"].get("structuredContent").is_none());
        assert_eq!(response["result"]["content"].as_array().unwrap().len(), 1);
        let expected = match path {
            "/" => "/alice.md\n/bob.md\n/config.yaml\n/notes/\n/schema.md",
            "/notes/" => "/notes/nested/",
            _ => "/notes/nested/carol.md",
        };
        assert_eq!(response["result"]["content"][0]["text"], expected);
        let raw = client
            .get(format!("{url}/path/{}", path.trim_start_matches('/')))
            .send()
            .await
            .unwrap();
        assert_eq!(raw.status(), StatusCode::OK);
        assert_eq!(raw.text().await.unwrap(), expected);
    }
    for path in ["../", "/notes/", "missing/"] {
        assert!(store.get_directory(path).is_err());
    }
    for path in ["/documents", "/validation-snapshot", "/validate", "/cli"] {
        assert_eq!(
            client
                .post(format!("{url}{path}"))
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            client
                .get(format!("{url}{path}"))
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::NOT_FOUND
        );
    }
    server.abort();
}
