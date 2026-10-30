use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use anyhow::Context;
use serde::Serialize;

use crate::{
    chunk::Chunk,
    config::Config,
    markdown::{Edge, ParsedPage},
    provider::{InputType, RetrievalProvider, validate_rerank_results, validate_vectors},
};

#[derive(Debug, Clone)]
/// Immutable exact, vector, and graph retrieval index.
pub(crate) struct SearchIndex {
    /// Indexed chunks in deterministic repository order.
    pub chunks: Vec<IndexedChunk>,
    document_frequency: HashMap<String, usize>,
    average_length: f64,
    graph: HashMap<String, BTreeSet<String>>,
    first_chunk: HashMap<String, usize>,
}

#[derive(Debug, Clone)]
/// Searchable fields and optional vector for one chunk.
pub(crate) struct IndexedChunk {
    /// Repository-relative page path.
    pub path: String,
    /// Source excerpt and embedding context.
    pub chunk: Chunk,
    /// Combined text used by exact retrieval.
    pub search_text: String,
    /// Exact-search term frequencies.
    pub terms: HashMap<String, usize>,
    /// Valid sidecar vector, when available.
    pub vector: Option<Vec<f32>>,
}

/// Prebuilt chunks and optional vectors keyed by page path.
pub(crate) type PageChunks = HashMap<String, Vec<(Chunk, Option<Vec<f32>>)>>;

#[derive(Debug, Clone, Serialize)]
/// Ranked source excerpts and retrieval warnings.
pub struct SearchResponse {
    /// Git commit used for retrieval and excerpt rendering.
    pub commit: String,
    /// Results in final rank order.
    pub results: Vec<SearchResult>,
    /// Retrieval components that failed or returned invalid data.
    pub degraded: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
/// One ranked source excerpt, ready to read or edit.
pub struct SearchResult {
    /// Repository-rooted path with an inclusive line range.
    pub path: String,
    /// Source lines with neighbor-based hashline anchors.
    pub excerpt: String,
}

#[derive(Debug)]
pub(crate) struct RankedSearchResponse {
    pub results: Vec<RankedSearchResult>,
    pub degraded: Vec<String>,
}

#[derive(Debug)]
pub(crate) struct RankedSearchResult {
    pub path: String,
    pub start_line: usize,
    pub end_line: usize,
}

impl SearchIndex {
    #[must_use]
    /// Builds a deterministic index from validated pages and sidecar vectors.
    pub(crate) fn build(
        parsed: &HashMap<String, ParsedPage>,
        edges: &[Edge],
        page_chunks: PageChunks,
    ) -> Self {
        let mut chunks = Vec::new();
        let mut first_chunk = HashMap::new();
        let mut page_chunks: Vec<_> = page_chunks.into_iter().collect();
        page_chunks.sort_by(|left, right| left.0.cmp(&right.0));
        for (path, mut values) in page_chunks {
            let Some(page) = parsed.get(&path) else {
                continue;
            };
            values.sort_by(|left, right| {
                left.0
                    .start_line
                    .cmp(&right.0.start_line)
                    .then_with(|| left.0.end_line.cmp(&right.0.end_line))
                    .then_with(|| left.0.text.cmp(&right.0.text))
            });
            let metadata = page.metadata.clone();
            for (chunk, vector) in values {
                let heading = chunk.heading.join(" ");
                let text = &chunk.text;
                let search_text = format!("{path} {metadata} {heading} {text}");
                let terms = term_counts(&search_text);
                first_chunk.entry(path.clone()).or_insert(chunks.len());
                chunks.push(IndexedChunk {
                    path: path.clone(),
                    chunk,
                    search_text,
                    terms,
                    vector,
                });
            }
        }
        let mut document_frequency = HashMap::new();
        for chunk in &chunks {
            for term in chunk.terms.keys() {
                *document_frequency.entry(term.clone()).or_insert(0) += 1;
            }
        }
        let average_length = if chunks.is_empty() {
            0.0
        } else {
            chunks
                .iter()
                .map(|chunk| chunk.terms.values().sum::<usize>())
                .sum::<usize>() as f64
                / chunks.len() as f64
        };
        let mut graph: HashMap<String, BTreeSet<String>> = HashMap::new();
        for edge in edges {
            graph
                .entry(edge.source.clone())
                .or_default()
                .insert(edge.target.clone());
            graph
                .entry(edge.target.clone())
                .or_default()
                .insert(edge.source.clone());
        }
        Self {
            chunks,
            document_frequency,
            average_length,
            graph,
            first_chunk,
        }
    }

