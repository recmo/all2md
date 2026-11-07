//! OpenAI wire format for external vectors, using the resident pull worker.
use super::*;
use crate::provider::EMBED_MODEL;
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(untagged)]
enum Input {
    One(String),
    Many(Vec<String>),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct EmbeddingsRequest {
    model: String,
    input: Input,
    dimensions: Option<usize>,
    encoding_format: Option<String>,
}

fn parameters(request: EmbeddingsRequest) -> anyhow::Result<(Value, bool)> {
    anyhow::ensure!(request.model == EMBED_MODEL, "unsupported embedding model");
    anyhow::ensure!(
        request.dimensions.is_none_or(|n| n == 2560),
        "dimensions must be 2560"
    );
    anyhow::ensure!(
        request
            .encoding_format
            .as_deref()
            .is_none_or(|v| v == "float"),
        "encoding_format must be float"
    );
    let inputs = match request.input {
        Input::One(text) => vec![text],
        Input::Many(texts) => texts,
    };
    anyhow::ensure!(
        !inputs.is_empty() && inputs.len() <= 4,
        "input requires 1..4 texts"
    );
    let mut role = None;
    let mut texts = Vec::new();
    let mut characters = 0;
    for (index, input) in inputs.into_iter().enumerate() {
        let (kind, text) = if let Some(text) = input.strip_prefix("query: ") {
            ("query", text)
        } else if let Some(text) = input.strip_prefix("document: ") {
            ("document", text)
        } else {
            anyhow::bail!("input requires the query: or document: prefix");
        };
        anyhow::ensure!(
            role.is_none_or(|previous| previous == kind),
            "mixed embedding roles"
        );
        role = Some(kind);
        characters += text.chars().count();
        anyhow::ensure!(
            !text.trim().is_empty() && characters <= 32768,
            "input requires nonempty texts and at most 32768 total characters"
        );
        texts.push(json!({"id":index.to_string(), "text":text}));
    }
    Ok((
        json!({"texts":texts,"input_type":role.unwrap(),"namespace":"search"}),
        role == Some("document"),
    ))
}

fn error(status: StatusCode, message: &str) -> Response {
    (
        status,
        Json(json!({"error":{"message":message,"type":"embeddings_error","code":status.as_u16()}})),
    )
        .into_response()
}

pub(super) async fn handle(
    State(state): State<AppState>,
    headers: HeaderMap,
    request: Result<Json<EmbeddingsRequest>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let authorized = state.embeddings_token.as_deref().is_some_and(|token| {
        state.worker_token.as_deref() != Some(token)
            && headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.strip_prefix("Bearer "))
                == Some(token)
    });
    if !authorized {
        return error(StatusCode::UNAUTHORIZED, "invalid embeddings credential");
    }
    let Ok(Json(request)) = request else {
        return error(StatusCode::BAD_REQUEST, "invalid embeddings request");
    };
    let (parameters, document) = match parameters(request) {
        Ok(value) => value,
        Err(err) => return error(StatusCode::BAD_REQUEST, &err.to_string()),
    };
    let queue = state.store.retrieval_queue();
    let submitting = queue.clone();
    let Ok(id) = run_blocking(move || submitting.submit_external(parameters, document)).await
    else {
        return error(StatusCode::SERVICE_UNAVAILABLE, "ML worker unavailable");
    };
    // Background jobs survive disconnects/timeouts; retries use their content hash.
    // Query waits fit within msgvault's interactive request budget.
    let deadline =
        tokio::time::Instant::now() + std::time::Duration::from_secs(if document { 25 } else { 3 });
    loop {
        let reading = queue.clone();
        let identity = id.clone();
        match run_blocking(move || reading.result(&identity)).await {
            Ok(Some(result)) => {
                let body: Value = match serde_json::from_str(&result) {
                    Ok(body) => body,
                    Err(_) => {
                        return error(StatusCode::INTERNAL_SERVER_ERROR, "invalid worker result");
                    }
                };
                let data: Vec<_> = body["records"].as_array().unwrap().iter().enumerate()
                    .map(|(index, record)| json!({"object":"embedding","index":index,"embedding":record["vector"]})).collect();
                return Json(json!({"object":"list","model":EMBED_MODEL,"data":data}))
                    .into_response();
            }
            Ok(None) => {}
            Err(_) => return error(StatusCode::SERVICE_UNAVAILABLE, "embedding job unavailable"),
        }
        if tokio::time::Instant::now() >= deadline {
            let mut response = error(
                StatusCode::SERVICE_UNAVAILABLE,
                "embeddings pending; retry the same input",
            );
            response
                .headers_mut()
                .insert(header::RETRY_AFTER, HeaderValue::from_static("5"));
            return response;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::provider::EMBED_RECIPE;
    use axum::body::Body;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    async fn request(app: &Router, path: &str, token: &str, body: Value) -> (StatusCode, Value) {
        let response = app
            .clone()
            .oneshot(
                HttpRequest::builder()
                    .method("POST")
                    .uri(path)
                    .header("authorization", format!("Bearer {token}"))
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = response.status();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }

    async fn claim(app: &Router) -> Vec<Value> {
        request(
            app,
            "/worker",
            "worker",
            json!({"op":"claim", "recipes":{EMBED_RECIPE:4}}),
        )
        .await
        .1
        .as_array()
        .unwrap()
        .clone()
    }

    async fn complete(app: &Router, assignment: &Value) -> StatusCode {
        let parameters = &assignment["parameters"]["ml"];
        let records: Vec<_> = parameters["texts"].as_array().unwrap().iter().enumerate().map(|(index, text)| {
            let mut vector = vec![0.0; 2560];
            vector[index] = 1.0;
            json!({"id":text["id"],"vector":vector,"metadata":{"text":text["text"],"input_type":parameters["input_type"]}})
        }).collect();
        let result = json!({"model":EMBED_MODEL,"revision":crate::provider::revision(EMBED_RECIPE).unwrap(),
            "space":{"recipe":EMBED_RECIPE,"namespace":"search","dimensions":2560},"records":records,
            "provenance":{"recipe":EMBED_RECIPE,"fingerprint":assignment["job"]["id"]}});
        request(app,"/worker","worker",json!({"op":"complete","id":assignment["job"]["id"],
            "attempt":assignment["job"]["attempt"],"outputs":{crate::retrieval_jobs::OUTPUT:result.to_string()}})).await.0
    }

    fn app(store: Arc<Store>) -> Router {
        application(AppState {
            store,
            worker_token: Some("worker".into()),
            embeddings_token: Some("client".into()),
        })
    }

    fn payload(input: &Value) -> Value {
        json!({"input":input,"model":EMBED_MODEL,"dimensions":2560,"encoding_format":"float"})
    }

    #[tokio::test]
    async fn external_embeddings_survive_retries_restart_and_keep_roles_order_and_priority() {
        let (dir, store) = crate::store::artifacts::tests::fixture();
        let app = app(store.clone());
        let body = payload(&json!([
            "document: First sample",
            "document: Second sample"
        ]));
        for token in ["", "worker", "wrong"] {
            assert_eq!(
                request(&app, "/v1/embeddings", token, body.clone()).await.0,
                StatusCode::UNAUTHORIZED
            );
        }
        assert_eq!(
            request(
                &app,
                "/worker",
                "client",
                json!({"op":"claim","recipes":{EMBED_RECIPE:4}})
            )
            .await
            .0,
            StatusCode::FORBIDDEN
        );
        for input in [
            json!(["unprefixed"]),
            json!(["query: q", "document: d"]),
            json!([]),
        ] {
            assert_eq!(
                request(&app, "/v1/embeddings", "client", payload(&input))
                    .await
                    .0,
                StatusCode::BAD_REQUEST
            );
        }
        for (field, value) in [
            ("model", json!("wrong")),
            ("dimensions", json!(768)),
            ("encoding_format", json!("base64")),
        ] {
            let mut invalid = body.clone();
            invalid[field] = value;
            let (status, error) = request(&app, "/v1/embeddings", "client", invalid).await;
            assert_eq!(status, StatusCode::BAD_REQUEST);
            assert!(error["error"]["message"].is_string());
        }
        let waiting = app.clone();
        let document_body = body.clone();
        let http = tokio::spawn(async move {
            request(&waiting, "/v1/embeddings", "client", document_body).await
        });
        let queue_dir = dir.path().join(".git/mdstore/retrieval");
        loop {
            if std::fs::read_dir(&queue_dir).is_ok_and(|mut entries| {
                entries.any(|entry| {
                    entry
                        .unwrap()
                        .path()
                        .extension()
                        .is_some_and(|v| v == "json")
                })
            }) {
                break;
            }
            tokio::task::yield_now().await;
        }
        // A dropped HTTP connection keeps the same durable work for a retry.
        http.abort();
        let (parameters, _) = parameters(serde_json::from_value(body.clone()).unwrap()).unwrap();
        let id = store
            .retrieval_queue()
            .submit_external(parameters.clone(), true)
            .unwrap();
        assert_eq!(
            store
                .retrieval_queue()
                .submit_external(parameters, true)
                .unwrap(),
            id
        );
        // Offline queries fail immediately, without converting single documents to queries.
        assert_eq!(
            request(
                &app,
                "/v1/embeddings",
                "client",
                payload(&json!("query: Offline"))
            )
            .await
            .0,
            StatusCode::SERVICE_UNAVAILABLE
        );
        let background = claim(&app).await;
        assert_eq!(background.len(), 1);
        assert_eq!(background[0]["parameters"]["ml"]["input_type"], "document");
        // Simulate a worker disconnect and daemon restart while its lease expires.
        let path = queue_dir.join(format!("{id}.json"));
        let mut job: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        job["expires"] = json!(0);
        std::fs::write(&path, serde_json::to_vec(&job).unwrap()).unwrap();
        drop(app);
        drop(store);
        let store = Store::open(dir.path()).unwrap();
        let app = self::app(store.clone());
        let querying = app.clone();
        let query_body = payload(&json!(["query: First query", "query: Second query"]));
        let retry_body = query_body.clone();
        let query = tokio::spawn(async move {
            request(&querying, "/v1/embeddings", "client", query_body).await
        });
        let query_id = loop {
            let jobs = store.jobs().unwrap();
            if let Some(job) = jobs.iter().find(|job| job.state.id != id) {
                break job.state.id.clone();
            }
            tokio::task::yield_now().await;
        };
        let first = claim(&app).await;
        assert_eq!(first.len(), 1);
        assert_eq!(first[0]["job"]["id"], query_id);
        assert_eq!(first[0]["parameters"]["ml"]["input_type"], "query");
        assert_eq!(
            first[0]["parameters"]["ml"]["texts"][0]["text"],
            "First query"
        );
        assert_eq!(complete(&app, &first[0]).await, StatusCode::OK);
        let (status, response) = query.await.unwrap();
        assert_eq!(status, StatusCode::OK);
        assert_eq!(response["model"], EMBED_MODEL);
        for index in 0..2 {
            assert_eq!(response["data"][index]["index"], index);
            assert_eq!(
                response["data"][index]["embedding"]
                    .as_array()
                    .unwrap()
                    .len(),
                2560
            );
            assert_eq!(response["data"][index]["embedding"][index], 1.0);
        }
        assert_eq!(
            request(&app, "/v1/embeddings", "client", retry_body)
                .await
                .1,
            response
        );
        let returned = claim(&app).await;
        assert_eq!(returned.len(), 1);
        assert_eq!(returned[0]["job"]["id"], id);
        assert_ne!(
            returned[0]["job"]["attempt"],
            background[0]["job"]["attempt"]
        );
        assert_eq!(
            complete(&app, &background[0]).await,
            StatusCode::UNPROCESSABLE_ENTITY
        );
        assert_eq!(complete(&app, &returned[0]).await, StatusCode::OK);
        assert_eq!(
            request(&app, "/v1/embeddings", "client", body).await.0,
            StatusCode::OK
        );
        assert_eq!(store.status().unwrap().vectors_total, 0);
        assert!(!dir.path().join("retrieval").exists());
        // A recent worker disappearing mid-request bounds the query wait and
        // concurrent/repeated HTTP requests reuse its pending lease.
        let body = payload(&json!("query: Lost worker"));
        let (a, b) = tokio::time::timeout(std::time::Duration::from_secs(5), async {
            tokio::join!(
                request(&app, "/v1/embeddings", "client", body.clone()),
                request(&app, "/v1/embeddings", "client", body)
            )
        })
        .await
        .unwrap();
        assert_eq!(a.0, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(b.0, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(claim(&app).await.len(), 1);
        // Background claims across clients stay within a four-text budget.
        for count in [4, 2, 1] {
            let inputs: Vec<_> = (0..count)
                .map(|n| format!("document: budget {count} {n}"))
                .collect();
            let (parameters, _) =
                self::parameters(serde_json::from_value(payload(&json!(inputs))).unwrap()).unwrap();
            store
                .retrieval_queue()
                .submit_external(parameters, true)
                .unwrap();
        }
        store.retrieval_queue().clear_documents().unwrap();
        let mut total = 0;
        loop {
            let assignments = claim(&app).await;
            if assignments.is_empty() {
                break;
            }
            let count: usize = assignments
                .iter()
                .map(|assignment| {
                    assignment["parameters"]["ml"]["texts"]
                        .as_array()
                        .unwrap()
                        .len()
                })
                .sum();
            assert!(count <= 4);
            total += count;
            for assignment in assignments {
                assert_eq!(complete(&app, &assignment).await, StatusCode::OK);
            }
        }
        assert_eq!(total, 7);
    }
}
