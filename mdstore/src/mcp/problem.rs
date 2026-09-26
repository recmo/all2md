//! The same structured failure is carried by HTTP and MCP tool responses.
use super::*;

pub(super) fn error(error: anyhow::Error) -> Response {
    Problem::from_error(&error).into_response()
}

#[derive(serde::Serialize)]
pub(super) struct Problem {
    #[serde(rename = "type")]
    kind: &'static str,
    title: String,
    pub status: u16,
    pub detail: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    findings: Vec<crate::markdown::Finding>,
}
impl Problem {
    pub(super) fn new(status: StatusCode, detail: impl Into<String>) -> Self {
        Self {
            kind: "about:blank",
            title: status.canonical_reason().unwrap_or("Request failed").into(),
            status: status.as_u16(),
            detail: detail.into(),
            findings: vec![],
        }
    }
    pub(super) fn from_error(error: &anyhow::Error) -> Self {
        if let Some(validation) = error.downcast_ref::<ValidationError>() {
            let mut problem = Self::new(StatusCode::UNPROCESSABLE_ENTITY, "Validation failed");
            problem.kind = "urn:mdstore:problem:validation";
            problem.findings = validation.findings.clone();
            return problem;
        }
        if error.is::<crate::store::artifacts::PreconditionFailed>() {
            return Self::new(
                StatusCode::PRECONDITION_FAILED,
                "File changed or already exists",
            );
        }
        Self::new(StatusCode::UNPROCESSABLE_ENTITY, error.to_string())
    }
}
impl IntoResponse for Problem {
    fn into_response(self) -> Response {
        let status = StatusCode::from_u16(self.status).expect("valid problem status");
        (
            status,
            [
                ("content-type", "application/problem+json"),
                ("cache-control", "no-store"),
            ],
            Json(self),
        )
            .into_response()
    }
}

// Also normalize router/extractor failures (404, 405, malformed JSON, auth),
// retaining headers such as Allow, WWW-Authenticate and Content-Range.
pub(super) async fn normalize(request: Request, next: Next) -> Response {
    let head = request.method() == axum::http::Method::HEAD;
    let response = next.run(request).await;
    if !(response.status().is_client_error() || response.status().is_server_error())
        || response
            .headers()
            .get("content-type")
            .is_some_and(|v| v == "application/problem+json")
    {
        return response;
    }
    let (parts, body) = response.into_parts();
    let bytes = axum::body::to_bytes(body, 64 * 1024)
        .await
        .unwrap_or_default();
    let detail = serde_json::from_slice::<Value>(&bytes)
        .ok()
        .and_then(|v| v.get("error").and_then(Value::as_str).map(str::to_owned))
        .unwrap_or_else(|| String::from_utf8_lossy(&bytes).into_owned());
    let mut result = Problem::new(
        parts.status,
        if detail.is_empty() {
            parts
                .status
                .canonical_reason()
                .unwrap_or("Request failed")
                .into()
        } else {
            detail
        },
    )
    .into_response();
    for (key, value) in &parts.headers {
        if key != "content-type" && key != "content-length" && key != "cache-control" {
            result.headers_mut().insert(key.clone(), value.clone());
        }
    }
    if head {
        *result.body_mut() = axum::body::Body::empty();
    }
    result
}
