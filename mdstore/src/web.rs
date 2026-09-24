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

include!(concat!(env!("OUT_DIR"), "/web_assets.rs"));

pub(crate) fn assets() -> Router<AppState> {
    let mut router = Router::new();
    for &(path, mime, content) in WEB_ASSETS {
        router = router.route(
            path,
            get(move || async move {
                (
                    [
                        ("content-type", mime),
                        ("x-content-type-options", "nosniff"),
                        (
                            "content-security-policy",
                            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
                        ),
                        ("cache-control", "no-cache"),
                    ],
                    content,
                )
                    .into_response()
            }),
        );
    }
    let (_, mime, content) = WEB_ASSETS
        .iter()
        .find(|(path, _, _)| *path == "/")
        .expect("SPA shell");
    router.route(
        "/settings",
        get(move || async move {
            (
                [
                    ("content-type", *mime),
                    (
                        "content-security-policy",
                        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
                    ),
                ],
                *content,
            )
                .into_response()
        }),
    )
}

pub(crate) fn api_router() -> Router<AppState> {
    Router::new()
        .route(
            "/ui/documents",
            get(|State(state): State<AppState>| async move { Json(state.store.documents()) }),
        )
        .route("/ui/validation-snapshot", get(|State(state): State<AppState>| async move { Json(state.store.validation_snapshot()) }))
        .route("/ui/validate", post(validate))
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
