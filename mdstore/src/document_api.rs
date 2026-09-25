use crate::{
    ApplyEditsRequest,
    mcp::{AppState, run_blocking},
    store::ValidationError,
};
use axum::{
    Json, Router,
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde_json::json;

pub(crate) fn api_router() -> Router<AppState> {
    Router::new()
        .route(
            "/documents",
            get(|State(state): State<AppState>| async move { Json(state.store.documents()) }),
        )
        .route("/validation-snapshot", get(|State(state): State<AppState>| async move { Json(state.store.validation_snapshot()) }))
        .route("/validate", post(validate))
}

async fn validate(
    State(state): State<AppState>,
    Json(request): Json<ApplyEditsRequest>,
) -> Response {
    match run_blocking(move || state.store.validate_edits(&request)).await {
        Ok(response) => {
            Json(json!({"valid": true, "paths": response.touched_paths, "restart_required": response.restart_required})).into_response()
        }
        Err(error) => {
            let findings = error.downcast_ref::<ValidationError>().map(|e| &e.findings);
            (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"error": error.to_string(), "findings": findings})),
            )
                .into_response()
        }
    }
}
