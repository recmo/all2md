use std::{net::SocketAddr, sync::Arc};

use anyhow::{Context, Result, bail};
use axum::{
    Json, Router,
    body::Bytes,
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde::Deserialize;
use serde_json::{Value, json};
use tower_http::trace::TraceLayer;

use crate::{ApplyEditsRequest, Store, store::ValidationError};

/// Preferred MCP protocol revision.
pub const MCP_PROTOCOL_VERSION: &str = "2025-06-18";

fn router(store: Arc<Store>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/mcp", post(mcp))
        .layer(TraceLayer::new_for_http())
        .with_state(store)
}

/// Serves the daemon until shutdown on the configured socket.
pub async fn serve(store: Arc<Store>, listen: SocketAddr) -> Result<()> {
    let listener = tokio::net::TcpListener::bind(listen)
        .await
        .with_context(|| format!("bind {listen}"))?;
    serve_listener(listener, store).await
}

/// Serves the daemon on an already-bound socket.
pub async fn serve_listener(
    listener: tokio::net::TcpListener,
    store: Arc<Store>,
) -> Result<()> {
    let listen = listener.local_addr().context("inspect listening socket")?;
    tracing::info!(%listen, "mdstore listening");
    let sync = Arc::clone(&store).synchronize();
    let embeddings = Arc::clone(&store).maintain_embeddings();
    let server = axum::serve(listener, router(store))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .into_future();
    tokio::select! {
        result = server => result?,
        _ = sync => bail!("synchronization worker stopped"),
        _ = embeddings => bail!("embedding worker stopped"),
    }
    Ok(())
}

async fn health(State(store): State<Arc<Store>>) -> Response {
    match run_blocking(move || store.status()).await {
        Ok(status) => (
            StatusCode::OK,
            Json(json!({"status": "ok", "store": status})),
        )
            .into_response(),
        Err(error) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"status": "error", "error": error.to_string()})),
        )
            .into_response(),
    }
}

async fn run_blocking<T>(
    operation: impl FnOnce() -> Result<T> + Send + 'static,
) -> Result<T>
where
    T: Send + 'static,
{
    tokio::task::spawn_blocking(operation)
        .await
        .context("blocking daemon operation failed")?
}

async fn mcp(State(store): State<Arc<Store>>, headers: HeaderMap, body: Bytes) -> Response {
    if let Some(version) = headers.get("mcp-protocol-version") {
        let Ok(version) = version.to_str() else {
            return StatusCode::BAD_REQUEST.into_response();
        };
        if version != MCP_PROTOCOL_VERSION {
            return StatusCode::BAD_REQUEST.into_response();
        }
    }
    let request: Value = match serde_json::from_slice(&body) {
        Ok(request) => request,
        Err(_) => return rpc_error(Value::Null, -32700, "parse error"),
    };
    let Some(object) = request.as_object() else {
        return rpc_error(Value::Null, -32600, "invalid request");
    };
    let valid_id = object
        .get("id")
        .is_none_or(|id| id.is_string() || id.as_i64().is_some() || id.as_u64().is_some());
    let Some(method) = object.get("method").and_then(Value::as_str) else {
        return rpc_error(Value::Null, -32600, "invalid request");
    };
    if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
        || !valid_id
        || object
            .get("params")
            .is_some_and(|params| !params.is_object() && !params.is_array())
    {
        return rpc_error(Value::Null, -32600, "invalid request");
    }
    let notification = !object.contains_key("id");
    let id = object.get("id").cloned().unwrap_or(Value::Null);
    let params = object.get("params").cloned().unwrap_or_else(|| json!({}));
    if notification {
        return StatusCode::ACCEPTED.into_response();
    }
    match method {
        "initialize" => initialize(id, &params),
        "ping" => rpc_result(id, json!({})),
        "tools/list" => rpc_result(id, json!({"tools": tools()})),
        "tools/call" => call_tool(id, &store, params).await,
        _ => rpc_error(id, -32601, "method not found"),
    }
}

fn initialize(id: Value, params: &Value) -> Response {
    let Some(_) = params.get("protocolVersion").and_then(Value::as_str) else {
        return rpc_error(id, -32602, "initialize requires protocolVersion");
    };
    rpc_result(
        id,
        json!({
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": false}},
            "serverInfo": {"name": "mdstore", "version": env!("CARGO_PKG_VERSION")}
        }),
    )
}

#[must_use]
/// Returns the exact public MCP tool allowlist.
pub fn tool_names() -> [&'static str; 3] {
    ["search", "read", "edit"]
}