    /// Runs exact and vector retrieval, graph expansion, fusion, and reranking.
    pub(crate) async fn search(
        self: &Arc<Self>,
        config: &Config,
        provider: &dyn RetrievalProvider,
        query: &str,
        variants: &[String],
    ) -> anyhow::Result<RankedSearchResponse> {
        let formulations: Vec<String> = std::iter::once(query.to_owned())
            .chain(
                variants
                    .iter()
                    .filter(|value| !value.trim().is_empty())
                    .cloned(),
            )
            .collect();
        let exact_index = Arc::clone(self);
        let exact_config = config.clone();
        let exact_formulations = formulations.clone();
        let mut scores = retrieval_task(move || {
            let mut scores: HashMap<usize, f64> = HashMap::new();
            for formulation in &exact_formulations {
                for (rank, (index, _)) in exact_index
                    .bm25(formulation)
                    .into_iter()
                    .take(exact_config.search.candidates)
                    .enumerate()
                {
                    *scores.entry(index).or_default() +=
                        1.0 / (exact_config.search.rrf_k + rank as f64 + 1.0);
                }
            }
            scores
        })
        .await?;
        let mut degraded = Vec::new();
        let query_vectors = match provider.embed(InputType::Query, &formulations).await {
            Ok(query_vectors) => {
                match validate_vectors(
                    &query_vectors,
                    formulations.len(),
                    config.provider.dimensions,
                ) {
                    Ok(()) => Some(query_vectors),
                    Err(error) => {
                        degraded.push(format!("embedding: {error}"));
                        None
                    }
                }
            }
            Err(error) => {
                degraded.push(format!("embedding: {error}"));
                None
            }
        };
        let vector_index = Arc::clone(self);
        let vector_config = config.clone();
        let (mut ranked, documents) = retrieval_task(move || {
            if let Some(query_vectors) = query_vectors {
                for vector in &query_vectors {
                    for (rank, (index, _)) in vector_index
                        .vector(vector)
                        .into_iter()
                        .take(vector_config.search.candidates)
                        .enumerate()
                    {
                        *scores.entry(index).or_default() +=
                            1.0 / (vector_config.search.rrf_k + rank as f64 + 1.0);
                    }
                }
            }
            vector_index.add_graph_signals(&vector_config, &mut scores);
            let mut ranked: Vec<(usize, f64)> = scores.into_iter().collect();
            ranked.sort_by(|a, b| {
                b.1.total_cmp(&a.1)
                    .then_with(|| vector_index.chunk_order(a.0, b.0))
            });
            ranked.truncate(vector_config.search.candidates);
            let documents: Vec<String> = ranked
                .iter()
                .map(|(index, _)| vector_index.chunks[*index].chunk.text.clone())
                .collect();
            (ranked, documents)
        })
        .await?;
        let mut rerank_scores = HashMap::new();
        match provider
            .rerank(query, &documents, config.search.limit)
            .await
        {
            Ok(results) => {
                match validate_rerank_results(&results, documents.len(), config.search.limit) {
                    Ok(()) => {
                        for result in results {
                            let chunk_index = ranked[result.index].0;
                            rerank_scores.insert(chunk_index, result.relevance_score);
                        }
                        ranked.sort_by(|a, b| {
                            rerank_scores
                                .get(&b.0)
                                .copied()
                                .unwrap_or(f64::NEG_INFINITY)
                                .total_cmp(
                                    &rerank_scores
                                        .get(&a.0)
                                        .copied()
                                        .unwrap_or(f64::NEG_INFINITY),
                                )
                                .then_with(|| b.1.total_cmp(&a.1))
                                .then_with(|| self.chunk_order(a.0, b.0))
                        });
                    }
                    Err(error) => degraded.push(format!("rerank: {error}")),
                }
            }
            Err(error) => degraded.push(format!("rerank: {error}")),
        }
        ranked.truncate(config.search.limit);
        let results = ranked
            .into_iter()
            .map(|(index, _)| {
                let chunk = &self.chunks[index];
                RankedSearchResult {
                    path: chunk.path.clone(),
                    start_line: chunk.chunk.start_line,
                    end_line: chunk.chunk.end_line,
                }
            })
            .collect();
        Ok(RankedSearchResponse { results, degraded })
    }

