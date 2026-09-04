use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use anyhow::Context;
use serde::Serialize;

use crate::{
    chunk::Chunk,
    config::Config,
    markdown::{Edge, ParsedPage, project_metadata},
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
    /// Repository-configured projected metadata.
    pub metadata: serde_json::Value,
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
/// Ranked search results and any provider degradation.
pub struct SearchResponse {
    /// Results in final rank order.
    pub results: Vec<SearchResult>,
    /// Retrieval arms that failed or returned invalid data.
    pub degraded: Vec<String>,
    /// Current vector availability across indexed chunks.
    pub vector_coverage: VectorCoverage,
}

#[derive(Debug, Clone, Serialize)]
/// Available and total indexed vectors.
pub struct VectorCoverage {
    /// Chunks with a valid vector.
    pub ready: usize,
    /// Total indexed chunks.
    pub total: usize,
}

#[derive(Debug, Clone, Serialize)]
/// One ranked searchable excerpt.
pub struct SearchResult {
    /// Repository-relative page path.
    pub path: String,
    /// Repository-configured projected metadata.
    pub metadata: serde_json::Value,
    /// Heading breadcrumb containing the excerpt.
    pub heading: Vec<String>,
    /// First excerpt line, one-based and inclusive.
    pub start_line: usize,
    /// Last excerpt line, one-based and inclusive.
    pub end_line: usize,
    /// Source excerpt without embedding-only context.
    pub excerpt: String,
    /// Exact, vector, or graph arms that contributed.
    pub matched_arms: Vec<String>,
    /// Reciprocal-rank-fusion score before reranking.
    pub fused_score: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    /// Provider reranking score, when reranking succeeded.
    pub rerank_score: Option<f64>,
}

impl SearchIndex {
    #[must_use]
    /// Builds a deterministic index from validated pages and sidecar vectors.
    pub(crate) fn build(
        config: &Config,
        pages: &HashMap<String, String>,
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
            let metadata = project_metadata(config, &page.frontmatter);
            for (chunk, vector) in values {
                let heading = chunk.heading.join(" ");
                let text = &chunk.text;
                let search_text = format!("{path} {metadata} {heading} {text}");
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
        let _ = pages;
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
    ) -> anyhow::Result<SearchResponse> {
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
        let (mut scores, mut arms) = tokio::task::spawn_blocking(move || {
            #[cfg(test)]
            if exact_formulations.first().map(String::as_str) == Some("__slow_retrieval__") {
                std::thread::sleep(std::time::Duration::from_millis(500));
            }
            let mut scores: HashMap<usize, f64> = HashMap::new();
            let mut arms: HashMap<usize, HashSet<String>> = HashMap::new();
            for (formulation_index, formulation) in exact_formulations.iter().enumerate() {
                for (rank, (index, _)) in exact_index
                    .bm25(formulation)
                    .into_iter()
                    .take(exact_config.search.candidates)
                    .enumerate()
                {
                    *scores.entry(index).or_default() +=
                        1.0 / (exact_config.search.rrf_k + rank as f64 + 1.0);
                    arms.entry(index)
                        .or_default()
                        .insert(format!("exact:{formulation_index}"));
                }
            }
            (scores, arms)
        })
        .await
        .context("exact retrieval task failed")?;
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
        let (mut ranked, mut arms, documents) = tokio::task::spawn_blocking(move || {
            if let Some(query_vectors) = query_vectors {
                for (formulation_index, vector) in query_vectors.iter().enumerate() {
                    for (rank, (index, _)) in vector_index
                        .vector(vector)
                        .into_iter()
                        .take(vector_config.search.candidates)
                        .enumerate()
                    {
                        *scores.entry(index).or_default() +=
                            1.0 / (vector_config.search.rrf_k + rank as f64 + 1.0);
                        arms.entry(index)
                            .or_default()
                            .insert(format!("vector:{formulation_index}"));
                    }
                }
            }
            vector_index.add_graph_signals(&vector_config, &mut scores, &mut arms);
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
            (ranked, arms, documents)
        })
        .await
        .context("vector retrieval task failed")?;
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
        Ok(SearchResponse {
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
        })
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        config::LinkConfig,
        markdown::parse_page,
        provider::{InputType, RerankResult},
    };

    struct NonFiniteProvider;

    enum RerankMode {
        Preserve,
        Reverse,
        Fail,
    }

    struct MatrixProvider {
        vectors: HashMap<String, Vec<f32>>,
        embed_fails: bool,
        rerank: RerankMode,
    }

    #[async_trait::async_trait]
    impl RetrievalProvider for MatrixProvider {
        async fn embed(&self, _: InputType, input: &[String]) -> anyhow::Result<Vec<Vec<f32>>> {
            if self.embed_fails {
                anyhow::bail!("embedding unavailable");
            }
            input
                .iter()
                .map(|query| {
                    self.vectors
                        .get(query)
                        .cloned()
                        .ok_or_else(|| anyhow::anyhow!("missing test vector for {query:?}"))
                })
                .collect()
        }

        async fn rerank(
            &self,
            _: &str,
            documents: &[String],
            top_n: usize,
        ) -> anyhow::Result<Vec<RerankResult>> {
            if matches!(self.rerank, RerankMode::Fail) {
                anyhow::bail!("rerank unavailable");
            }
            let count = documents.len().min(top_n);
            Ok((0..count)
                .map(|index| RerankResult {
                    index,
                    relevance_score: match self.rerank {
                        RerankMode::Preserve => (count - index) as f64,
                        RerankMode::Reverse => index as f64,
                        RerankMode::Fail => unreachable!(),
                    },
                })
                .collect())
        }

        fn model(&self) -> &str {
            "matrix"
        }

        fn dimensions(&self) -> usize {
            2
        }

        fn embedding_provider_identity(&self) -> String {
            "matrix".into()
        }
    }

    fn matrix_index(
        entries: &[(&str, &str, Option<[f32; 2]>)],
        edges: &[Edge],
    ) -> (Config, Arc<SearchIndex>) {
        let config = Config::from_yaml(
            "documents:\n  include: ['**/*.md']\nsearch:\n  limit: 10\n  candidates: 10\n  graph_weight: 0.5\nprovider:\n  dimensions: 2\n",
        )
        .unwrap();
        let pages: HashMap<String, String> = entries
            .iter()
            .map(|(path, text, _)| ((*path).into(), (*text).into()))
            .collect();
        let parsed = pages
            .iter()
            .map(|(path, text)| {
                (
                    path.clone(),
                    parse_page(text, &LinkConfig::default()).unwrap(),
                )
            })
            .collect();
        let chunks = entries
            .iter()
            .map(|(path, text, vector)| {
                (
                    (*path).into(),
                    vec![(
                        Chunk {
                            start_line: 1,
                            end_line: 1,
                            heading: Vec::new(),
                            text: (*text).into(),
                            embedding_text: (*text).into(),
                        },
                        vector.map(Vec::from),
                    )],
                )
            })
            .collect();
        let index = SearchIndex::build(&config, &pages, &parsed, edges, chunks);
        (config, Arc::new(index))
    }

    #[async_trait::async_trait]
    impl RetrievalProvider for NonFiniteProvider {
        async fn embed(&self, _: InputType, input: &[String]) -> anyhow::Result<Vec<Vec<f32>>> {
            Ok(input.iter().map(|_| vec![f32::NAN, 0.0]).collect())
        }

        async fn rerank(
            &self,
            _: &str,
            _: &[String],
            _: usize,
        ) -> anyhow::Result<Vec<RerankResult>> {
            Ok(Vec::new())
        }

        fn model(&self) -> &str {
            "invalid"
        }

        fn dimensions(&self) -> usize {
            2
        }

        fn embedding_provider_identity(&self) -> String {
            "invalid".into()
        }
    }

    #[test]
    fn index_order_does_not_depend_on_hashmap_iteration() {
        let config =
            Config::from_yaml("documents:\n  include: ['**/*.md']\nprovider:\n  dimensions: 2\n")
                .unwrap();
        let pages: HashMap<String, String> = HashMap::from([
            ("b.md".into(), "bravo".into()),
            ("a.md".into(), "alpha".into()),
        ]);
        let parsed = pages
            .iter()
            .map(|(path, text)| {
                (
                    path.clone(),
                    parse_page(text, &LinkConfig::default()).unwrap(),
                )
            })
            .collect();
        let chunk = |text: &str| Chunk {
            start_line: 1,
            end_line: 1,
            heading: Vec::new(),
            text: text.into(),
            embedding_text: text.into(),
        };
        let first = PageChunks::from([
            ("b.md".into(), vec![(chunk("bravo"), None)]),
            ("a.md".into(), vec![(chunk("alpha"), None)]),
        ]);
        let second = PageChunks::from([
            ("a.md".into(), vec![(chunk("alpha"), None)]),
            ("b.md".into(), vec![(chunk("bravo"), None)]),
        ]);
        let paths = |index: SearchIndex| {
            index
                .chunks
                .into_iter()
                .map(|chunk| chunk.path)
                .collect::<Vec<_>>()
        };
        assert_eq!(
            paths(SearchIndex::build(&config, &pages, &parsed, &[], first)),
            paths(SearchIndex::build(&config, &pages, &parsed, &[], second))
        );
    }

    #[tokio::test]
    async fn non_finite_query_vectors_degrade_before_ranking() {
        let config =
            Config::from_yaml("documents:\n  include: ['**/*.md']\nprovider:\n  dimensions: 2\n")
                .unwrap();
        let pages = HashMap::from([("a.md".into(), "alpha".into())]);
        let parsed = HashMap::from([(
            "a.md".into(),
            parse_page("alpha", &LinkConfig::default()).unwrap(),
        )]);
        let chunks = PageChunks::from([(
            "a.md".into(),
            vec![(
                Chunk {
                    start_line: 1,
                    end_line: 1,
                    heading: Vec::new(),
                    text: "alpha".into(),
                    embedding_text: "alpha".into(),
                },
                Some(vec![1.0, 0.0]),
            )],
        )]);
        let index = Arc::new(SearchIndex::build(&config, &pages, &parsed, &[], chunks));

        let response = index
            .search(&config, &NonFiniteProvider, "alpha", &[])
            .await
            .unwrap();
        assert_eq!(response.results.len(), 1);
        assert_eq!(response.results[0].matched_arms, ["exact:0"]);
        assert!(
            response
                .degraded
                .iter()
                .any(|message| message.contains("non-finite"))
        );
    }

    #[tokio::test]
    async fn exact_only_search_and_fusion_are_stable() {
        let (config, index) = matrix_index(
            &[
                ("b.md", "shared phrase", None),
                ("a.md", "shared phrase", None),
            ],
            &[],
        );
        let provider = MatrixProvider {
            vectors: HashMap::new(),
            embed_fails: true,
            rerank: RerankMode::Fail,
        };

        let first = index
            .search(&config, &provider, "shared phrase", &[])
            .await
            .unwrap();
        let second = index
            .search(&config, &provider, "shared phrase", &[])
            .await
            .unwrap();
        let summary = |response: &SearchResponse| {
            response
                .results
                .iter()
                .map(|result| {
                    (
                        result.path.clone(),
                        result.matched_arms.clone(),
                        result.fused_score,
                    )
                })
                .collect::<Vec<_>>()
        };

        assert_eq!(summary(&first), summary(&second));
        assert_eq!(first.results[0].path, "a.md");
        assert!(
            first
                .results
                .iter()
                .all(|result| result.matched_arms == ["exact:0"])
        );
    }

    #[tokio::test]
    async fn retrieval_scans_do_not_stall_the_async_scheduler() {
        let (config, index) = matrix_index(&[("a.md", "ordinary text", None)], &[]);
        let provider = Arc::new(MatrixProvider {
            vectors: HashMap::new(),
            embed_fails: true,
            rerank: RerankMode::Fail,
        });
        let search = tokio::spawn(async move {
            index
                .search(&config, provider.as_ref(), "__slow_retrieval__", &[])
                .await
        });
        let started = std::time::Instant::now();
        tokio::task::yield_now().await;
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        assert!(started.elapsed() < std::time::Duration::from_millis(250));
        search.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn vector_and_variant_arms_retrieve_independently() {
        let (config, index) = matrix_index(
            &[
                ("a.md", "ordinary text", Some([0.0, 1.0])),
                ("b.md", "needle text", Some([1.0, 0.0])),
            ],
            &[],
        );
        let provider = MatrixProvider {
            vectors: HashMap::from([
                ("semantic".into(), vec![1.0, 0.0]),
                ("missing".into(), vec![0.0, 1.0]),
                ("needle".into(), vec![1.0, 0.0]),
            ]),
            embed_fails: false,
            rerank: RerankMode::Preserve,
        };

        let vector_only = index
            .search(&config, &provider, "semantic", &[])
            .await
            .unwrap();
        assert_eq!(vector_only.results[0].path, "b.md");
        assert_eq!(vector_only.results[0].matched_arms, ["vector:0"]);

        let variant = index
            .search(&config, &provider, "missing", &["needle".into()])
            .await
            .unwrap();
        let target = variant
            .results
            .iter()
            .find(|result| result.path == "b.md")
            .unwrap();
        assert!(target.matched_arms.contains(&"exact:1".into()));
        assert!(target.matched_arms.contains(&"vector:1".into()));
    }

    #[tokio::test]
    async fn graph_assist_adds_authored_neighbors() {
        let edge = Edge {
            source: "seed.md".into(),
            relation: "related".into(),
            target: "neighbor.md".into(),
        };
        let (config, index) = matrix_index(
            &[
                ("seed.md", "origin keyword", None),
                ("neighbor.md", "otherwise unrelated", None),
            ],
            &[edge],
        );
        let provider = MatrixProvider {
            vectors: HashMap::new(),
            embed_fails: true,
            rerank: RerankMode::Fail,
        };

        let response = index
            .search(&config, &provider, "origin keyword", &[])
            .await
            .unwrap();
        let neighbor = response
            .results
            .iter()
            .find(|result| result.path == "neighbor.md")
            .unwrap();
        assert_eq!(neighbor.matched_arms, ["graph"]);
    }

    #[tokio::test]
    async fn rerank_overrides_fusion_and_failure_preserves_it() {
        let (config, index) = matrix_index(
            &[
                ("a.md", "shared phrase", None),
                ("b.md", "shared phrase", None),
            ],
            &[],
        );
        let reranked = MatrixProvider {
            vectors: HashMap::new(),
            embed_fails: true,
            rerank: RerankMode::Reverse,
        };
        let fallback = MatrixProvider {
            vectors: HashMap::new(),
            embed_fails: true,
            rerank: RerankMode::Fail,
        };

        let response = index
            .search(&config, &reranked, "shared", &[])
            .await
            .unwrap();
        assert_eq!(response.results[0].path, "b.md");
        assert!(response.results[0].rerank_score.is_some());

        let response = index
            .search(&config, &fallback, "shared", &[])
            .await
            .unwrap();
        assert_eq!(response.results[0].path, "a.md");
        assert!(
            response
                .results
                .iter()
                .all(|result| result.rerank_score.is_none())
        );
        assert!(
            response
                .degraded
                .iter()
                .any(|message| message.contains("rerank unavailable"))
        );
    }
}
