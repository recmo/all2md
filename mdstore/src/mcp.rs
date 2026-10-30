use std::{net::SocketAddr, sync::Arc};

use anyhow::{Context, Result, bail};
use axum::{
    Json, Router,
    body::Bytes,
    extract::{Path, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde::Deserialize;
use serde_json::{Value, json};
use tower_http::trace::TraceLayer;

use crate::{
    ApplyEditsRequest, Store,
    hashline::HashlineEditError,
    store::{StoreState, ValidationError},
};

/// Preferred MCP protocol revision.
pub const MCP_PROTOCOL_VERSION: &str = "2025-06-18";

fn router(store: Arc<Store>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/mcp", post(mcp))
        .route("/path", get(raw_root))
        .route("/path/", get(raw_root))
        .route("/path/{*path}", get(raw_path))
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
pub async fn serve_listener(listener: tokio::net::TcpListener, store: Arc<Store>) -> Result<()> {
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

async fn raw_root(State(store): State<Arc<Store>>, headers: HeaderMap) -> Response {
    raw_document(&store, "/", &headers)
}

async fn raw_path(
    State(store): State<Arc<Store>>,
    Path(path): Path<String>,
    headers: HeaderMap,
) -> Response {
    raw_document(&store, &path, &headers)
}

fn raw_document(store: &Store, path: &str, headers: &HeaderMap) -> Response {
    let state = store.read_state();
    let result = if path.ends_with('/') {
        state
            .get_directory_source(path)
            .map(|(revision, children)| (revision, Some(rooted_directory(children)), None))
    } else {
        state
            .get_source(path)
            .map(|source| (source.revision, source.text, source.template))
    };
    let (revision, text, template) = match result {
        Ok(source) => source,
        Err(error) => {
            return (
                StatusCode::NOT_FOUND,
                [(header::CACHE_CONTROL, "no-store")],
                error.to_string(),
            )
                .into_response();
        }
    };
    let etag = format!("\"{revision}\"");
    let unchanged = text.is_some()
        && headers.get_all(header::IF_NONE_MATCH).iter().any(|value| {
            value.to_str().is_ok_and(|value| {
                value.split(',').any(|tag| {
                    let tag = tag.trim();
                    tag == "*" || tag.strip_prefix("W/").unwrap_or(tag) == etag
                })
            })
        });
    let status = if unchanged {
        StatusCode::NOT_MODIFIED
    } else if text.is_some() {
        StatusCode::OK
    } else {
        StatusCode::NOT_FOUND
    };
    let body = if unchanged {
        String::new()
    } else {
        text.unwrap_or_default()
    };
    let mut response = (status, [(header::CACHE_CONTROL, "no-cache")], body).into_response();
    response.headers_mut().insert(
        header::ETAG,
        HeaderValue::from_str(&etag).expect("Git object ID"),
    );
    if let Some(template) = template {
        let mut url = reqwest::Url::parse("http://mdstore.invalid/path").expect("static URL");
        url.path_segments_mut()
            .expect("hierarchical URL")
            .extend(template.trim_start_matches('/').split('/'));
        let link = format!("<{}>; rel=\"describedby\"", url.path());
        response.headers_mut().insert(
            header::LINK,
            HeaderValue::from_str(&link).expect("encoded URL"),
        );
    }
    response
}

fn rooted_directory(children: Vec<String>) -> String {
    children
        .into_iter()
        .map(|path| format!("/{path}"))
        .collect::<Vec<_>>()
        .join("\n")
}

async fn run_blocking<T>(operation: impl FnOnce() -> Result<T> + Send + 'static) -> Result<T>
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
            "description": "Search documents; variants optionally add query formulations. Returns snapshot commit, ranked /path:start-end excerpts with edit-ready anchors, and degraded retrieval warnings.",
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
            "description": "Read files or directories. Paths are repository-rooted; leading / is optional. Files return path, schema path, and LINE:HH|text (two hex digits). Read the schema separately for rules. Missing Markdown files return their schema for creation. Directories list child paths with trailing / for directories. Arrays preserve order; any invalid read rejects the batch. One commit header applies to all returned blocks.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "description": "Path or non-empty path array. Files: :LINE or :START-END (one-based, inclusive). Directories: / for root, trailing / for children, /** for all descendants, /**/ for descendant directories only.",
                        "oneOf": [
                            {"type": "string", "minLength": 1},
                            {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
                        ]
                    }
                },
                "required": ["path"],
                "additionalProperties": false
            }
        }),
        json!({
            "name": "edit",
            "description": "Validate and commit one atomic batch. Edits, anchors, and links use original paths/snapshot; moves run after edits. Copy anchors from read/search; reread on stale errors. Replacement text omits hashline prefixes; whole-file edits require a complete-file anchor. Config/template edits require permission. Create paths support {serial:03}; read the parent for allocated names. Returns {success:true,commit} or {errors:[{location?,message,schema?}]}.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "minLength": 1},
                    "expected_commit": {"type": "string", "description": "Commit from read/search; required for moves. Stale batches fail."},
                    "moves": {
                        "type": "array", "minItems": 1,
                        "description": "Simultaneous document moves; chains/swaps allowed. Occupied destinations must move or be deleted. Rewrites incoming Markdown/wiki links and rebases outgoing relative links.",
                        "items": {
                            "type": "object",
                            "properties": {"from": {"type": "string", "minLength": 1}, "to": {"type": "string", "minLength": 1}},
                            "required": ["from", "to"], "additionalProperties": false
                        }
                    },
                    "edits": {
                        "description": "Paths (leading / optional) mapped to operations.",
                        "type": "object",
                        "propertyNames": {"minLength": 1},
                        "additionalProperties": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"oneOf": edit_operation_schemas()}
                        }
                    }
                },
                "required": ["summary"],
                "anyOf": [{"required": ["edits"]}, {"required": ["moves", "expected_commit"]}],
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
                "anchor": {"type": "string", "description": "LINE:HH or START:HH..END:HH, inclusive. Complete range for whole-file edits; 0: for an empty file; omit to create a missing file."},
                "content": {"type": "string", "description": "Replacement text; empty deletes the selected lines or whole file."}
            },
            "required": ["op", "content"],
            "additionalProperties": false
        }),
        json!({
            "type": "object",
            "properties": {
                "op": {"const": "append"},
                "anchor": {"type": "string", "description": "LINE:HH to insert after; 0: for file start; omit for EOF. File must exist."},
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
    #[serde(default)]
    edits: std::collections::BTreeMap<String, Vec<ApplyOperation>>,
    expected_commit: Option<String>,
    #[serde(default)]
    moves: Vec<crate::Move>,
}

impl ApplyArguments {
    fn into_request(mut self) -> Result<ApplyEditsRequest> {
        if self.edits.is_empty() && self.moves.is_empty() {
            bail!("edits or moves must contain at least one path");
        }
        for movement in &mut self.moves {
            strip_root_prefix(&mut movement.from);
            strip_root_prefix(&mut movement.to);
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
            expected_commit: self.expected_commit,
            moves: self.moves,
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

fn read_page(state: &StoreState, path: &str) -> Result<String> {
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
        let children = match directories_only {
            Some(only) => state.get_subtree(&path, only)?,
            None => state.get_directory(&path)?,
        };
        return Ok(rooted_directory(children));
    }
    let page = state.get_page(&path, window)?;
    let template = page.template.as_deref().unwrap_or("none");
    let content = page.content.as_deref().unwrap_or("File does not exist.");
    Ok(format!("Path: /{path}\nSchema: {template}\n\n{content}"))
}

fn read_pages(store: &Store, arguments: GetArguments) -> Result<Value> {
    let paths = match arguments.path {
        PagePaths::Single(path) => vec![path],
        PagePaths::Multiple(paths) => paths,
    };
    if paths.is_empty() {
        bail!("path arrays must contain at least one path");
    }
    let state = store.read_state();
    let mut content = vec![json!({"type": "text", "text": format!("Commit: {}", state.head)})];
    for path in paths {
        let text =
            read_page(&state, &path).map_err(|error| anyhow::anyhow!("read {path:?}: {error}"))?;
        content.push(json!({"type": "text", "text": text}));
    }
    Ok(json!(content))
}

fn strip_root_prefix(path: &mut String) {
    if path.starts_with('/') {
        path.remove(0);
    }
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
    let plain_page = params.name == "read";
    let result = match params.name.as_str() {
        "search" => match serde_json::from_value::<SearchArguments>(params.arguments) {
            Ok(arguments) => store
                .search(&arguments.query, &arguments.variants)
                .await
                .and_then(|response| serde_json::to_value(response).map_err(Into::into)),
            Err(error) => Err(error.into()),
        },
        "read" => match serde_json::from_value::<GetArguments>(params.arguments) {
            Ok(arguments) => read_pages(store, arguments),
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
                    .map(|commit| json!({"success": true, "commit": commit}))
            }
            Err(error) => Err(error),
        },
        _ => return rpc_error(id, -32602, "unknown tool"),
    };
    match result {
        Ok(value) => {
            let result = if plain_page {
                json!({"content": value, "isError": false})
            } else {
                json!({"content": [], "structuredContent": value, "isError": false})
            };
            rpc_result(id, result)
        }
        Err(error) => {
            if params.name == "edit" {
                let errors = match error.downcast_ref::<ValidationError>() {
                    Some(validation) => json!(validation_errors(validation)),
                    None => match error.downcast_ref::<HashlineEditError>() {
                        Some(edit) => json!([{
                            "location": error_location(&edit.path, edit.line),
                            "message": edit.source.to_string(),
                        }]),
                        None => json!([{"message": error.to_string()}]),
                    },
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
