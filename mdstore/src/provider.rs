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

    async fn post(&self, endpoint: &str, body: &impl Serialize) -> Result<reqwest::Response> {
        let mut attempt = 0;
        loop {
            let response = self
                .client
                .post(self.endpoint(endpoint))
                .bearer_auth(self.key()?)
                .json(body)
                .send()
                .await
                .with_context(|| format!("call ZeroEntropy {endpoint}"))?;
            let status = response.status();
            if attempt < 2 && (status == StatusCode::TOO_MANY_REQUESTS || status.is_server_error())
            {
                tokio::time::sleep(std::time::Duration::from_millis(100 * (1_u64 << attempt)))
                    .await;
                attempt += 1;
                continue;
            }
            if status != StatusCode::OK {
                bail!(
                    "ZeroEntropy {endpoint} returned {status}: {}",
                    response.text().await.unwrap_or_default()
                );
            }
            return Ok(response);
        }
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
        let response = self
            .post(
                "models/embed",
                &EmbedRequest {
                    model: &self.config.embedding_model,
                    input_type: input_type.as_str(),
                    input,
                    dimensions: self.config.dimensions,
                    encoding_format: "float",
                },
            )
            .await?;
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
        let response = self
            .post(
                "models/rerank",
                &RerankRequest {
                    model: &self.config.rerank_model,
                    query,
                    documents,
                    top_n,
                },
            )
            .await?;
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
