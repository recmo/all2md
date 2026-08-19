use std::collections::{HashMap, HashSet};

use serde::Serialize;

use crate::{
    chunk::Chunk,
    config::Config,
    markdown::{Edge, ParsedPage, project_metadata},
    provider::{InputType, RetrievalProvider},
};

#[derive(Debug, Clone)]
pub struct SearchIndex {
    pub chunks: Vec<IndexedChunk>,
    document_frequency: HashMap<String, usize>,
    average_length: f64,
    graph: HashMap<String, HashSet<String>>,
    first_chunk: HashMap<String, usize>,
}

#[derive(Debug, Clone)]
pub struct IndexedChunk {
    pub path: String,
    pub chunk: Chunk,
    pub metadata: serde_json::Value,
    pub search_text: String,
    pub terms: HashMap<String, usize>,
    pub vector: Option<Vec<f32>>,
}

pub type PageChunks = HashMap<String, Vec<(Chunk, Option<Vec<f32>>)>>;

#[derive(Debug, Clone, Serialize)]
pub struct SearchResponse {
    pub results: Vec<SearchResult>,
    pub degraded: Vec<String>,
    pub vector_coverage: VectorCoverage,
}

#[derive(Debug, Clone, Serialize)]
pub struct VectorCoverage {
    pub ready: usize,
    pub total: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct SearchResult {
    pub path: String,
    pub metadata: serde_json::Value,
    pub heading: Vec<String>,
    pub start_line: usize,
    pub end_line: usize,
    pub excerpt: String,
    pub matched_arms: Vec<String>,
    pub fused_score: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rerank_score: Option<f64>,
}

impl SearchIndex {
    #[must_use]
    pub fn build(
        config: &Config,
        pages: &HashMap<String, String>,
        parsed: &HashMap<String, ParsedPage>,
        edges: &[Edge],
        page_chunks: PageChunks,
    ) -> Self {
        let mut chunks = Vec::new();
        let mut first_chunk = HashMap::new();
        for (path, values) in page_chunks {
            let Some(page) = parsed.get(&path) else {
                continue;
            };
            let metadata = project_metadata(config, &page.frontmatter);
            for (chunk, vector) in values {
                let search_text = format!(
                    "{} {} {} {}",
                    path,
                    metadata,
                    chunk.heading.join(" "),
                    chunk.text
                );
                let terms = term_counts(&search_text);
                first_chunk.entry(path.clone()).or_insert(chunks.len());
                chunks.push(IndexedChunk {
                    path: path.clone(),
                    chunk,
                    metadata: metadata.clone(),
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
        let mut graph: HashMap<String, HashSet<String>> = HashMap::new();
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
        let _ = pages;
        Self {
            chunks,
            document_frequency,
            average_length,
            graph,
            first_chunk,
        }
    }

    pub async fn search(
        &self,
        config: &Config,
        provider: &dyn RetrievalProvider,
        query: &str,
        variants: &[String],
    ) -> SearchResponse {
        let formulations: Vec<String> = std::iter::once(query.to_owned())
            .chain(
                variants
                    .iter()
                    .filter(|value| !value.trim().is_empty())
                    .cloned(),
            )
            .collect();
        let mut scores: HashMap<usize, f64> = HashMap::new();
        let mut arms: HashMap<usize, HashSet<String>> = HashMap::new();
        for (formulation_index, formulation) in formulations.iter().enumerate() {
            for (rank, (index, _)) in self
                .bm25(formulation)
                .into_iter()
                .take(config.search.candidates)
                .enumerate()
            {
                *scores.entry(index).or_default() +=
                    1.0 / (config.search.rrf_k + rank as f64 + 1.0);
                arms.entry(index)
                    .or_default()
                    .insert(format!("exact:{formulation_index}"));
            }
        }
        let mut degraded = Vec::new();
        match provider.embed(InputType::Query, &formulations).await {
            Ok(query_vectors) => {
                for (formulation_index, vector) in query_vectors.iter().enumerate() {
                    for (rank, (index, _)) in self
                        .vector(vector)
                        .into_iter()
                        .take(config.search.candidates)
                        .enumerate()
                    {
                        *scores.entry(index).or_default() +=
                            1.0 / (config.search.rrf_k + rank as f64 + 1.0);
                        arms.entry(index)
                            .or_default()
                            .insert(format!("vector:{formulation_index}"));
                    }
                }
            }
            Err(error) => degraded.push(format!("embedding: {error}")),
        }
        self.add_graph_signals(config, &mut scores, &mut arms);
        let mut ranked: Vec<(usize, f64)> = scores.into_iter().collect();
        ranked.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        ranked.truncate(config.search.candidates);

        let documents: Vec<String> = ranked
            .iter()
            .map(|(index, _)| self.chunks[*index].chunk.text.clone())
            .collect();
        let mut rerank_scores = HashMap::new();
        match provider
            .rerank(query, &documents, config.search.limit)
            .await
        {
            Ok(results) => {
                for result in results {
                    if let Some((chunk_index, _)) = ranked.get(result.index) {
                        rerank_scores.insert(*chunk_index, result.relevance_score);
                    }
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
                });
            }
            Err(error) => degraded.push(format!("rerank: {error}")),
        }
        ranked.truncate(config.search.limit);
        let results = ranked
            .into_iter()
            .map(|(index, fused_score)| {
                let chunk = &self.chunks[index];
                let mut matched_arms: Vec<String> = arms
                    .remove(&index)
                    .unwrap_or_default()
                    .into_iter()
                    .collect();
                matched_arms.sort();
                SearchResult {
                    path: chunk.path.clone(),
                    metadata: chunk.metadata.clone(),
                    heading: chunk.chunk.heading.clone(),
                    start_line: chunk.chunk.start_line,
                    end_line: chunk.chunk.end_line,
                    excerpt: chunk.chunk.text.clone(),
                    matched_arms,
                    fused_score,
                    rerank_score: rerank_scores.get(&index).copied(),
                }
            })
            .collect();
        SearchResponse {
            results,
            degraded,
            vector_coverage: VectorCoverage {
                ready: self
                    .chunks
                    .iter()
                    .filter(|chunk| chunk.vector.is_some())
                    .count(),
                total: self.chunks.len(),
            },
        }
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
                let idf = ((count - frequency_docs + 0.5) / (frequency_docs + 0.5) + 1.0).ln();
                let denominator =
                    frequency + 1.2 * (1.0 - 0.75 + 0.75 * length / self.average_length.max(1.0));
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
        output.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
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
        output.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        output
    }

    fn add_graph_signals(
        &self,
        config: &Config,
        scores: &mut HashMap<usize, f64>,
        arms: &mut HashMap<usize, HashSet<String>>,
    ) {
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
            arms.entry(index).or_default().insert("graph".into());
        }
    }
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
        dot += left * right;
        left_norm += left * left;
        right_norm += right * right;
    }
    if left_norm == 0.0 || right_norm == 0.0 {
        0.0
    } else {
        dot / left_norm.sqrt() / right_norm.sqrt()
    }
}
