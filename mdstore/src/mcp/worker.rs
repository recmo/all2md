//! Worker-only lease operations, independent of MCP transport.
use super::*;
use crate::store::artifacts::Completion;

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum WorkerRequest {
    Retry {
        id: String,
    },
    Search {
        id: String,
        attempt: String,
        query: VectorQuery,
    },
    Claim {
        recipes: Vec<String>,
    },
    Heartbeat {
        id: String,
        attempt: String,
        #[serde(default)]
        progress: Value,
        failure: Option<String>,
    },
    Complete {
        id: String,
        attempt: String,
        outputs: std::collections::BTreeMap<String, String>,
        #[serde(default)]
        assets: std::collections::BTreeMap<String, crate::store::artifacts::Asset>,
    },
}
pub(super) async fn handle(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<WorkerRequest>,
) -> Response {
    let is_worker = state.worker_token.as_deref().is_some_and(|token| {
        headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            == Some(token)
    });
    if !is_worker && !matches!(request, WorkerRequest::Retry { .. }) {
        return StatusCode::FORBIDDEN.into_response();
    }
    match run_blocking(move || match request {
        WorkerRequest::Retry { id } => { state.store.retry_job(&id)?; Ok(Value::Null) },
        WorkerRequest::Search { id, attempt, query } => {
            Ok(json!({"results": state.store.search_reference_vectors(query.space, query.vector, query.limit, query.filter, (id, attempt))?}))
        }
        WorkerRequest::Claim { recipes } => {
            Ok(serde_json::to_value(state.store.claim_job(&recipes)?)?)
        }
        WorkerRequest::Heartbeat {
            id,
            attempt,
            progress,
            failure,
        } => {
            state.store.heartbeat(&id, &attempt, progress, failure)?;
            Ok(Value::Null)
        }
        WorkerRequest::Complete {
            id,
            attempt,
            outputs,
            assets,
        } => {
            state.store.complete_job(
                &id,
                Completion {
                    attempt,
                    outputs,
                    assets,
                },
            )?;
            Ok(Value::Null)
        }
    })
    .await
    {
        Ok(value) => Json(value).into_response(),
        Err(error) => problem::Problem::from_error(&error).into_response(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct VectorQuery {
    space: crate::vectors::EmbeddingSpace,
    vector: Vec<f32>,
    limit: usize,
    #[serde(default)]
    filter: std::collections::BTreeMap<String, Value>,
}