fn tools() -> Vec<Value> {
    vec![
        json!({
            "name": "search",
            "description": "Hybrid exact, embedding, graph-assisted, and reranked search. Optional variants supply alternative query formulations. Returns ranked results containing only a repository-rooted /path:start-end and a hashline-anchored excerpt, plus degraded retrieval warnings. Excerpt anchors can be used directly with edit.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "variants": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["query"],
                "additionalProperties": false
            }
        }),
        json!({
            "name": "read",
            "description": "Read a file or directory from the published document tree. Use / for the root or a path ending in / for a directory: returns a plain-text list of direct child paths, with trailing / for directories. Append ** for all descendants or **/ for descendant directories only (for example /notes/** and /notes/**/). Subtree listings exclude the starting directory and are complete, sorted and deduplicated. File reads return only path, template and content; returned paths start with / at the repository root. Reads and edits also accept paths without that prefix. Default file reads return a plain-text path/schema header followed by four-character neighbor-based hashlines, without a structured duplicate. Append :LINE or :START-END to the path for a line window. Pass a non-empty path array for ordered batch reads with a shared format. Hashline batches return one text block per path; raw file batches return structured results under results. An invalid read rejects the whole batch. Anchors must match at the supplied line number; stale anchors require a fresh read. Use format=raw for structured results containing exact source text (including line endings) for whole-page replacements. Directories support plain-text output only. Markdown responses identify the nearest schema by its repository-rooted template path with a leading /; read that path separately for instructions and rules. A proposed Markdown path returns content=null and its template for discovery before creation. Configuration edits require server.allow_config_edits; template edits require server.allow_template_edits.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "description": "Repository-rooted path or non-empty array of paths. Each may end in :LINE or :START-END (one-based, inclusive). Use a trailing / for direct children, /** for all descendants, or /**/ for descendant directories only.",
                        "oneOf": [
                            {"type": "string", "minLength": 1},
                            {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
                        ]
                    },
                    "format": {"type": "string", "enum": ["hashlines", "raw"], "default": "hashlines"}
                },
                "required": ["path"],
                "additionalProperties": false
            }
        }),
        json!({
            "name": "edit",
            "description": "Atomically validate and locally commit an edit batch. Supply edits as an object mapping each file path to a non-empty operation array; operations omit path. All anchors refer to the pre-edit snapshot. Use replace with an anchor for a line or inclusive range, with the complete anchor range for a whole existing file, or without an anchor to create a missing file. Use 0: to replace an existing empty file with non-empty content. Empty-to-empty replacement is an error. Empty replacement content deletes the selected lines; deleting the file requires a single complete-file anchor. Use append to insert after an anchor, 0: for file start, or omit anchor for EOF; the file must exist. Config edits require server.allow_config_edits; listener changes require a restart. Template edits require server.allow_template_edits and validate the complete corpus against the proposed templates. Create paths may contain {serial:03} for template-governed allocation; read the parent directory afterward to discover allocated paths. Git replication runs in the background. Returns {success: true} or {errors: [...]}. Error location and optional schema use /path:line syntax, omitting :line when unavailable and omitting absent schema.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "minLength": 1},
                    "edits": {
                        "type": "object",
                        "minProperties": 1,
                        "propertyNames": {"minLength": 1},
                        "additionalProperties": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"oneOf": edit_operation_schemas()}
                        }
                    }
                },
                "required": ["summary", "edits"],
                "additionalProperties": false
            }
        }),
    ]
}

