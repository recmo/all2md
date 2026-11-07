use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use serde::Deserialize;

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

#[derive(Debug, Clone, Deserialize)]
/// One provider reranking result.
pub struct RerankResult {
    /// Index into the submitted document array.
    pub index: usize,
    /// Provider relevance score.
    pub relevance_score: f64,
}

#[derive(Debug, thiserror::Error)]
#[error("document embeddings queued or unavailable")]
pub(crate) struct PendingEmbeddings;

pub(crate) const EMBED_RECIPE: &str = "text-embeddings-zembed-1-v1";
pub(crate) const RERANK_RECIPE: &str = "text-rerank-zerank-2-v1";
const EMBED_MODEL: &str = "zeroentropy/zembed-1-embedding";
const EMBED_REVISION: &str = "cf13c81f3274394053d166740294f7eea4586f7a";
const RERANK_MODEL: &str = "zeroentropy/zerank-2-reranker";
const RERANK_REVISION: &str = "5eae30d5ee3c6b2df2ef6d723bde45172d761c4c";

pub(crate) fn revision(recipe: &str) -> Result<&'static str> {
    match recipe {
        EMBED_RECIPE => Ok(EMBED_REVISION),
        RERANK_RECIPE => Ok(RERANK_REVISION),
        _ => bail!("unknown retrieval recipe: {recipe}"),
    }
}

#[derive(Debug, Clone)]
/// Posts all retrieval inference to mdstore's durable ML worker queue.
pub struct WorkerProvider {
    queue: crate::retrieval_jobs::Queue,
    config: ProviderConfig,
}

impl WorkerProvider {
    #[must_use]
    /// Creates a retrieval provider using the repository's private job directory.
    pub fn new(config: ProviderConfig, git_dir: &std::path::Path) -> Self {
        Self {
            queue: crate::retrieval_jobs::Queue::new(git_dir),
            config,
        }
    }

    async fn request(
        &self,
        recipe: &'static str,
        parameters: serde_json::Value,
        document: bool,
    ) -> Result<String> {
        let items = parameters["texts"].as_array().context("missing texts")?;
        anyhow::ensure!(
            !items.is_empty() && items.len() <= 256,
            "worker requests require 1..256 texts"
        );
        for item in items {
            let text = item["text"].as_str().context("missing text")?;
            anyhow::ensure!(
                !text.trim().is_empty() && text.chars().count() <= 32768,
                "worker texts require 1..32768 characters"
            );
        }
        let queue = self.queue.clone();
        let timeout = self.config.request_timeout_seconds;
        let id = tokio::task::spawn_blocking(move || {
            queue.submit(recipe, parameters, document, timeout)
        });
        let id = id.await??;
        // Later completions can be reused without holding up this search.
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(timeout.min(3));
        loop {
            let queue = self.queue.clone();
            let identity = id.clone();
            if let Some(result) =
                tokio::task::spawn_blocking(move || queue.result(&identity)).await??
            {
                return Ok(result);
            }
            if document {
                return Err(PendingEmbeddings.into());
            }
            if tokio::time::Instant::now() >= deadline {
                bail!("ML worker result pending: {id}");
            }
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        }
    }
}

fn texts(input: &[String]) -> serde_json::Value {
    serde_json::Value::Array(
        input
            .iter()
            .enumerate()
            .map(|(index, text)| serde_json::json!({"id": index.to_string(), "text": text}))
            .collect(),
    )
}

#[derive(Deserialize)]
struct Embeddings {
    records: Vec<Embedding>,
}
#[derive(Deserialize)]
struct Embedding {
    vector: Vec<f32>,
}
#[derive(Deserialize)]
struct Ranking {
    results: Vec<RerankResult>,
}

pub(crate) fn validate_result(
    recipe: &str,
    parameters: &serde_json::Value,
    output: &str,
) -> Result<()> {
    let body: serde_json::Value = serde_json::from_str(output).context("decode worker result")?;
    let items = parameters["texts"]
        .as_array()
        .context("missing input texts")?;
    if recipe == EMBED_RECIPE {
        anyhow::ensure!(
            body["model"] == EMBED_MODEL
                && body["revision"] == EMBED_REVISION
                && body["space"]["recipe"] == EMBED_RECIPE
                && body["space"]["namespace"] == "search"
                && body["space"]["dimensions"] == 2560,
            "wrong embedding model or space"
        );
        let embeddings: Embeddings = serde_json::from_value(body.clone())?;
        let vectors: Vec<_> = embeddings.records.into_iter().map(|r| r.vector).collect();
        validate_vectors(&vectors, items.len(), 2560)?;
        for (index, vector) in vectors.iter().enumerate() {
            anyhow::ensure!(
                body["records"][index]["id"] == items[index]["id"],
                "embedding IDs out of order"
            );
            anyhow::ensure!(
                body["records"][index]["metadata"]["text"] == items[index]["text"]
                    && body["records"][index]["metadata"]["input_type"] == parameters["input_type"],
                "wrong embedding input"
            );
            let norm: f64 = vector.iter().map(|v| f64::from(*v).powi(2)).sum();
            anyhow::ensure!(
                (0.99..=1.01).contains(&norm),
                "embedding must be normalized"
            );
        }
    } else {
        anyhow::ensure!(
            recipe == RERANK_RECIPE
                && body["model"] == RERANK_MODEL
                && body["revision"] == RERANK_REVISION
                && body["query"] == parameters["query"],
            "wrong reranking model or query"
        );
        let ranking: Ranking = serde_json::from_value(body.clone())?;
        let top_n = parameters["top_n"].as_u64().context("missing top_n")? as usize;
        validate_rerank_results(&ranking.results, items.len(), top_n)?;
        for (index, result) in ranking.results.iter().enumerate() {
            anyhow::ensure!(
                body["results"][index]["id"] == items[result.index]["id"],
                "wrong reranking ID"
            );
        }
    }
    Ok(())
}

#[async_trait]
impl RetrievalProvider for WorkerProvider {
    async fn embed(&self, input_type: InputType, input: &[String]) -> Result<Vec<Vec<f32>>> {
        if input.is_empty() {
            return Ok(Vec::new());
        }
        let document = matches!(input_type, InputType::Document);
        let result = self
            .request(
                EMBED_RECIPE,
                serde_json::json!({
                    "texts": texts(input),
                    "input_type": if document { "document" } else { "query" },
                    "namespace": "search"
                }),
                document,
            )
            .await?;
        let result: Embeddings = serde_json::from_str(&result)?;
        Ok(result
            .records
            .into_iter()
            .map(|record| record.vector)
            .collect())
    }

    async fn rerank(
        &self,
        query: &str,
        documents: &[String],
        top_n: usize,
    ) -> Result<Vec<RerankResult>> {
        if documents.is_empty() || top_n == 0 {
            return Ok(Vec::new());
        }
        anyhow::ensure!(
            !query.trim().is_empty() && query.chars().count() <= 32768,
            "worker queries require 1..32768 characters"
        );
        let result = self
            .request(
                RERANK_RECIPE,
                serde_json::json!({
                    "texts": texts(documents), "query": query,
                    "top_n": top_n.min(documents.len())
                }),
                false,
            )
            .await?;
        Ok(serde_json::from_str::<Ranking>(&result)?.results)
    }
    fn model(&self) -> &str {
        EMBED_MODEL
    }
    fn dimensions(&self) -> usize {
        2560
    }
    fn embedding_provider_identity(&self) -> String {
        format!("ml-worker:{EMBED_RECIPE}:{EMBED_REVISION}")
    }
}
