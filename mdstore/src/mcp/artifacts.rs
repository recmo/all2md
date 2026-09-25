use super::*;
use crate::store::artifacts::{Completion, Derivation};
use anyhow::ensure;
use axum::{
    body::Body,
    extract::{DefaultBodyLimit, Path, Query},
};
use futures_util::StreamExt;
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt};

pub(super) fn routes() -> Router<AppState> {
    Router::new()
        .route("/artifacts", get(manifest))
        .route("/lfs/objects/batch", post(lfs_batch))
        .route("/lfs/objects/{oid}", get(lfs_download).put(lfs_upload))
        .route("/assets", get(download).put(upload))
        .route("/assets/ticket", post(media_ticket))
        .route("/playback/{ticket}", get(playback))
        .route("/derivations", post(create))
        .route(
            "/derivations/{id}",
            axum::routing::delete(remove).patch(update_inputs),
        )
        .route("/jobs/{id}/retry", post(retry))
        .route("/jobs", get(jobs))
        .route("/vectors/search", post(vector_search))
        .route("/worker/vectors/search", post(worker_vector_search))
        .route("/worker/claim", post(claim))
        .route("/worker/inputs", get(worker_download))
        .route("/worker/outputs", axum::routing::put(worker_upload))
        .route("/worker/jobs/{id}/heartbeat", post(heartbeat))
        .route("/worker/jobs/{id}/complete", post(complete))
        .layer(DefaultBodyLimit::max(16 * 1024 * 1024))
}

fn timestamp() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
pub(super) fn playback_path(state: &AppState, ticket: &str) -> Option<String> {
    state
        .media_tickets
        .lock()
        .get(ticket)
        .filter(|(_, expires)| *expires > timestamp())
        .map(|(path, _)| path.clone())
}
async fn media_ticket(State(state): State<AppState>, Query(query): Query<AssetQuery>) -> Response {
    reply((|| -> Result<Value> {
        state.store.asset(&query.path)?;
        let ticket = crate::store::artifacts::secret()?;
        let mut tickets = state.media_tickets.lock();
        tickets.retain(|_, (_, expires)| *expires > timestamp());
        ensure!(tickets.len() < 1000, "too many playback sessions");
        tickets.insert(ticket.clone(), (query.path, timestamp() + 3600));
        Ok(json!({"url": format!("/playback/{ticket}"), "expires_in": 3600}))
    })())
}
async fn playback(
    State(state): State<AppState>,
    Path(ticket): Path<String>,
    headers: HeaderMap,
) -> Response {
    let Some(path) = playback_path(&state, &ticket) else {
        return StatusCode::UNAUTHORIZED.into_response();
    };
    match state.store.asset(&path) {
        Ok(asset) => {
            let mut response = serve_asset(&state.store, asset, &headers)
                .await
                .unwrap_or_else(error);
            let mime = match path.rsplit('.').next().unwrap_or("") {
                "mp4" => "video/mp4",
                "webm" => "video/webm",
                "m4a" => "audio/mp4",
                "mp3" => "audio/mpeg",
                "wav" => "audio/wav",
                "flac" => "audio/flac",
                "ogg" => "audio/ogg",
                _ => "application/octet-stream",
            };
            response
                .headers_mut()
                .insert("content-type", mime.parse().unwrap());
            response
        }
        Err(e) => error(e),
    }
}