fn edit_operation_schemas() -> Vec<Value> {
    vec![
        json!({
            "type": "object",
            "properties": {
                "op": {"const": "replace"},
                "anchor": {"type": "string", "description": "LINE:HASH or START:HASH..END:HASH. Use the complete range for whole-file replacement/deletion, 0: for an existing empty file, or omit to create a missing file."},
                "content": {"type": "string", "description": "Replacement text; empty deletes the selected lines or whole file."}
            },
            "required": ["op", "content"],
            "additionalProperties": false
        }),
        json!({
            "type": "object",
            "properties": {
                "op": {"const": "append"},
                "anchor": {"type": "string", "description": "LINE:HASH to insert after; 0: for file start; omit for EOF. File must exist."},
                "content": {"type": "string", "minLength": 1}
            },
            "required": ["op", "content"],
            "additionalProperties": false
        }),
    ]
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum ApplyOperation {
    Replace {
        anchor: Option<String>,
        content: String,
    },
    Append {
        anchor: Option<String>,
        content: String,
    },
}

impl ApplyOperation {
    fn into_edit(self, path: String) -> crate::EditOperation {
        match self {
            Self::Replace { anchor, content } => crate::EditOperation::Replace {
                path,
                anchor,
                content,
            },
            Self::Append { anchor, content } => crate::EditOperation::Append {
                path,
                anchor,
                content,
            },
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ApplyArguments {
    summary: String,
    edits: std::collections::BTreeMap<String, Vec<ApplyOperation>>,
}

impl ApplyArguments {
    fn into_request(self) -> Result<ApplyEditsRequest> {
        if self.edits.is_empty() {
            bail!("edits must contain at least one path");
        }
        let mut paths = std::collections::BTreeMap::new();
        for (mut path, operations) in self.edits {
            strip_root_prefix(&mut path);
            if path.is_empty() {
                bail!("edit paths must be non-empty");
            }
            if paths.insert(path.clone(), operations).is_some() {
                bail!("duplicate edit path after normalization: /{path}");
            }
        }
        let mut edits = Vec::new();
        for (path, operations) in paths {
            if operations.is_empty() {
                bail!("edits for /{path} must contain at least one operation");
            }
            for operation in operations {
                edits.push(operation.into_edit(path.clone()));
            }
        }
        Ok(ApplyEditsRequest {
            summary: self.summary,
            edits,
        })
    }
}

#[derive(Deserialize)]
struct CallParams {
    name: String,
    #[serde(default)]
    arguments: Value,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SearchArguments {
    query: String,
    #[serde(default)]
    variants: Vec<String>,
}

#[derive(Default, Deserialize, Clone, Copy)]
#[serde(rename_all = "snake_case")]
enum PageFormat {
    #[default]
    Hashlines,
    Raw,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum PagePaths {
    Single(String),
    Multiple(Vec<String>),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetArguments {
    path: PagePaths,
    #[serde(default)]
    format: PageFormat,
}

fn split_page_path(path: &str) -> Result<(String, Option<(usize, usize)>)> {
    let Some((path, range)) = path.rsplit_once(':').filter(|(_, suffix)| {
        suffix
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'-')
    }) else {
        return Ok((path.to_owned(), None));
    };
    let (start, end) = range.split_once('-').unwrap_or((range, range));
    let positive_line = |value: &str| {
        value
            .parse::<usize>()
            .ok()
            .filter(|line| *line > 0)
            .context("line ranges must use positive integers: :LINE or :START-END")
    };
    let (start, end) = (positive_line(start)?, positive_line(end)?);
    if start > end {
        bail!("line range start must not exceed its end");
    }
    Ok((path.to_owned(), Some((start, end))))
}

fn read_page(store: &Store, path: &str, format: PageFormat) -> Result<(Value, bool)> {
    let (mut path, window) = split_page_path(path)?;
    let subtree = path
        .strip_suffix("/**/")
        .map(|base| (format!("{base}/"), true))
        .or_else(|| {
            path.strip_suffix("/**")
                .map(|base| (format!("{base}/"), false))
        });
    let directories_only = subtree.as_ref().map(|(_, only)| *only);
    if let Some((directory, _)) = subtree {
        path = directory;
    }
    if path != "/" {
        strip_root_prefix(&mut path);
    }
    if path.ends_with('/') {
        if window.is_some() {
            bail!("line windows are only supported for files");
        }
        if matches!(format, PageFormat::Raw) {
            bail!("raw format is only supported for files");
        }
        let children = match directories_only {
            Some(only) => store.get_subtree(&path, only)?,
            None => store.get_directory(&path)?,
        };
        return Ok((json!({"path": path, "children": children}), true));
    }
    if matches!(format, PageFormat::Raw) && window.is_some() {
        bail!("raw format requires a full-page read");
    }
    let page = store.get_page(&path, window)?;
    let mut value = serde_json::to_value(&page)?;
    if matches!(format, PageFormat::Raw) {
        value["content"] = json!(page.text);
    }
    Ok((value, matches!(format, PageFormat::Hashlines)))
}

fn read_pages(store: &Store, arguments: GetArguments) -> Result<(Value, bool)> {
    match arguments.path {
        PagePaths::Single(path) => read_page(store, &path, arguments.format)
            .map(|(value, plain)| (rooted_page_paths(value), plain)),
        PagePaths::Multiple(paths) => {
            if paths.is_empty() {
                bail!("path arrays must contain at least one path");
            }
            let pages: Vec<_> = paths
                .iter()
                .map(|path| {
                    read_page(store, path, arguments.format)
                        .map(|(value, plain)| (rooted_page_paths(value), plain))
                        .map_err(|error| anyhow::anyhow!("read {path:?}: {error}"))
                })
                .collect::<Result<_>>()?;
            let plain = pages.iter().all(|(_, plain)| *plain);
            let results: Vec<_> = pages.into_iter().map(|(value, _)| value).collect();
            Ok((json!({"results": results}), plain))
        }
    }
}

fn strip_root_prefix(path: &mut String) {
    if path.starts_with('/') {
        path.remove(0);
    }
}

fn root_path_value(value: &mut Value) {
    if let Some(path) = value.as_str() {
        if !path.starts_with('/') {
            *value = json!(format!("/{path}"));
        }
    }
}

fn rooted_page_paths(mut value: Value) -> Value {
    root_path_value(&mut value["path"]);
    if let Some(template) = value.get_mut("template") {
        root_path_value(template);
    }
    if let Some(children) = value.get_mut("children").and_then(Value::as_array_mut) {
        for child in children {
            root_path_value(child);
        }
    }
    value
}

fn page_text(value: &Value) -> String {
    if let Some(children) = value.get("children").and_then(Value::as_array) {
        return children
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join("\n");
    }
    let path = value["path"].as_str().unwrap_or_default();
    let template = value["template"].as_str().unwrap_or("none");
    let content = value["content"].as_str().unwrap_or("File does not exist.");
    format!("Path: {path}\nSchema: {template}\n\n{content}")
}

fn error_location(path: &str, line: Option<usize>) -> String {
    let path = format!("/{}", path.trim_start_matches('/'));
    match line {
        Some(line) => format!("{path}:{line}"),
        None => path,
    }
}

fn validation_errors(validation: &ValidationError) -> Vec<Value> {
    validation
        .findings
        .iter()
        .map(|finding| {
            let mut error = json!({
                "location": error_location(&finding.path, finding.line),
                "message": finding.message,
            });
            if let Some(source) = &finding.source {
                error["schema"] = json!(error_location(&source.path, Some(source.line)));
            }
            error
        })
        .collect()
}

async fn call_tool(id: Value, store: &Arc<Store>, params: Value) -> Response {
    let params: CallParams = match serde_json::from_value(params) {
        Ok(value) => value,
        Err(error) => return rpc_error(id, -32602, &format!("invalid tool call: {error}")),
    };
    let mut plain_page = false;
    let result = match params.name.as_str() {
        "search" => match serde_json::from_value::<SearchArguments>(params.arguments) {
            Ok(arguments) => store
                .search(&arguments.query, &arguments.variants)
                .await
                .and_then(|response| serde_json::to_value(response).map_err(Into::into)),
            Err(error) => Err(error.into()),
        },
        "read" => match serde_json::from_value::<GetArguments>(params.arguments) {
            Ok(arguments) => read_pages(store, arguments).map(|(value, plain)| {
                plain_page = plain;
                value
            }),
            Err(error) => Err(error.into()),
        },
        "edit" => match serde_json::from_value::<ApplyArguments>(params.arguments)
            .map_err(anyhow::Error::from)
            .and_then(ApplyArguments::into_request)
        {
            Ok(arguments) => {
                let blocking_store = Arc::clone(store);
                run_blocking(move || blocking_store.apply_edits(&arguments))
                    .await
                    .map(|_| json!({"success": true}))
            }
            Err(error) => Err(error),
        },
        _ => return rpc_error(id, -32602, "unknown tool"),
    };
    match result {
        Ok(value) => {
            let result = if plain_page {
                let pages = value.get("results").and_then(Value::as_array);
                let content: Vec<_> = pages.map_or_else(
                    || vec![json!({"type": "text", "text": page_text(&value)})],
                    |pages| {
                        pages
                            .iter()
                            .map(|page| json!({"type": "text", "text": page_text(page)}))
                            .collect()
                    },
                );
                json!({"content": content, "isError": false})
            } else {
                json!({"content": [], "structuredContent": value, "isError": false})
            };
            rpc_result(id, result)
        }
        Err(error) => {
            if params.name == "edit" {
                let errors = match error.downcast_ref::<ValidationError>() {
                    Some(validation) => json!(validation_errors(validation)),
                    None => json!([{"message": error.to_string()}]),
                };
                return rpc_result(
                    id,
                    json!({
                        "content": [],
                        "structuredContent": {"errors": errors},
                        "isError": true
                    }),
                );
            }
            let result = json!({
                "content": [{"type": "text", "text": error.to_string()}],
                "isError": true
            });
            rpc_result(id, result)
        }
    }
}

fn rpc_result(id: Value, result: Value) -> Response {
    let mut response = serde_json::Map::new();
    response.insert("jsonrpc".into(), json!("2.0"));
    response.insert("id".into(), id);
    response.insert("result".into(), result);
    (StatusCode::OK, Json(Value::Object(response))).into_response()
}

fn rpc_error(id: Value, code: i64, message: &str) -> Response {
    let mut response = serde_json::Map::new();
    response.insert("jsonrpc".into(), json!("2.0"));
    response.insert("id".into(), id);
    response.insert("error".into(), json!({"code": code, "message": message}));
    (StatusCode::OK, Json(Value::Object(response))).into_response()
}
