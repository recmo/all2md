//! Worker-only lease operations, independent of MCP transport.
use super::*;
use crate::store::artifacts::Completion;

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum WorkerRequest {
    Search {
        id: String,
        attempt: String,
        query: control::VectorQuery,
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
    Json(request): Json<WorkerRequest>,
) -> Response {
    match run_blocking(move || match request {
        WorkerRequest::Search { id, attempt, query } => {
            control::search(&state.store, query, Some((id, attempt)))
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
