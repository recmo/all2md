//! Repository resources use their own URLs; text writes reuse the validated edit path.
use super::*;
use crate::{
    EditOperation,
    config::{is_config_resource_path, validate_repo_path},
};
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
    match run_blocking(move || state.store.get_directory("/")).await {
        Ok(value) => Json(value).into_response(),
        Err(e) => artifacts::error(e),
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
fn media_type(path: &str) -> &'static str {
    match path
        .rsplit('.')
        .next()
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "md" => "text/markdown; charset=utf-8",
        "yaml" | "yml" => "application/yaml",
        "toml" => "application/toml",
        "json" => "application/json",
        "pdf" => "application/pdf",
        "mp4" | "m4v" => "video/mp4",
        "webm" => "video/webm",
        "mov" => "video/quicktime",
        "m4a" => "audio/mp4",
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "flac" => "audio/flac",
        "ogg" | "opus" => "audio/ogg",
        "aac" => "audio/aac",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "avif" => "image/avif",
        _ => "application/octet-stream",
    }
}
fn etag(headers: &HeaderMap, hash: &str) -> bool {
    headers
        .get("if-none-match")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| {
            v == "*"
                || v.split(',')
                    .any(|part| part.trim() == format!("\"{hash}\""))
        })
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
                return Ok(Json(state.store.get_directory(&path)?).into_response());
            }
            validate_repo_path(&path)?;
            state.store.asset(&path).ok()
        };
        let metadata = !is_worker
            && headers
                .get("accept")
                .is_some_and(|value| value == "application/vnd.mdstore.page+json");
        let mut response = if metadata {
            let page = state.store.get_page(&path, None)?;
            let hash = page.hash.clone();
            let mut response = Json(page).into_response();
            if let Some(hash) = hash {
                response
                    .headers_mut()
                    .insert("etag", format!("\"{hash}\"").parse()?);
            }
            response
        } else if let Some(asset) = asset {
            if etag(&headers, &asset.oid) {
                return Ok(StatusCode::NOT_MODIFIED.into_response());
            }
            artifacts::serve_asset(&state.store, asset, &headers).await?
        } else {
            let page = match state.store.get_page(&path, None) {
                Ok(page) if page.exists => page,
                _ => return Ok(StatusCode::NOT_FOUND.into_response()),
            };
            let hash = page.hash.context("missing file hash")?;
            if etag(&headers, &hash) {
                return Ok(StatusCode::NOT_MODIFIED.into_response());
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
                .insert("content-type", media_type(&path).parse()?);
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
    result.unwrap_or_else(file_error)
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
fn matches(current: Option<&str>, expected: Option<&str>) -> Result<()> {
    if current != expected {
        return Err(crate::store::artifacts::PreconditionFailed.into());
    }
    Ok(())
}
fn file_error(error: anyhow::Error) -> Response {
    if error.is::<crate::store::artifacts::PreconditionFailed>() {
        return (
            StatusCode::PRECONDITION_FAILED,
            Json(json!({"error": "file changed or already exists"})),
        )
            .into_response();
    }
    if let Some(validation) = error.downcast_ref::<ValidationError>() {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"error": "validation failed", "findings": validation.findings})),
        )
            .into_response();
    }
    artifacts::error(error)
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
                artifacts::receive_object(state.store.clone(), body, 64 * 1024 * 1024, None)
                    .await?;
            state.store.check_output_lease(job, attempt, &path)?;
            Ok::<_, anyhow::Error>(Json(asset).into_response())
        }
        .await;
        return result.unwrap_or_else(file_error);
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
                let page = state.store.get_page(&path, None)?;
                matches(page.hash.as_deref(), expected.as_deref())?;
                let edit = if page.exists {
                    EditOperation::ReplacePage {
                        path: path.clone(),
                        base: page.text.unwrap_or_default(),
                        content: text,
                    }
                } else {
                    EditOperation::CreatePage {
                        path: path.clone(),
                        content: text,
                    }
                };
                state.store.apply_edits(&ApplyEditsRequest {
                    edit_summary: format!("Write {path}"),
                    edits: vec![edit],
                })?;
                let page = state.store.get_page(&path, None)?;
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
                artifacts::receive_object(state.store.clone(), body, 16 * 1024 * 1024 * 1024, None)
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
    result.unwrap_or_else(file_error)
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
            let page = state.store.get_page(&path, None)?;
            matches(page.hash.as_deref(), Some(&expected))?;
            state.store.apply_edits(&ApplyEditsRequest {
                edit_summary: format!("Delete {path}"),
                edits: vec![EditOperation::DeletePage {
                    path,
                    base: page.text.unwrap_or_default(),
                }],
            })?;
        }
        Ok(StatusCode::NO_CONTENT.into_response())
    })
    .await;
    result.unwrap_or_else(file_error)
}

