//! Repository resources use their own URLs; text writes reuse the validated edit path.
use super::*;
use crate::config::{is_config_resource_path, validate_repo_path};
use axum::{
    body::Body,
    extract::{Path, Query},
};

pub(super) fn routes() -> Router<AppState> {
    Router::new()
        .route("/", get(root))
        .route("/{*path}", get(read).put(write).delete(delete))
}
async fn root(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if worker(&state, &headers) {
        return StatusCode::FORBIDDEN.into_response();
    }
    match run_blocking(move || state.store.read_state().file_directory("/")).await {
        Ok(value) => json_read(&value, &headers).unwrap_or_else(problem::error),
        Err(e) => problem::error(e),
    }
}
#[derive(Default, Deserialize)]
struct Lease {
    job: Option<String>,
    attempt: Option<String>,
}
fn worker(state: &AppState, headers: &HeaderMap) -> bool {
    state.worker_token.as_deref().is_some_and(|token| {
        headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| v.strip_prefix("Bearer ") == Some(token))
    })
}
fn lease(query: &Lease) -> Result<(&str, &str)> {
    Ok((
        query.job.as_deref().context("worker job is required")?,
        query
            .attempt
            .as_deref()
            .context("worker attempt is required")?,
    ))
}
fn etag(headers: &HeaderMap, hash: &str) -> bool {
    headers
        .get("if-none-match")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| {
            v.trim() == "*"
                || v.split(',').any(|part| {
                    part.trim().strip_prefix("W/").unwrap_or(part.trim()) == format!("\"{hash}\"")
                })
        })
}
fn not_modified(hash: &str) -> Response {
    (
        StatusCode::NOT_MODIFIED,
        [
            ("etag", format!("\"{hash}\"")),
            ("vary", "Accept".into()),
            ("cache-control", "private, no-store".into()),
        ],
    )
        .into_response()
}
fn json_read(value: &impl serde::Serialize, headers: &HeaderMap) -> Result<Response> {
    let bytes = serde_json::to_string(value)?;
    let hash = crate::validation::source_hash(&bytes);
    if etag(headers, &hash) {
        return Ok(not_modified(&hash));
    }
    Ok(Response::builder()
        .header("content-type", "application/json")
        .header("content-length", bytes.len())
        .header("etag", format!("\"{hash}\""))
        .header("vary", "Accept")
        .header("cache-control", "private, no-store")
        .body(Body::from(bytes))?)
}
async fn read(
    State(state): State<AppState>,
    Path(path): Path<String>,
    Query(query): Query<Lease>,
    headers: HeaderMap,
) -> Response {
    let result = async {
        let is_worker = worker(&state, &headers);
        let asset = if is_worker {
            let (job, attempt) = lease(&query)?;
            Some(state.store.leased_asset(job, attempt, &path)?)
        } else {
            if path.ends_with('/') {
                return json_read(&state.store.read_state().file_directory(&path)?, &headers);
            }
            validate_repo_path(&path)?;
            state.store.asset(&path).ok()
        };
        let metadata = !is_worker
            && headers
                .get("accept")
                .is_some_and(|value| value == "application/vnd.mdstore.page+json");
        let mut response = if metadata {
            return json_read(&state.store.file_page(&path)?, &headers);
        } else if let Some(asset) = asset {
            if etag(&headers, &asset.oid) {
                return Ok(not_modified(&asset.oid));
            }
            transfers::serve_asset(&state.store, asset, &headers).await?
        } else {
            let page = match state.store.file_page(&path) {
                Ok(page) if page.exists => page,
                _ => return Ok(StatusCode::NOT_FOUND.into_response()),
            };
            let hash = page.hash.context("missing file hash")?;
            if etag(&headers, &hash) {
                return Ok(not_modified(&hash));
            }
            let text = page.text.unwrap_or_default();
            Response::builder()
                .header("etag", format!("\"{hash}\""))
                .header("content-length", text.len())
                .body(Body::from(text))?
        };
        if !metadata {
            response
                .headers_mut()
                .insert("content-type", crate::media::media_type(&path).parse()?);
        }
        response
            .headers_mut()
            .insert("cache-control", "private, no-store".parse()?);
        response.headers_mut().insert("vary", "Accept".parse()?);
        response
            .headers_mut()
            .insert("x-content-type-options", "nosniff".parse()?);
        Ok::<_, anyhow::Error>(response)
    }
    .await;
    result.unwrap_or_else(problem::error)
}
fn condition(headers: &HeaderMap) -> std::result::Result<Option<String>, Response> {
    if headers.get("if-none-match").is_some_and(|v| v == "*") && !headers.contains_key("if-match") {
        return Ok(None);
    }
    if let Some(value) = headers.get("if-match").and_then(|v| v.to_str().ok())
        && !headers.contains_key("if-none-match")
        && let Some(hash) = value.strip_prefix('"').and_then(|v| v.strip_suffix('"'))
        && !hash.is_empty()
        && !hash.contains('"')
    {
        return Ok(Some(hash.into()));
    }
    Err((StatusCode::PRECONDITION_REQUIRED, Json(json!({"error": "Use If-None-Match: * to create, or If-Match: <quoted ETag> to replace/delete"}))).into_response())
}
async fn write(
    State(state): State<AppState>,
    Path(path): Path<String>,
    Query(query): Query<Lease>,
    headers: HeaderMap,
    body: Body,
) -> Response {
    if worker(&state, &headers) {
        let result = async {
            let (job, attempt) = lease(&query)?;
            state.store.check_output_lease(job, attempt, &path)?;
            let asset =
                transfers::receive_object(state.store.clone(), body, 64 * 1024 * 1024).await?;
            state.store.check_output_lease(job, attempt, &path)?;
            Ok::<_, anyhow::Error>(Json(asset).into_response())
        }
        .await;
        return result.unwrap_or_else(problem::error);
    }
    let expected = match condition(&headers) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let result = async {
        validate_repo_path(&path)?;
        let created = expected.is_none();
        let mut response = if path.ends_with(".md") || is_config_resource_path(&path) {
            let bytes = axum::body::to_bytes(body, 16 * 1024 * 1024).await?;
            let text = String::from_utf8(bytes.to_vec())?;
            run_blocking(move || {
                let page = state
                    .store
                    .edit_file(&path, expected.as_deref(), Some(text))?
                    .context("missing written page")?;
                let hash = page.hash.clone().context("missing file hash")?;
                let mut response = Json(page).into_response();
                response
                    .headers_mut()
                    .insert("etag", format!("\"{hash}\"").parse()?);
                Ok(response)
            })
            .await?
        } else {
            let asset =
                transfers::receive_object(state.store.clone(), body, 16 * 1024 * 1024 * 1024)
                    .await?;
            run_blocking(move || {
                state.store.put_asset(path, asset.clone(), expected)?;
                let hash = asset.oid.clone();
                let mut response = Json(asset).into_response();
                response
                    .headers_mut()
                    .insert("etag", format!("\"{hash}\"").parse()?);
                Ok(response)
            })
            .await?
        };
        *response.status_mut() = if created {
            StatusCode::CREATED
        } else {
            StatusCode::OK
        };
        Ok::<_, anyhow::Error>(response)
    }
    .await;
    result.unwrap_or_else(problem::error)
}
async fn delete(
    State(state): State<AppState>,
    Path(path): Path<String>,
    headers: HeaderMap,
) -> Response {
    if worker(&state, &headers) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let expected = match condition(&headers) {
        Ok(Some(value)) => value,
        Ok(None) => return StatusCode::PRECONDITION_FAILED.into_response(),
        Err(response) => return response,
    };
    let result = run_blocking(move || {
        validate_repo_path(&path)?;
        if state.store.asset(&path).is_ok() {
            state.store.delete_asset(&path, &expected)?;
        } else {
            state.store.edit_file(&path, Some(&expected), None)?;
        }
        Ok(StatusCode::NO_CONTENT.into_response())
    })
    .await;
    result.unwrap_or_else(problem::error)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::EditOperation;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    async fn send(
        app: &Router,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
        body: impl Into<Body>,
    ) -> (StatusCode, HeaderMap, Vec<u8>) {
        let mut request = Request::builder()
            .method(method)
            .uri(path)
            .header("host", "localhost");
        for (key, value) in headers {
            request = request.header(*key, *value);
        }
        let response = app
            .clone()
            .oneshot(request.body(body.into()).unwrap())
            .await
            .unwrap();
        (
            response.status(),
            response.headers().clone(),
            response
                .into_body()
                .collect()
                .await
                .unwrap()
                .to_bytes()
                .to_vec(),
        )
    }
    fn fixture() -> (tempfile::TempDir, Router, Arc<Store>) {
        let (dir, store) = crate::store::artifacts::tests::fixture();
        let app = application(AppState {
            store: store.clone(),
            worker_token: Some("worker".into()),
        });
        (dir, app, store)
    }
    #[tokio::test]
    async fn processing_uses_config_health_and_worker_without_mcp_extensions() {
        let (_dir, app, store) = fixture();
        let auth = ("authorization", "Bearer user");
        let worker = ("authorization", "Bearer worker");
        send(
            &app,
            "PUT",
            "/audio.wav",
            &[auth, ("if-none-match", "*")],
            "audio",
        )
        .await;
        async fn configure(app: &Router, store: &Store, declarations: Value) -> StatusCode {
            let page = store.file_page("config.yaml").unwrap();
            let mut config = crate::config::Config::from_yaml(page.text.as_ref().unwrap()).unwrap();
            config.derivations = serde_json::from_value(declarations).unwrap();
            let response = send(
                app,
                "PUT",
                "/config.yaml",
                &[
                    ("authorization", "Bearer user"),
                    ("if-match", &format!("\"{}\"", page.hash.unwrap())),
                ],
                serde_yaml::to_string(&config).unwrap(),
            )
            .await;
            response.0
        }
        let definition = json!({"source":"recording.md","recipe":"fixture","fields":["hotwords"],"inputs":["audio.wav"],"outputs":["transcript.md"]});
        assert_eq!(
            configure(&app, &store, json!({"recording": definition})).await,
            StatusCode::OK
        );
        let head = store.file_page("config.yaml").unwrap().revision;
        assert!(
            !configure(
                &app,
                &store,
                json!({"recording": definition, "duplicate": definition})
            )
            .await
            .is_success()
        );
        assert_eq!(store.file_page("config.yaml").unwrap().revision, head);
        for (name, args) in [
            ("get", json!({"resource":"jobs"})),
            ("get", json!({"resource":"derivations"})),
            (
                "edit",
                json!({"resource":"jobs","action":"retry","id":"recording"}),
            ),
            ("search", json!({"space":{},"vector":[],"limit":1})),
        ] {
            let (_, _, body) = send(&app, "POST", "/mcp", &[auth], json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":name,"arguments":args}}).to_string()).await;
            let response: Value = serde_json::from_slice(&body).unwrap();
            assert!(response["result"]["isError"] == true || response["error"]["code"] == -32602);
        }
        assert_eq!(
            send(&app, "POST", "/mcp", &[worker], "{}").await.0,
            StatusCode::FORBIDDEN
        );
        let claim = json!({"op":"claim","recipes":["fixture"]});
        assert_eq!(
            send(
                &app,
                "POST",
                "/worker",
                &[auth, ("content-type", "application/json")],
                claim.to_string()
            )
            .await
            .0,
            StatusCode::FORBIDDEN
        );
        let (_, _, body) = send(
            &app,
            "POST",
            "/worker",
            &[worker, ("content-type", "application/json")],
            claim.to_string(),
        )
        .await;
        let assignment: Value = serde_json::from_slice(&body).unwrap();
        let attempt = assignment["job"]["attempt"].as_str().unwrap();
        let query = json!({"space":{"namespace":"speakers","recipe":"fixture","dimensions":2},"vector":[1,0],"limit":5});
        for request in [
            json!({"op":"search","id":"recording","attempt":attempt,"query":query}),
            json!({"op":"heartbeat","id":"recording","attempt":attempt,"progress":{"stage":"working"}}),
            json!({"op":"complete","id":"recording","attempt":attempt,"outputs":{"transcript.md":"# Transcript\n"}}),
        ] {
            let (status, _, body) = send(
                &app,
                "POST",
                "/worker",
                &[worker, ("content-type", "application/json")],
                request.to_string(),
            )
            .await;
            assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
        }
        let (_, _, body) = send(&app, "GET", "/health", &[auth], "").await;
        let status: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(status["jobs"][0]["status"], "current");
        assert_eq!(status["jobs"][0]["published"], true);
        assert!(status["jobs"][0]["attempt"].is_null());
        assert_eq!(
            send(
                &app,
                "POST",
                "/worker",
                &[auth, ("content-type", "application/json")],
                json!({"op":"retry","id":"recording"}).to_string()
            )
            .await
            .0,
            StatusCode::OK
        );
        let claimed = store.claim_job(&["fixture".into()]).unwrap().unwrap();
        assert_eq!(configure(&app, &store, json!({})).await, StatusCode::OK);
        assert!(store.jobs().unwrap().is_empty());
        assert!(store.file_page("transcript.md").unwrap().readonly);
        let (status, _, _) = send(
            &app,
            "POST",
            "/worker",
            &[worker, ("content-type", "application/json")],
            json!({"op":"heartbeat","id":"recording","attempt":claimed.job.state.attempt})
                .to_string(),
        )
        .await;
        assert!(!status.is_success());
        assert_ne!(
            send(&app, "POST", "/mcp/worker", &[auth], "{}").await.0,
            StatusCode::OK
        );
    }
    #[tokio::test]
    async fn shutdown_closes_idle_change_streams() {
        let (_dir, app, store) = fixture();
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/mcp/events")
                    .header("host", "localhost")
                    .header("authorization", "Bearer user")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let mut stream = response.into_body();
        assert!(stream.frame().await.is_some());
        store.stop_changes();
        assert!(
            tokio::time::timeout(std::time::Duration::from_secs(1), stream.frame())
                .await
                .unwrap()
                .is_none()
        );
    }

    #[tokio::test]
    async fn metadata_validators_are_independent_from_content_preconditions() {
        let (_dir, app, _) = fixture();
        let auth = ("authorization", "Bearer user");
        let accept = ("accept", "application/vnd.mdstore.page+json");
        let (_, raw, _) = send(&app, "GET", "/recording.md", &[auth], "").await;
        let (_, meta, bytes) = send(&app, "GET", "/recording.md", &[auth, accept], "").await;
        let value: Value = serde_json::from_slice(&bytes).unwrap();
        for unused in ["content", "metadata", "relations"] {
            assert!(value.get(unused).is_none());
        }
        let content_tag = raw["etag"].to_str().unwrap();
        let meta_tag = meta["etag"].to_str().unwrap();
        assert_ne!(meta_tag, content_tag);
        assert_eq!(
            content_tag,
            format!("\"{}\"", value["hash"].as_str().unwrap())
        );
        let weak = format!("W/{meta_tag}");
        let (status, headers, body) = send(
            &app,
            "GET",
            "/recording.md",
            &[auth, accept, ("if-none-match", &weak)],
            "",
        )
        .await;
        assert_eq!(status, StatusCode::NOT_MODIFIED);
        assert_eq!(headers["etag"], meta_tag);
        assert_eq!(headers["vary"], "Accept");
        assert!(body.is_empty());
        let (_, directory, _) = send(&app, "GET", "/", &[auth], "").await;
        assert_eq!(
            send(
                &app,
                "GET",
                "/",
                &[auth, ("if-none-match", directory["etag"].to_str().unwrap())],
                ""
            )
            .await
            .0,
            StatusCode::NOT_MODIFIED
        );
        assert_eq!(
            send(
                &app,
                "PUT",
                "/other.md",
                &[auth, ("if-none-match", "*")],
                "# Other\n"
            )
            .await
            .0,
            StatusCode::CREATED
        );
        let (status, next, bytes) = send(
            &app,
            "GET",
            "/recording.md",
            &[auth, accept, ("if-none-match", meta_tag)],
            "",
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_ne!(next["etag"], meta_tag);
        assert_eq!(
            serde_json::from_slice::<Value>(&bytes).unwrap()["hash"],
            value["hash"]
        );
        assert_eq!(
            send(
                &app,
                "PUT",
                "/recording.md",
                &[auth, ("if-match", meta_tag)],
                "# Recording\n"
            )
            .await
            .0,
            StatusCode::PRECONDITION_FAILED
        );
        assert_eq!(
            send(
                &app,
                "PUT",
                "/recording.md",
                &[auth, ("if-match", content_tag)],
                "# Recording\n"
            )
            .await
            .0,
            StatusCode::OK
        );
    }

    #[tokio::test]
    async fn single_file_and_batch_constraints_share_problem_details() {
        let (_dir, app, _) = fixture();
        let auth = ("authorization", "Bearer user");
        let a = "# A\n\n[B](b.md)\n";
        let b = "# B\n\n[A](a.md)\n";
        let (status, headers, bytes) =
            send(&app, "PUT", "/a.md", &[auth, ("if-none-match", "*")], a).await;
        assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
        assert_eq!(headers["content-type"], "application/problem+json");
        let problem: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(problem["status"], 422);
        assert!(!problem["findings"].as_array().unwrap().is_empty());
        let request = |edits: Value| {
            let mut operations = serde_json::Map::new();
            for edit in edits.as_array().unwrap() {
                let path = edit["path"].as_str().unwrap();
                let op = if edit["op"] == "delete_page" {
                    EditOperation::replace_file(
                        path.into(),
                        Some(edit["base"].as_str().unwrap()),
                        String::new(),
                    )
                } else {
                    EditOperation::replace_file(
                        path.into(),
                        None,
                        edit["content"].as_str().unwrap().into(),
                    )
                };
                let mut op = serde_json::to_value(op).unwrap();
                op.as_object_mut().unwrap().remove("path");
                operations.insert(format!("/{path}"), json!([op]));
            }
            json!({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"edit", "arguments":{"summary":"Atomic change", "edits": operations}}}).to_string()
        };
        let (_, _, bytes) = send(
            &app,
            "POST",
            "/mcp",
            &[auth],
            request(json!([{"op":"create_page", "path":"a.md", "content":a}])),
        )
        .await;
        assert!(!serde_json::from_slice::<Value>(&bytes).unwrap()["result"]["structuredContent"]["errors"].as_array().unwrap().is_empty());
        let (_, _, bytes) = send(&app, "POST", "/mcp", &[auth], request(json!([{"op":"create_page", "path":"a.md", "content":a}, {"op":"create_page", "path":"b.md", "content":b}]))).await;
        assert_eq!(
            serde_json::from_slice::<Value>(&bytes).unwrap()["result"]["isError"],
            false
        );
        let (_, headers, _) = send(&app, "GET", "/a.md", &[auth], "").await;
        assert_eq!(
            send(
                &app,
                "DELETE",
                "/a.md",
                &[auth, ("if-match", headers["etag"].to_str().unwrap())],
                ""
            )
            .await
            .0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
        let (_, _, bytes) = send(&app, "POST", "/mcp", &[auth], request(json!([{"op":"delete_page", "path":"a.md", "base":a}, {"op":"delete_page", "path":"b.md", "base":b}]))).await;
        assert_eq!(
            serde_json::from_slice::<Value>(&bytes).unwrap()["result"]["isError"],
            false
        );
        for path in ["/a.md", "/b.md"] {
            let (status, headers, bytes) = send(&app, "GET", path, &[auth], "").await;
            assert_eq!(status, StatusCode::NOT_FOUND);
            assert_eq!(headers["content-type"], "application/problem+json");
            assert_eq!(
                serde_json::from_slice::<Value>(&bytes).unwrap()["status"],
                404
            );
        }
    }

    #[tokio::test]
    async fn change_stream_sends_current_state_updates() {
        let (_dir, app, store) = fixture();
        let auth = ("authorization", "Bearer user");
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/mcp/events")
                    .header("host", "localhost")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.headers()["content-type"], "text/event-stream");
        let mut stream = response.into_body();
        async fn event(stream: &mut Body) -> String {
            let frame = tokio::time::timeout(std::time::Duration::from_secs(2), stream.frame())
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            String::from_utf8(frame.into_data().unwrap().to_vec()).unwrap()
        }
        let initial = event(&mut stream).await;
        assert!(initial.contains(&store.file_page("recording.md").unwrap().revision));
        store.jobs().unwrap();
        assert!(event(&mut stream).await.contains("\"jobs\":1"));
        store.jobs().unwrap(); // No notification for unchanged state.
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(50), stream.frame())
                .await
                .is_err()
        );
        send(
            &app,
            "PUT",
            "/new.md",
            &[auth, ("if-none-match", "*")],
            "# New\n",
        )
        .await;
        assert!(
            event(&mut stream)
                .await
                .contains(&store.file_page("new.md").unwrap().revision)
        );
    }

    #[tokio::test]
    async fn assets_replace_and_delete_conditionally_without_losing_history() {
        let (_dir, app, store) = fixture();
        let auth = ("authorization", "Bearer user");
        let (status, headers, _) = send(
            &app,
            "PUT",
            "/clip.wav",
            &[auth, ("if-none-match", "*")],
            "old",
        )
        .await;
        assert_eq!(status, StatusCode::CREATED);
        let old = store.asset("clip.wav").unwrap();
        let first = headers["etag"].to_str().unwrap();
        assert_eq!(
            send(
                &app,
                "PUT",
                "/clip.wav",
                &[auth, ("if-none-match", "*")],
                "new"
            )
            .await
            .0,
            StatusCode::PRECONDITION_FAILED
        );
        let (status, headers, _) = send(
            &app,
            "PUT",
            "/clip.wav",
            &[auth, ("if-match", first)],
            "new",
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let second = headers["etag"].to_str().unwrap();
        assert_ne!(first, second);
        assert_eq!(send(&app, "GET", "/clip.wav", &[auth], "").await.2, b"new");
        assert_eq!(
            send(
                &app,
                "DELETE",
                "/clip.wav",
                &[auth, ("if-match", first)],
                ""
            )
            .await
            .0,
            StatusCode::PRECONDITION_FAILED
        );
        assert_eq!(
            send(
                &app,
                "DELETE",
                "/clip.wav",
                &[auth, ("if-match", second)],
                ""
            )
            .await
            .0,
            StatusCode::NO_CONTENT
        );
        assert_eq!(
            send(&app, "GET", "/clip.wav", &[auth], "").await.0,
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            std::fs::read(store.object_path(&old.oid).unwrap()).unwrap(),
            b"old"
        );
    }
    #[tokio::test]
    async fn filesystem_writes_validate_and_use_etags() {
        let (_dir, app, store) = fixture();
        let auth = ("authorization", "Bearer user");
        assert_eq!(
            send(&app, "PUT", "/note.md", &[auth], "# Note\n").await.0,
            StatusCode::PRECONDITION_REQUIRED
        );
        assert_eq!(
            send(
                &app,
                "PUT",
                "/note.md",
                &[auth, ("if-none-match", "*")],
                "[Broken](missing.md)\n"
            )
            .await
            .0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
        assert_eq!(
            send(
                &app,
                "PUT",
                "/note.md",
                &[auth, ("if-none-match", "*")],
                "# Note\n"
            )
            .await
            .0,
            StatusCode::CREATED
        );
        let (status, headers, body) = send(&app, "GET", "/note.md", &[auth], "").await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, b"# Note\n");
        let hash = headers["etag"].to_str().unwrap();
        assert_eq!(send(&app, "HEAD", "/note.md", &[auth], "").await.2, b"");
        assert_eq!(
            send(
                &app,
                "GET",
                "/note.md",
                &[auth, ("if-none-match", hash)],
                ""
            )
            .await
            .0,
            StatusCode::NOT_MODIFIED
        );
        assert_eq!(
            send(
                &app,
                "PUT",
                "/note.md",
                &[auth, ("if-none-match", "*")],
                "# Clobber\n"
            )
            .await
            .0,
            StatusCode::PRECONDITION_FAILED
        );
        assert_eq!(
            send(
                &app,
                "PUT",
                "/note.md",
                &[auth, ("if-match", hash)],
                "# Updated\n"
            )
            .await
            .0,
            StatusCode::OK
        );
        assert_eq!(
            send(&app, "DELETE", "/note.md", &[auth, ("if-match", hash)], "")
                .await
                .0,
            StatusCode::PRECONDITION_FAILED
        );
        let hash = format!("\"{}\"", store.file_page("note.md").unwrap().hash.unwrap());
        assert_eq!(
            send(&app, "DELETE", "/note.md", &[auth, ("if-match", &hash)], "")
                .await
                .0,
            StatusCode::NO_CONTENT
        );
        assert_eq!(
            send(&app, "GET", "/note.md", &[auth], "").await.0,
            StatusCode::NOT_FOUND
        );
        assert_eq!(send(&app, "GET", "/", &[auth], "").await.0, StatusCode::OK);
        for path in [
            "health/file.md",
            "mcp/file.md",
            "webui/file.md",
            "worker/file.md",
        ] {
            assert!(validate_repo_path(path).is_err());
        }
    }
    #[tokio::test]
    async fn worker_transfers_are_confined_to_leased_paths() {
        let (_dir, app, store) = fixture();
        let user = ("authorization", "Bearer user");
        let worker = ("authorization", "Bearer worker");
        send(
            &app,
            "PUT",
            "/audio.wav",
            &[user, ("if-none-match", "*")],
            "audio",
        )
        .await;
        let definition = crate::config::DerivationConfig {
            source: "recording.md".into(),
            recipe: "fixture".into(),
            fields: vec!["hotwords".into()],
            inputs: vec!["audio.wav".into()],
            outputs: vec!["transcript.md".into(), "vectors.json".into()],
            reference_namespace: None,
        };
        let id = store.create_derivation(definition).unwrap();
        let assignment = store.claim_job(&["fixture".into()]).unwrap().unwrap();
        let attempt = assignment.job.state.attempt.unwrap();
        let url = format!("/audio.wav?job={id}&attempt={attempt}");
        assert_eq!(
            send(&app, "GET", "/", &[worker], "").await.0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            send(&app, "GET", "/audio.wav", &[worker], "").await.0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
        assert_eq!(send(&app, "GET", &url, &[worker], "").await.2, b"audio");
        assert_ne!(
            send(
                &app,
                "GET",
                &format!("/recording.md?job={id}&attempt={attempt}"),
                &[worker],
                ""
            )
            .await
            .0,
            StatusCode::OK
        );
        let (status, _, bytes) = send(
            &app,
            "PUT",
            &format!("/vectors.json?job={id}&attempt={attempt}"),
            &[worker],
            "{}",
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let asset = serde_json::from_slice(&bytes).unwrap();
        store
            .complete_job(
                &id,
                crate::store::artifacts::Completion {
                    attempt,
                    outputs: std::collections::BTreeMap::from([(
                        "transcript.md".into(),
                        "# Transcript\n".into(),
                    )]),
                    assets: std::collections::BTreeMap::from([("vectors.json".into(), asset)]),
                },
            )
            .unwrap();
        let hash = format!(
            "\"{}\"",
            store.file_page("transcript.md").unwrap().hash.unwrap()
        );
        assert_eq!(
            send(
                &app,
                "PUT",
                "/transcript.md",
                &[user, ("if-match", &hash)],
                "# Tampered\n"
            )
            .await
            .0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
        assert_eq!(
            send(&app, "GET", &url, &[worker], "").await.0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
    }
}
