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
    ["search", "get_page", "apply_edits"]
}

fn tools() -> Vec<Value> {
    vec![
        json!({
            "name": "search",
            "description": "Hybrid exact, embedding, graph-assisted, and reranked search. Query expansion belongs to the caller.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "variants": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["query", "variants"],
                "additionalProperties": false
            }
        }),
        json!({
            "name": "get_page",
            "description": "Read a file or directory from the published document tree. Use / for the root or a path ending in / for a directory: returns direct children, permissions, repository identity and Git revision. File reads return only path, template and content; returned paths start with / at the repository root. Reads and edits also accept paths without that prefix. Default file reads return a plain-text path/schema header followed by four-character neighbor-based hashlines, without a structured duplicate. Anchors must match at the supplied line number; stale anchors require a fresh read. Use format=raw for structured results containing exact source text (including line endings) for whole-page replacements. Directory reads and expanded-template requests also return structured results. Markdown responses identify the nearest schema by its repository-rooted template path with a leading /; read that path separately for instructions and rules. Editors can explicitly request include_template=true for the expanded template. A proposed Markdown path returns content=null and its template for discovery before creation. Configuration edits require server.allow_config_edits; template edits require server.allow_template_edits.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "format": {"type": "string", "enum": ["hashlines", "raw"], "default": "hashlines"},
                    "include_template": {"type": "boolean", "default": false}
                },
                "required": ["path"],
                "additionalProperties": false
            }
        }),
        json!({
            "name": "apply_edits",
            "description": "Atomically validate and locally commit an edit batch. Use hashline anchors for partial edits, or replace_page with exact original base text for whole-page edits. Config edits require server.allow_config_edits; listener changes require a restart. Template edits require server.allow_template_edits and validate the complete corpus against the proposed templates. Create paths may contain {serial:03} for template-governed allocation; resolved paths are returned. Git replication runs in the background.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "edit_summary": {"type": "string", "minLength": 1},
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"oneOf": edit_operation_schemas()}
                    }
                },
                "required": ["edit_summary", "edits"],
                "additionalProperties": false
            }
        }),
    ]
}

fn edit_operation_schemas() -> Vec<Value> {
    let mut schemas: Vec<Value> = [
        ("replace", true, true),
        ("insert_before", true, true),
        ("insert_after", true, true),
        ("delete", true, false),
        ("create_page", false, true),
        ("remove_page", true, false),
    ]
    .into_iter()
    .map(|(operation, anchor, content)| {
        let mut properties = serde_json::Map::from_iter([
            ("op".into(), json!({"const": operation})),
            ("path".into(), json!({"type": "string", "minLength": 1})),
        ]);
        let mut required = vec!["op", "path"];
        if anchor {
            properties.insert("anchor".into(), json!({"type": "string", "minLength": 2}));
            required.push("anchor");
        }
        if content {
            properties.insert("content".into(), json!({"type": "string"}));
            required.push("content");
        }
        json!({
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": false
        })
    })
    .collect();
    schemas.push(json!({
        "type": "object",
        "properties": {"op": {"const": "replace_page"}, "path": {"type": "string"}, "base": {"type": "string"}, "content": {"type": "string"}},
        "required": ["op", "path", "base", "content"], "additionalProperties": false
    }));
    schemas.push(json!({
        "type": "object",
        "properties": {"op": {"const": "delete_page"}, "path": {"type": "string"}, "base": {"type": "string"}},
        "required": ["op", "path", "base"], "additionalProperties": false
    }));
    schemas
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
    variants: Vec<String>,
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "snake_case")]
enum PageFormat {
    #[default]
    Hashlines,
    Raw,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetArguments {
    path: String,
    start_line: Option<usize>,
    end_line: Option<usize>,
    #[serde(default)]
    format: PageFormat,
    #[serde(default)]
    include_template: bool,
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
    if value.get("template").is_some_and(Value::is_object) {
        root_path_value(&mut value["template"]["path"]);
    } else if let Some(template) = value.get_mut("template") {
        root_path_value(template);
    }
    if let Some(children) = value.get_mut("children").and_then(Value::as_array_mut) {
        for child in children {
            root_path_value(&mut child["path"]);
        }
    }
    value
}

fn page_text(value: &Value) -> String {
    let path = value["path"].as_str().unwrap_or_default();
    let template = value["template"].as_str().unwrap_or("none");
    let content = value["content"].as_str().unwrap_or("File does not exist.");
    format!("Path: {path}\nSchema: {template}\n\n{content}")
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
                .and_then(|value| serde_json::to_value(value).map_err(Into::into)),
            Err(error) => Err(error.into()),
        },
        "get_page" => match serde_json::from_value::<GetArguments>(params.arguments) {
            Ok(mut arguments) => {
                if arguments.path != "/" {
                    strip_root_prefix(&mut arguments.path);
                }
                plain_page = matches!(arguments.format, PageFormat::Hashlines)
                    && !arguments.include_template
                    && !arguments.path.ends_with('/');
                let window = arguments
                    .start_line
                    .map(|start| (start, arguments.end_line.unwrap_or(start)));
                if arguments.path.ends_with('/') {
                    if arguments.start_line.is_some() || arguments.end_line.is_some() {
                        Err(anyhow::anyhow!("line windows are only supported for files"))
                    } else {
                        store.get_directory(&arguments.path)
                    }
                } else {
                    if matches!(arguments.format, PageFormat::Raw)
                        && (arguments.start_line.is_some() || arguments.end_line.is_some())
                    {
                        Err(anyhow::anyhow!("raw format requires a full-page read"))
                    } else {
                        store.get_page(&arguments.path, window).and_then(|page| {
                            let mut value = serde_json::to_value(&page)?;
                            if matches!(arguments.format, PageFormat::Raw) {
                                value["content"] = json!(page.text);
                            }
                            if arguments.include_template {
                                value["template"] = json!(page.template);
                            }
                            Ok(value)
                        })
                    }
                }
            }
            Err(error) => Err(error.into()),
        },
        "apply_edits" => match serde_json::from_value::<ApplyEditsRequest>(params.arguments) {
            Ok(mut arguments) => {
                for edit in &mut arguments.edits {
                    strip_root_prefix(edit.path_mut());
                }
                let blocking_store = Arc::clone(store);
                match run_blocking(move || blocking_store.apply_edits(&arguments)).await {
                    Ok(value) => serde_json::to_value(value).map_err(Into::into),
                    Err(error) => Err(error),
                }
            }
            Err(error) => Err(error.into()),
        },
        _ => return rpc_error(id, -32602, "unknown tool"),
    };
    let result = if params.name == "get_page" {
        result.map(rooted_page_paths)
    } else {
        result
    };
    match result {
        Ok(value) => {
            let result = if plain_page {
                json!({
                    "content": [{"type": "text", "text": page_text(&value)}],
                    "isError": false
                })
            } else {
                json!({"content": [], "structuredContent": value, "isError": false})
            };
            rpc_result(id, result)
        }
        Err(error) => {
            let findings = error
                .downcast_ref::<ValidationError>()
                .map(|validation| validation.findings.clone());
            let mut result = json!({
                "content": [{"type": "text", "text": error.to_string()}],
                "isError": true
            });
            if let Some(findings) = findings {
                result["structuredContent"] = json!({"validation_findings": findings});
            }
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
