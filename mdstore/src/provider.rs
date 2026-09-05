use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};

use crate::config::ProviderConfig;

#[async_trait]
/// Supplies query/document embeddings and final reranking.
pub trait RetrievalProvider: Send + Sync {
    /// Embeds inputs in the provider mode appropriate to their role.
    async fn embed(&self, input_type: InputType, input: &[String]) -> Result<Vec<Vec<f32>>>;
    /// Reranks candidate documents for the original query.
    async fn rerank(
        &self,
        query: &str,
        documents: &[String],
        top_n: usize,
    ) -> Result<Vec<RerankResult>>;
    /// Returns the embedding model identifier.
    fn model(&self) -> &str;
    /// Returns the embedding vector dimensions.
    fn dimensions(&self) -> usize;
    /// Returns a credential-free embedding backend identity.
    fn embedding_provider_identity(&self) -> String;
}

#[derive(Debug, Clone, Copy)]
/// Provider input mode for embedding requests.
pub enum InputType {
    /// Search query or caller-supplied query variant.
    Query,
    /// Persisted document chunk.
    Document,
}

pub(crate) fn validate_vectors(
    vectors: &[Vec<f32>],
    expected_count: usize,
    dimensions: usize,
) -> Result<()> {
    if vectors.len() != expected_count
        || vectors.iter().any(|vector| {
            vector.len() != dimensions || vector.iter().any(|value| !value.is_finite())
        })
    {
        bail!("embedding response has unexpected count, dimensions, or non-finite values");
    }
    Ok(())
}

pub(crate) fn validate_rerank_results(
    results: &[RerankResult],
    document_count: usize,
    top_n: usize,
) -> Result<()> {
    let expected = document_count.min(top_n);
    if results.len() != expected {
        bail!(
            "rerank response has {} results; expected {expected}",
            results.len()
        );
    }
    let mut seen = vec![false; document_count];
    for result in results {
        if result.index >= document_count {
            bail!(
                "rerank response index {} is outside {document_count} documents",
                result.index
            );
        }
        if !result.relevance_score.is_finite() {
            bail!("rerank response contains a non-finite score");
        }
        if std::mem::replace(&mut seen[result.index], true) {
            bail!("rerank response contains duplicate index {}", result.index);
        }
    }
    Ok(())
}

impl InputType {
    fn as_str(self) -> &'static str {
        match self {
            Self::Query => "query",
            Self::Document => "document",
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
/// One provider reranking result.
pub struct RerankResult {
    /// Index into the submitted document array.
    pub index: usize,
    /// Provider relevance score.
    pub relevance_score: f64,
}

#[derive(Clone)]
/// HTTP client for the ZeroEntropy embedding and reranking APIs.
pub struct ZeroEntropyProvider {
    client: reqwest::Client,
    config: ProviderConfig,
    api_key: Option<String>,
}

impl std::fmt::Debug for ZeroEntropyProvider {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ZeroEntropyProvider")
            .field("config", &self.config)
            .field("api_key_configured", &self.api_key.is_some())
            .finish_non_exhaustive()
    }
}

impl ZeroEntropyProvider {
    #[must_use]
    /// Creates a bounded HTTP client and snapshots the configured API key.
    pub fn new(config: ProviderConfig) -> Self {
        let api_key = std::env::var(&config.api_key_env)
            .ok()
            .filter(|value| !value.is_empty());
        Self {
            client: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(
                    config.request_timeout_seconds,
                ))
                .build()
                .expect("build ZeroEntropy HTTP client"),
            config,
            api_key,
        }
    }

    fn endpoint(&self, path: &str) -> String {
        format!("{}/{path}", self.config.base_url.trim_end_matches('/'))
    }

    fn key(&self) -> Result<&str> {
        self.api_key
            .as_deref()
            .context(format!("{} is not set", self.config.api_key_env))
    }

    async fn retry_delay(attempt: usize) {
        tokio::time::sleep(std::time::Duration::from_millis(100 * (1_u64 << attempt))).await;
    }
}

#[derive(Serialize)]
struct EmbedRequest<'a> {
    model: &'a str,
    input_type: &'a str,
    input: &'a [String],
    dimensions: usize,
    encoding_format: &'static str,
}

#[derive(Deserialize)]
struct EmbedResponse {
    results: Vec<EmbedResult>,
}

#[derive(Deserialize)]
struct EmbedResult {
    embedding: Vec<f32>,
}