#[derive(Deserialize)]
struct LfsBatch {
    operation: String,
    objects: Vec<crate::store::artifacts::Asset>,
}
async fn lfs_batch(State(state): State<AppState>, Json(request): Json<LfsBatch>) -> Response {
    let result = (|| -> Result<Value> {
        ensure!(
            matches!(request.operation.as_str(), "download" | "upload"),
            "unsupported LFS operation"
        );
        ensure!(request.objects.len() <= 1000, "too many LFS objects");
        let base = std::env::var("MDSTORE_PUBLIC_URL")
            .context("set MDSTORE_PUBLIC_URL to enable Git LFS transfers")?;
        ensure!(
            base.starts_with("http://") || base.starts_with("https://"),
            "invalid public URL"
        );
        let mut objects = Vec::new();
        for asset in request.objects {
            let exists = state
                .store
                .object_path(&asset.oid)?
                .metadata()
                .is_ok_and(|m| m.len() == asset.size);
            let mut item = json!({"oid": asset.oid, "size": asset.size});
            if request.operation == "download" && !exists {
                item["error"] = json!({"code": 404, "message": "object unavailable"});
            } else if request.operation == "download" || !exists {
                let mut action = json!({"href": format!("{}/lfs/objects/{}", base.trim_end_matches('/'), asset.oid)});
                if let Some(token) = &state.bearer_token {
                    action["header"] = json!({"Authorization": format!("Bearer {token}")});
                }
                item["actions"] = json!({request.operation.clone(): action});
            }
            objects.push(item);
        }
        Ok(json!({"transfer": "basic", "objects": objects}))
    })();
    let mut response = reply(result);
    response.headers_mut().insert(
        "content-type",
        "application/vnd.git-lfs+json".parse().unwrap(),
    );
    response
}
async fn lfs_download(
    State(state): State<AppState>,
    Path(oid): Path<String>,
    headers: HeaderMap,
) -> Response {
    let result = async {
        let size = tokio::fs::metadata(state.store.object_path(&oid)?)
            .await?
            .len();
        serve_asset(
            &state.store,
            crate::store::artifacts::Asset { oid, size },
            &headers,
        )
        .await
    }
    .await;
    result.unwrap_or_else(error)
}
async fn receive_object(
    store: Arc<Store>,
    body: Body,
    limit: u64,
    expected: Option<String>,
) -> Result<crate::store::artifacts::Asset> {
    use sha2::Digest;
    if let Some(oid) = &expected {
        store.object_path(oid)?;
    }
    let temporary = store.object_temporary()?;
    let mut writer = tokio::fs::File::from_std(temporary.reopen()?);
    let mut stream = body.into_data_stream();
    let mut size = 0_u64;
    let mut hash = sha2::Sha256::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        size += chunk.len() as u64;
        ensure!(size <= limit, "asset exceeds upload limit");
        hash.update(&chunk);
        writer.write_all(&chunk).await?;
    }
    if let Some(oid) = expected {
        ensure!(
            format!("{:x}", hash.finalize()) == oid,
            "LFS digest mismatch"
        );
    }
    writer.sync_all().await?;
    drop(writer);
    run_blocking(move || store.store_object(temporary)).await
}

async fn lfs_upload(
    State(state): State<AppState>,
    Path(oid): Path<String>,
    body: Body,
) -> Response {
    reply(
        receive_object(state.store, body, 16 * 1024 * 1024 * 1024, Some(oid))
            .await
            .map(|_| ()),
    )
}

fn error(error: anyhow::Error) -> Response {
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(json!({"error": format!("{error:#}")})),
    )
        .into_response()
}

fn reply<T: serde::Serialize>(result: Result<T>) -> Response {
    match result {
        Ok(value) => Json(value).into_response(),
        Err(e) => error(e),
    }
}