#[cfg(test)]
mod tests {
    use super::*;
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
            bearer_token: Some("user".into()),
            worker_token: Some("worker".into()),
            sessions: Arc::default(),
        });
        (dir, app, store)
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
    async fn sessions_authenticate_media_and_reject_csrf_and_logout() {
        let (_dir, app, _) = fixture();
        let auth = ("authorization", "Bearer user");
        let (status, _, _) = send(
            &app,
            "PUT",
            "/audio.wav",
            &[auth, ("if-none-match", "*")],
            "0123456789",
        )
        .await;
        assert_eq!(status, StatusCode::CREATED);
        assert_eq!(
            send(&app, "GET", "/audio.wav", &[], "").await.0,
            StatusCode::UNAUTHORIZED
        );
        let (status, headers, _) = send(&app, "POST", "/mcp/session", &[auth], "").await;
        assert_eq!(status, StatusCode::OK);
        let set_cookie = headers["set-cookie"].to_str().unwrap();
        assert!(set_cookie.contains("HttpOnly; SameSite=Strict"));
        assert!(!set_cookie.contains("user"));
        let cookie = ("cookie", set_cookie.split(';').next().unwrap());
        let (status, headers, bytes) = send(
            &app,
            "GET",
            "/audio.wav",
            &[cookie, ("range", "bytes=2-5")],
            "",
        )
        .await;
        assert_eq!(status, StatusCode::PARTIAL_CONTENT);
        assert_eq!(bytes, b"2345");
        assert_eq!(headers["content-type"], "audio/wav");
        assert_eq!(
            send(
                &app,
                "PUT",
                "/other.wav",
                &[cookie, ("if-none-match", "*")],
                "audio"
            )
            .await
            .0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            send(
                &app,
                "PUT",
                "/other.wav",
                &[
                    cookie,
                    ("if-none-match", "*"),
                    ("origin", "https://evil.example")
                ],
                "audio"
            )
            .await
            .0,
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            send(
                &app,
                "PUT",
                "/other.wav",
                &[
                    cookie,
                    ("if-none-match", "*"),
                    ("origin", "http://localhost")
                ],
                "audio"
            )
            .await
            .0,
            StatusCode::CREATED
        );
        assert_eq!(
            send(
                &app,
                "POST",
                "/mcp/session",
                &[cookie, ("origin", "http://localhost")],
                ""
            )
            .await
            .0,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            send(
                &app,
                "DELETE",
                "/mcp/session",
                &[cookie, ("origin", "http://localhost")],
                ""
            )
            .await
            .0,
            StatusCode::OK
        );
        assert_eq!(
            send(&app, "GET", "/audio.wav", &[cookie], "").await.0,
            StatusCode::UNAUTHORIZED
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
        let hash = format!(
            "\"{}\"",
            store.get_page("note.md", None).unwrap().hash.unwrap()
        );
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
        for path in ["health/file.md", "mcp/file.md", "webui/file.md"] {
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
        let definition = crate::store::artifacts::Derivation {
            source: "recording.md".into(),
            recipe: "fixture".into(),
            fields: vec!["hotwords".into()],
            inputs: vec!["audio.wav".into()],
            outputs: vec!["transcript.md".into(), "vectors.json".into()],
            reference_namespace: None,
            publication: None,
        };
        let id = store.create_derivation(definition).unwrap();
        let assignment = store.claim_job(&["fixture".into()]).unwrap().unwrap();
        let attempt = assignment.job.attempt.unwrap();
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
            store.get_page("transcript.md", None).unwrap().hash.unwrap()
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