#[derive(Serialize)]
struct RerankRequest<'a> {
    model: &'a str,
    query: &'a str,
    documents: &'a [String],
    top_n: usize,
}

#[derive(Deserialize)]
struct RerankResponse {
    results: Vec<RerankResult>,
}

#[async_trait]
impl RetrievalProvider for ZeroEntropyProvider {
    async fn embed(&self, input_type: InputType, input: &[String]) -> Result<Vec<Vec<f32>>> {
        if input.is_empty() {
            return Ok(Vec::new());
        }
        let mut response = None;
        for attempt in 0..3 {
            let current = self
                .client
                .post(self.endpoint("models/embed"))
                .bearer_auth(self.key()?)
                .json(&EmbedRequest {
                    model: &self.config.embedding_model,
                    input_type: input_type.as_str(),
                    input,
                    dimensions: self.config.dimensions,
                    encoding_format: "float",
                })
                .send()
                .await
                .context("call ZeroEntropy embedding endpoint")?;
            let retryable = current.status() == StatusCode::TOO_MANY_REQUESTS
                || current.status().is_server_error();
            response = Some(current);
            if !retryable || attempt == 2 {
                break;
            }
            Self::retry_delay(attempt).await;
        }
        let response = response.context("embedding request was not attempted")?;
        let status = response.status();
        if status != StatusCode::OK {
            bail!(
                "ZeroEntropy embedding endpoint returned {status}: {}",
                response.text().await.unwrap_or_default()
            );
        }
        let body: EmbedResponse = response.json().await.context("decode embedding response")?;
        let vectors: Vec<Vec<f32>> = body
            .results
            .into_iter()
            .map(|result| result.embedding)
            .collect();
        validate_vectors(&vectors, input.len(), self.config.dimensions)?;
        Ok(vectors)
    }

    async fn rerank(
        &self,
        query: &str,
        documents: &[String],
        top_n: usize,
    ) -> Result<Vec<RerankResult>> {
        if documents.is_empty() {
            return Ok(Vec::new());
        }
        let mut response = None;
        for attempt in 0..3 {
            let current = self
                .client
                .post(self.endpoint("models/rerank"))
                .bearer_auth(self.key()?)
                .json(&RerankRequest {
                    model: &self.config.rerank_model,
                    query,
                    documents,
                    top_n,
                })
                .send()
                .await
                .context("call ZeroEntropy rerank endpoint")?;
            let retryable = current.status() == StatusCode::TOO_MANY_REQUESTS
                || current.status().is_server_error();
            response = Some(current);
            if !retryable || attempt == 2 {
                break;
            }
            Self::retry_delay(attempt).await;
        }
        let response = response.context("rerank request was not attempted")?;
        let status = response.status();
        if status != StatusCode::OK {
            bail!(
                "ZeroEntropy rerank endpoint returned {status}: {}",
                response.text().await.unwrap_or_default()
            );
        }
        Ok(response
            .json::<RerankResponse>()
            .await
            .context("decode rerank response")?
            .results)
    }

    fn model(&self) -> &str {
        &self.config.embedding_model
    }

    fn dimensions(&self) -> usize {
        self.config.dimensions
    }