    fn bm25(&self, query: &str) -> Vec<(usize, f64)> {
        let query_terms: HashSet<String> = tokenize(query).into_iter().collect();
        let count = self.chunks.len() as f64;
        let mut output = Vec::new();
        for (index, chunk) in self.chunks.iter().enumerate() {
            let length = chunk.terms.values().sum::<usize>() as f64;
            let mut score = 0.0;
            for term in &query_terms {
                let frequency = chunk.terms.get(term).copied().unwrap_or(0) as f64;
                if frequency == 0.0 {
                    continue;
                }
                let frequency_docs = self.document_frequency.get(term).copied().unwrap_or(0) as f64;
                let idf = ((count - frequency_docs + 0.5) / (frequency_docs + 0.5)).ln_1p();
                let denominator = 1.2_f64.mul_add(
                    1.0 - 0.75 + 0.75 * length / self.average_length.max(1.0),
                    frequency,
                );
                score += idf * frequency * 2.2 / denominator;
            }
            let lowered = chunk.search_text.to_lowercase();
            if lowered.contains(&query.to_lowercase()) {
                score += 2.0;
            }
            if score > 0.0 {
                output.push((index, score));
            }
        }
        output.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| self.chunk_order(a.0, b.0)));
        output
    }

    fn vector(&self, query: &[f32]) -> Vec<(usize, f64)> {
        let mut output: Vec<_> = self
            .chunks
            .iter()
            .enumerate()
            .filter_map(|(index, chunk)| {
                chunk
                    .vector
                    .as_ref()
                    .map(|vector| (index, cosine(query, vector)))
            })
            .collect();
        output.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| self.chunk_order(a.0, b.0)));
        output
    }

    fn chunk_order(&self, left: usize, right: usize) -> std::cmp::Ordering {
        let left = &self.chunks[left];
        let right = &self.chunks[right];
        left.path
            .cmp(&right.path)
            .then_with(|| left.chunk.start_line.cmp(&right.chunk.start_line))
            .then_with(|| left.chunk.end_line.cmp(&right.chunk.end_line))
            .then_with(|| left.chunk.text.cmp(&right.chunk.text))
    }

    fn add_graph_signals(&self, config: &Config, scores: &mut HashMap<usize, f64>) {
        let page_scores: HashMap<String, f64> =
            scores
                .iter()
                .fold(HashMap::new(), |mut output, (index, score)| {
                    output
                        .entry(self.chunks[*index].path.clone())
                        .and_modify(|value| *value = value.max(*score))
                        .or_insert(*score);
                    output
                });
        let mut additions = Vec::new();
        for (path, score) in page_scores {
            let Some(neighbors) = self.graph.get(&path) else {
                continue;
            };
            let normalization = (neighbors.len().max(1) as f64).sqrt();
            for neighbor in neighbors {
                if let Some(index) = self.first_chunk.get(neighbor) {
                    additions.push((*index, score * config.search.graph_weight / normalization));
                }
            }
        }
        for (index, score) in additions {
            *scores.entry(index).or_default() += score;
        }
    }
}

async fn retrieval_task<T: Send + 'static>(
    operation: impl FnOnce() -> T + Send + 'static,
) -> anyhow::Result<T> {
    tokio::task::spawn_blocking(operation)
        .await
        .context("retrieval task failed")
}

fn term_counts(text: &str) -> HashMap<String, usize> {
    let mut output = HashMap::new();
    for term in tokenize(text) {
        *output.entry(term).or_insert(0) += 1;
    }
    output
}

fn tokenize(text: &str) -> Vec<String> {
    text.split(|character: char| !character.is_alphanumeric())
        .filter(|term| term.len() > 1)
        .map(str::to_lowercase)
        .collect()
}

fn cosine(left: &[f32], right: &[f32]) -> f64 {
    if left.len() != right.len() || left.is_empty() {
        return f64::NEG_INFINITY;
    }
    let (mut dot, mut left_norm, mut right_norm) = (0.0_f64, 0.0_f64, 0.0_f64);
    for (left, right) in left.iter().zip(right) {
        let left = f64::from(*left);
        let right = f64::from(*right);
        dot = left.mul_add(right, dot);
        left_norm = left.mul_add(left, left_norm);
        right_norm = right.mul_add(right, right_norm);
    }
    if left_norm == 0.0 || right_norm == 0.0 {
        0.0
    } else {
        dot / left_norm.sqrt() / right_norm.sqrt()
    }
}
