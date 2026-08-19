use std::{net::SocketAddr, sync::Arc};

use anyhow::{Context, Result, bail};
use axum::{
    Json, Router,
    body::Bytes,
    extract::{Request, State},
    http::{HeaderMap, StatusCode, header::ORIGIN},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde::Deserialize;
use serde_json::{Value, json};
use tower_http::trace::TraceLayer;

use crate::{ApplyEditsRequest, Store, store::ValidationError};

pub const MCP_PROTOCOL_VERSION: &str = "2025-06-18";
pub const LEGACY_MCP_PROTOCOL_VERSION: &str = "2025-03-26";

#[derive(Clone)]
struct AppState {
    store: Arc<Store>,
    bearer_token: Option<String>,
}

pub fn router(store: Arc<Store>, bearer_token: Option<String>) -> Router {
    let state = AppState {
        store,
        bearer_token,
    };
    Router::new()
        .route("/health", get(health))
        .route("/mcp", post(mcp))
        .route("/cli", post(cli))
        .layer(middleware::from_fn_with_state(state.clone(), authorize))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

pub async fn serve(
    store: Arc<Store>,
    listen: SocketAddr,
    bearer_token: Option<String>,
) -> Result<()> {
    if !listen.ip().is_loopback() && bearer_token.as_deref().is_none_or(str::is_empty) {
        bail!("a bearer token is required when listening beyond loopback");
    }
    let listener = tokio::net::TcpListener::bind(listen)
        .await
        .with_context(|| format!("bind {listen}"))?;
    tracing::info!(%listen, "mdstore listening");
    axum::serve(listener, router(store, bearer_token))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}

async fn health(State(state): State<AppState>) -> Response {
    match state.store.status() {
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

#[derive(Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
enum CliCommand {
    Validate,
    Reindex,
    Push,
}

async fn cli(State(state): State<AppState>, Json(command): Json<CliCommand>) -> Response {
    match command {
        CliCommand::Validate => match state.store.validate() {
            Ok(()) => Json(json!({"valid": true})).into_response(),
            Err(findings) => Json(json!({"valid": false, "findings": findings})).into_response(),
        },
        CliCommand::Reindex => match state.store.reindex().await {
            Ok(()) => match state.store.status() {
                Ok(status) => Json(json!(status)).into_response(),
                Err(error) => internal_error(error),
            },
            Err(error) => internal_error(error),
        },
        CliCommand::Push => match state.store.push() {
            Ok(push) => {
                let background = state.store.clone();
                tokio::spawn(async move {
                    if let Err(error) = background.reindex_missing().await {
                        tracing::warn!(%error, "post-refresh embedding rebuild is degraded");
                    }
                });
                Json(json!(push)).into_response()
            }
            Err(error) => internal_error(error),
        },
    }
}

fn internal_error(error: anyhow::Error) -> Response {
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({"error": error.to_string()})),
    )
        .into_response()
}

async fn authorize(State(state): State<AppState>, request: Request, next: Next) -> Response {
    if request.headers().contains_key(ORIGIN) {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error": "browser origins are not allowed"})),
        )
            .into_response();
    }
    if let Some(expected) = &state.bearer_token {
        let actual = request
            .headers()
            .get("authorization")
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.strip_prefix("Bearer "));
        if actual != Some(expected.as_str()) {
            return (
                StatusCode::UNAUTHORIZED,
                Json(json!({"error": "unauthorized"})),
            )
                .into_response();
        }
    }
    next.run(request).await
}

async fn mcp(State(state): State<AppState>, headers: HeaderMap, body: Bytes) -> Response {
    if let Some(version) = headers.get("mcp-protocol-version") {
        let Ok(version) = version.to_str() else {
            return StatusCode::BAD_REQUEST.into_response();
        };
        if !matches!(version, MCP_PROTOCOL_VERSION | LEGACY_MCP_PROTOCOL_VERSION) {
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
        "tools/call" => call_tool(id, &state.store, params).await,
        _ => rpc_error(id, -32601, "method not found"),
    }
}

fn initialize(id: Value, params: &Value) -> Response {
    let Some(requested) = params.get("protocolVersion").and_then(Value::as_str) else {
        return rpc_error(id, -32602, "initialize requires protocolVersion");
    };
    let negotiated = if matches!(
        requested,
        MCP_PROTOCOL_VERSION | LEGACY_MCP_PROTOCOL_VERSION
    ) {
        requested
    } else {
        MCP_PROTOCOL_VERSION
    };
    rpc_result(
        id,
        json!({
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": false}},
            "serverInfo": {"name": "mdstore", "version": env!("CARGO_PKG_VERSION")}
        }),
    )
}

#[must_use]
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
            "description": "Read tracked Markdown or a .mdstore configuration resource with hashline anchors.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1}
                },
                "required": ["path"],
                "additionalProperties": false
            }
        }),
        json!({
            "name": "apply_edits",
            "description": "Atomically validate, commit, and push a hashline-anchored edit batch.",
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
    [
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
    .collect()
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

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct GetArguments {
    path: String,
    start_line: Option<usize>,
    end_line: Option<usize>,
}

async fn call_tool(id: Value, store: &Arc<Store>, params: Value) -> Response {
    let params: CallParams = match serde_json::from_value(params) {
        Ok(value) => value,
        Err(error) => return rpc_error(id, -32602, &format!("invalid tool call: {error}")),
    };
    let result = match params.name.as_str() {
        "search" => match serde_json::from_value::<SearchArguments>(params.arguments) {
            Ok(arguments) => store
                .search(&arguments.query, &arguments.variants)
                .await
                .and_then(|value| serde_json::to_value(value).map_err(Into::into)),
            Err(error) => Err(error.into()),
        },
        "get_page" => match serde_json::from_value::<GetArguments>(params.arguments) {
            Ok(arguments) => {
                let window = arguments
                    .start_line
                    .map(|start| (start, arguments.end_line.unwrap_or(start)));
                store
                    .get_page(&arguments.path, window)
                    .and_then(|value| serde_json::to_value(value).map_err(Into::into))
            }
            Err(error) => Err(error.into()),
        },
        "apply_edits" => match serde_json::from_value::<ApplyEditsRequest>(params.arguments) {
            Ok(arguments) => match store.apply_edits(&arguments) {
                Ok(value) => {
                    let background = Arc::clone(store);
                    tokio::spawn(async move {
                        if let Err(error) = background.reindex_missing().await {
                            tracing::warn!(%error, "post-edit embedding rebuild is degraded");
                        }
                    });
                    serde_json::to_value(value).map_err(Into::into)
                }
                Err(error) => Err(error),
            },
            Err(error) => Err(error.into()),
        },
        _ => return rpc_error(id, -32602, "unknown tool"),
    };
    match result {
        Ok(value) => rpc_result(
            id,
            json!({
                "content": [{"type": "text", "text": serde_json::to_string_pretty(&value).unwrap_or_default()}],
                "structuredContent": value,
                "isError": false
            }),
        ),
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
    (
        StatusCode::OK,
        Json(json!({"jsonrpc": "2.0", "id": id, "result": result})),
    )
        .into_response()
}

fn rpc_error(id: Value, code: i64, message: &str) -> Response {
    (
        StatusCode::OK,
        Json(json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})),
    )
        .into_response()
}