async fn manifest(State(state): State<AppState>) -> Response {
    reply(run_blocking(move || state.store.artifact_manifest()).await)
}
async fn jobs(State(state): State<AppState>) -> Response {
    reply(run_blocking(move || state.store.jobs()).await)
}
async fn create(State(state): State<AppState>, Json(definition): Json<Derivation>) -> Response {
    reply(run_blocking(move || state.store.create_derivation(definition)).await)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct InputUpdate {
    inputs: Vec<String>,
}
async fn update_inputs(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(update): Json<InputUpdate>,
) -> Response {
    reply(run_blocking(move || state.store.update_derivation_inputs(&id, update.inputs)).await)
}
async fn remove(State(state): State<AppState>, Path(id): Path<String>) -> Response {
    reply(run_blocking(move || state.store.remove_derivation(&id)).await)
}
async fn retry(State(state): State<AppState>, Path(id): Path<String>) -> Response {
    reply(run_blocking(move || state.store.retry_job(&id)).await)
}

#[derive(Deserialize)]
struct AssetQuery {
    path: String,
}

async fn upload(
    State(state): State<AppState>,
    Query(query): Query<AssetQuery>,
    body: Body,
) -> Response {
    let result = async {
        crate::config::validate_repo_path(&query.path)?;
        let asset =
            receive_object(state.store.clone(), body, 16 * 1024 * 1024 * 1024, None).await?;
        run_blocking(move || {
            state.store.attach_asset(query.path, asset.clone())?;
            Ok(asset)
        })
        .await
    }
    .await;
    reply(result)
}

async fn download(
    State(state): State<AppState>,
    Query(query): Query<AssetQuery>,
    headers: HeaderMap,
) -> Response {
    let store = state.store.clone();
    match run_blocking(move || store.asset(&query.path)).await {
        Ok(asset) => serve_asset(&state.store, asset, &headers)
            .await
            .unwrap_or_else(error),
        Err(e) => error(e),
    }
}

async fn serve_asset(
    store: &Store,
    asset: crate::store::artifacts::Asset,
    headers: &HeaderMap,
) -> Result<Response> {
    let mut file = tokio::fs::File::open(store.object_path(&asset.oid)?).await?;
    ensure!(
        file.metadata().await?.len() == asset.size,
        "asset object size mismatch"
    );
    let mut start = 0;
    let mut end = asset.size;
    let mut status = StatusCode::OK;
    if let Some(range) = headers.get("range") {
        let parsed = range
            .to_str()
            .ok()
            .and_then(|r| r.strip_prefix("bytes="))
            .and_then(|r| r.split_once('-'))
            .and_then(|(a, b)| {
                if a.is_empty() {
                    let suffix: u64 = b.parse().ok()?;
                    Some((asset.size.saturating_sub(suffix), asset.size))
                } else {
                    Some((
                        a.parse().ok()?,
                        if b.is_empty() {
                            asset.size
                        } else {
                            b.parse::<u64>().ok()?.checked_add(1)?.min(asset.size)
                        },
                    ))
                }
            });
        let Some((a, b)) = parsed.filter(|(a, b)| a < b && *a < asset.size) else {
            return Ok((
                StatusCode::RANGE_NOT_SATISFIABLE,
                [("content-range", format!("bytes */{}", asset.size))],
            )
                .into_response());
        };
        start = a;
        end = b;
        status = StatusCode::PARTIAL_CONTENT;
    }
    file.seek(std::io::SeekFrom::Start(start)).await?;
    let body = Body::from_stream(tokio_util::io::ReaderStream::new(file.take(end - start)));
    let mut builder = Response::builder()
        .status(status)
        .header("accept-ranges", "bytes")
        .header("content-length", (end - start).to_string())
        .header("content-type", "application/octet-stream")
        .header("cache-control", "private, no-store")
        .header("etag", format!("\"{}\"", asset.oid));
    if status == StatusCode::PARTIAL_CONTENT {
        builder = builder.header(
            "content-range",
            format!("bytes {start}-{}/{}", end - 1, asset.size),
        );
    }
    Ok(builder.body(body)?)
}

#[derive(Deserialize)]
struct Claim {
    recipes: Vec<String>,
}
async fn claim(State(state): State<AppState>, Json(request): Json<Claim>) -> Response {
    reply(run_blocking(move || state.store.claim_job(&request.recipes)).await)
}
#[derive(Deserialize)]
struct Lease {
    job: String,
    attempt: String,
    path: String,
}
async fn worker_download(
    State(state): State<AppState>,
    Query(query): Query<Lease>,
    headers: HeaderMap,
) -> Response {
    let store = state.store.clone();
    match run_blocking(move || store.leased_asset(&query.job, &query.attempt, &query.path)).await {
        Ok(asset) => serve_asset(&state.store, asset, &headers)
            .await
            .unwrap_or_else(error),
        Err(e) => error(e),
    }
}
async fn worker_upload(
    State(state): State<AppState>,
    Query(query): Query<Lease>,
    body: Body,
) -> Response {
    let result = async {
        state
            .store
            .check_output_lease(&query.job, &query.attempt, &query.path)?;
        let asset = receive_object(state.store.clone(), body, 64 * 1024 * 1024, None).await?;
        state
            .store
            .check_output_lease(&query.job, &query.attempt, &query.path)?;
        Ok(asset)
    }
    .await;
    reply(result)
}

#[derive(Deserialize)]
struct Heartbeat {
    attempt: String,
    #[serde(default)]
    progress: Value,
    failure: Option<String>,
}
async fn heartbeat(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(request): Json<Heartbeat>,
) -> Response {
    reply(
        run_blocking(move || {
            state
                .store
                .heartbeat(&id, &request.attempt, request.progress, request.failure)
        })
        .await,
    )
}
async fn complete(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(request): Json<Completion>,
) -> Response {
    reply(run_blocking(move || state.store.complete_job(&id, request)).await)
}

#[derive(Deserialize)]
struct VectorQuery {
    space: crate::vectors::EmbeddingSpace,
    vector: Vec<f32>,
    limit: usize,
    #[serde(default)]
    filter: std::collections::BTreeMap<String, Value>,
    job: Option<String>,
    attempt: Option<String>,
}
async fn vector_search(
    State(state): State<AppState>,
    Json(request): Json<VectorQuery>,
) -> Response {
    reply(
        run_blocking(move || {
            state.store.search_artifact_vectors(
                request.space,
                request.vector,
                request.limit,
                request.filter,
                None,
            )
        })
        .await,
    )
}
async fn worker_vector_search(
    State(state): State<AppState>,
    Json(request): Json<VectorQuery>,
) -> Response {
    let (Some(job), Some(attempt)) = (request.job, request.attempt) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    reply(
        run_blocking(move || {
            state.store.search_artifact_vectors(
                request.space,
                request.vector,
                request.limit,
                request.filter,
                Some((job, attempt)),
            )
        })
        .await,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    async fn send(
        app: &Router,
        method: &str,
        uri: &str,
        token: &str,
        body: Body,
        range: Option<&str>,
    ) -> (StatusCode, HeaderMap, Vec<u8>) {
        let mut request = Request::builder()
            .method(method)
            .uri(uri)
            .header("host", "localhost")
            .header("authorization", format!("Bearer {token}"))
            .header("content-type", "application/json");
        if let Some(range) = range {
            request = request.header("range", range);
        }
        let response = app
            .clone()
            .oneshot(request.body(body).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let headers = response.headers().clone();
        let body = response
            .into_body()
            .collect()
            .await
            .unwrap()
            .to_bytes()
            .to_vec();
        (status, headers, body)
    }
    fn json_body(value: Value) -> Body {
        Body::from(value.to_string())
    }

    #[tokio::test]
    async fn assets_playback_and_leased_publication_share_store_authority() {
        let (_dir, store) = crate::store::artifacts::tests::fixture();
        let state = AppState {
            store: store.clone(),
            bearer_token: Some("user".into()),
            worker_token: Some("worker".into()),
            media_tickets: Arc::new(parking_lot::Mutex::new(std::collections::BTreeMap::new())),
        };
        let app = routes()
            .layer(middleware::from_fn_with_state(state.clone(), authorize))
            .with_state(state.clone());
        let (status, _, _) = send(
            &app,
            "PUT",
            "/assets?path=audio.m4a",
            "worker",
            Body::from("0123456789"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        let (status, _, body) = send(
            &app,
            "PUT",
            "/assets?path=audio.m4a",
            "user",
            Body::from("0123456789"),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
        let (status, headers, body) = send(
            &app,
            "GET",
            "/assets?path=audio.m4a",
            "user",
            Body::empty(),
            Some("bytes=2-5"),
        )
        .await;
        assert_eq!(status, StatusCode::PARTIAL_CONTENT);
        assert_eq!(body, b"2345");
        assert_eq!(headers["content-range"], "bytes 2-5/10");
        assert_eq!(
            send(
                &app,
                "GET",
                "/assets?path=audio.m4a",
                "user",
                Body::empty(),
                Some("bytes=90-")
            )
            .await
            .0,
            StatusCode::RANGE_NOT_SATISFIABLE
        );
        let (_, _, body) = send(
            &app,
            "POST",
            "/assets/ticket?path=audio.m4a",
            "user",
            Body::empty(),
            None,
        )
        .await;
        let ticket: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(
            send(
                &app,
                "GET",
                ticket["url"].as_str().unwrap(),
                "",
                Body::empty(),
                Some("bytes=-3")
            )
            .await
            .2,
            b"789"
        );
        let definition = json!({"source":"recording.md","recipe":"fixture","fields":["hotwords"],"inputs":["audio.m4a"],"outputs":["transcript.md"]});
        let (status, _, body) = send(
            &app,
            "POST",
            "/derivations",
            "user",
            json_body(definition),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
        let id: String = serde_json::from_slice(&body).unwrap();
        assert_eq!(
            send(
                &app,
                "POST",
                "/worker/claim",
                "user",
                json_body(json!({"recipes":["fixture"]})),
                None
            )
            .await
            .0,
            StatusCode::UNAUTHORIZED
        );
        let (_, _, body) = send(
            &app,
            "POST",
            "/worker/claim",
            "worker",
            json_body(json!({"recipes":["fixture"]})),
            None,
        )
        .await;
        let assignment: Value = serde_json::from_slice(&body).unwrap();
        let attempt = assignment["job"]["attempt"].as_str().unwrap();
        assert_eq!(
            send(
                &app,
                "GET",
                &format!("/worker/inputs?job={id}&attempt={attempt}&path=audio.m4a"),
                "worker",
                Body::empty(),
                None
            )
            .await
            .2,
            b"0123456789"
        );
        let (status, _, body) = send(
            &app,
            "POST",
            &format!("/worker/jobs/{id}/complete"),
            "worker",
            json_body(json!({"attempt":attempt,"outputs":{"transcript.md":"# Derived\n"}})),
            None,
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&body));
        assert!(store.get_page("transcript.md", None).unwrap().readonly);
    }
}