    fn embedding_provider_identity(&self) -> String {
        let endpoint = reqwest::Url::parse(&self.config.base_url).map_or_else(
            |_| self.config.base_url.trim_end_matches('/').to_owned(),
            |mut endpoint| {
                let _ = endpoint.set_username("");
                let _ = endpoint.set_password(None);
                endpoint.set_query(None);
                endpoint.set_fragment(None);
                endpoint.to_string().trim_end_matches('/').to_owned()
            },
        );
        format!("zeroentropy:{endpoint}")
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    use std::time::{Duration, Instant};

    use axum::{
        Json, Router,
        extract::State,
        http::{HeaderMap, StatusCode},
        routing::post,
    };
    use serde_json::{Value, json};

    use super::*;

    #[tokio::test]
    async fn zeroentropy_wire_contract_and_retry() {
        let attempts = Arc::new(AtomicUsize::new(0));
        let app = Router::new()
            .route("/v1/models/embed", post(embed))
            .route("/v1/models/rerank", post(rerank))
            .with_state(attempts.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let provider = ZeroEntropyProvider {
            client: reqwest::Client::new(),
            config: ProviderConfig {
                base_url: format!("http://{address}/v1"),
                api_key_env: "UNUSED".into(),
                embedding_model: "zembed-1".into(),
                rerank_model: "zerank-2".into(),
                dimensions: 2,
                batch_size: Some(2),
                request_timeout_seconds: 30,
            },
            api_key: Some("test-token".into()),
        };
        let vectors = provider
            .embed(InputType::Document, &["one".into(), "two".into()])
            .await
            .unwrap();
        assert_eq!(attempts.load(Ordering::SeqCst), 2);
        assert_eq!(vectors, [vec![1.0, 0.0], vec![0.0, 1.0]]);
        let ranked = provider
            .rerank("two", &["one".into(), "two".into()], 1)
            .await
            .unwrap();
        assert_eq!(ranked[0].index, 1);
    }

    #[tokio::test]
    async fn zeroentropy_requests_have_a_finite_timeout() {
        let app = Router::new().route(
            "/v1/models/embed",
            post(|| async {
                tokio::time::sleep(Duration::from_secs(5)).await;
                Json(json!({"results": [{"embedding": [1.0, 0.0]}]}))
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let provider = ZeroEntropyProvider::new(ProviderConfig {
            base_url: format!("http://{address}/v1"),
            api_key_env: "PATH".into(),
            embedding_model: "zembed-1".into(),
            rerank_model: "zerank-2".into(),
            dimensions: 2,
            batch_size: Some(2),
            request_timeout_seconds: 1,
        });
        let start = Instant::now();
        assert!(
            provider
                .embed(InputType::Document, &["one".into()])
                .await
                .is_err()
        );
        assert!(start.elapsed() < Duration::from_secs(3));
    }

    #[test]
    fn provider_identity_includes_endpoint_but_not_credentials() {
        let provider = ZeroEntropyProvider {
            client: reqwest::Client::new(),
            config: ProviderConfig {
                base_url: "https://user:password@example.com/v1/?token=secret#fragment".into(),
                api_key_env: "SECRET_ENV_NAME".into(),
                embedding_model: "zembed-1".into(),
                rerank_model: "zerank-2".into(),
                dimensions: 2,
                batch_size: Some(2),
                request_timeout_seconds: 30,
            },
            api_key: Some("api-secret".into()),
        };
        let identity = provider.embedding_provider_identity();
        assert_eq!(identity, "zeroentropy:https://example.com/v1");
        assert!(!identity.contains("secret"));
        assert!(!identity.contains("password"));
    }

    #[test]
    fn rejects_non_finite_embedding_values() {
        assert!(validate_vectors(&[vec![1.0, f32::NAN]], 1, 2).is_err());
        assert!(validate_vectors(&[vec![f32::INFINITY, 0.0]], 1, 2).is_err());
        assert!(validate_vectors(&[vec![1.0, 0.0]], 1, 2).is_ok());
    }

    #[test]
    fn rerank_results_are_complete_unique_finite_and_in_range() {
        let result = |index, relevance_score| RerankResult {
            index,
            relevance_score,
        };
        let valid = [result(1, 0.9), result(0, 0.4)];
        assert!(validate_rerank_results(&valid, 3, 2).is_ok());
        for invalid in [
            vec![result(1, 0.9)],
            vec![result(1, 0.9), result(1, 0.4)],
            vec![result(3, 0.9), result(0, 0.4)],
            vec![result(1, f64::NAN), result(0, 0.4)],
        ] {
            assert!(validate_rerank_results(&invalid, 3, 2).is_err());
        }
    }

    async fn embed(
        State(attempts): State<Arc<AtomicUsize>>,
        headers: HeaderMap,
        Json(body): Json<Value>,
    ) -> (StatusCode, Json<Value>) {
        assert_eq!(headers["authorization"], "Bearer test-token");
        assert_eq!(body["model"], "zembed-1");
        assert_eq!(body["input_type"], "document");
        assert_eq!(body["dimensions"], 2);
        if attempts.fetch_add(1, Ordering::SeqCst) == 0 {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({"error": "retry"})),
            );
        }
        (
            StatusCode::OK,
            Json(json!({
                "results": [
                    {"embedding": [1.0, 0.0]},
                    {"embedding": [0.0, 1.0]}
                ]
            })),
        )
    }

    async fn rerank(headers: HeaderMap, Json(body): Json<Value>) -> Json<Value> {
        assert_eq!(headers["authorization"], "Bearer test-token");
        assert_eq!(body["model"], "zerank-2");
        assert_eq!(body["query"], "two");
        assert_eq!(body["top_n"], 1);
        Json(json!({"results": [{"index": 1, "relevance_score": 0.9}]}))
    }
}
