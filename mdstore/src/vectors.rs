//! Exact similarity search over independent, versioned embedding collections.
//!
//! Collections are rebuildable indexes, not authoritative artifact storage.
//! Callers provide provenance as metadata and filter authorized references before
//! ranking. This module does not generate embeddings or infer speaker identities.
//!
//! ```
//! use mdstore::vectors::{EmbeddingSpace, VectorCollection, VectorRecord};
//!
//! let space = EmbeddingSpace {
//!     namespace: "speakers".into(),
//!     recipe: "example-model-and-preprocessing-v1".into(),
//!     dimensions: 192,
//! };
//! // Illustrative embeddings; production records come from published artifacts.
//! let reference = vec![1.0; 192];
//! let index = VectorCollection::new(space.clone(), vec![VectorRecord {
//!     id: "recording-1/speaker-1".into(),
//!     vector: reference.clone(),
//!     metadata: ("recording-1", "/people/alice.md"),
//! }])?;
//! let authorized_recordings = ["recording-1"];
//! let matches = index.search(&space, &reference, 5, |record| {
//!     authorized_recordings.contains(&record.metadata.0)
//! })?;
//! assert_eq!(matches[0].record.metadata.1, "/people/alice.md");
//! # Ok::<(), anyhow::Error>(())
//! ```

use std::collections::HashSet;

use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};

/// Identity of an embedding space. Equal dimensions alone do not imply compatibility.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EmbeddingSpace {
    /// Independent collection purpose, such as `documents` or `speakers`.
    pub namespace: String,
    /// Model, weights, preprocessing, and aggregation recipe version.
    pub recipe: String,
    /// Number of components in each embedding.
    pub dimensions: usize,
}

/// A vector and the provenance needed to interpret and authorize its use.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VectorRecord<M> {
    /// Stable, unique identifier within this collection.
    pub id: String,
    /// Embedding produced by the collection's declared recipe.
    pub vector: Vec<f32>,
    /// Caller-owned metadata, such as artifact hash, recording, and speaker identity.
    pub metadata: M,
}

/// Immutable collection of validated embeddings in one embedding space.
#[derive(Debug, Clone)]
pub struct VectorCollection<M> {
    space: EmbeddingSpace,
    records: Vec<VectorRecord<M>>,
}

/// A matching reference and its cosine similarity, not an identity probability.
#[derive(Debug)]
pub struct VectorMatch<'a, M> {
    /// Original reference including provenance.
    pub record: &'a VectorRecord<M>,
    /// Cosine similarity in the range -1 through 1.
    pub similarity: f64,
}

impl<M> VectorCollection<M> {
    /// Builds an index, rejecting ambiguous IDs and invalid vectors.
    pub fn new(space: EmbeddingSpace, records: Vec<VectorRecord<M>>) -> Result<Self> {
        ensure!(!space.namespace.trim().is_empty(), "empty vector namespace");
        ensure!(!space.recipe.trim().is_empty(), "empty embedding recipe");
        ensure!(space.dimensions > 0, "zero embedding dimensions");
        let mut ids = HashSet::new();
        for record in &records {
            ensure!(!record.id.trim().is_empty(), "empty vector record ID");
            ensure!(
                ids.insert(&record.id),
                "duplicate vector record ID: {}",
                record.id
            );
            validate_vector(&record.vector, space.dimensions)?;
        }
        Ok(Self { space, records })
    }

    /// The model and collection identity required on queries.
    #[must_use]
    pub fn space(&self) -> &EmbeddingSpace {
        &self.space
    }

    /// Searches only permitted references; authorization is evaluated before top-k.
    ///
    /// The caller must authenticate the query and supply a reference filter based
    /// on its trusted job/access context. No text embedding or reranking runs here.
    pub fn search(
        &self,
        space: &EmbeddingSpace,
        query: &[f32],
        limit: usize,
        mut permitted: impl FnMut(&VectorRecord<M>) -> bool,
    ) -> Result<Vec<VectorMatch<'_, M>>> {
        ensure!(space == &self.space, "incompatible embedding space");
        validate_vector(query, self.space.dimensions)?;
        if limit == 0 {
            return Ok(Vec::new());
        }
        let mut matches: Vec<_> = self
            .records
            .iter()
            .filter(|record| permitted(record))
            .map(|record| VectorMatch {
                record,
                similarity: cosine(query, &record.vector),
            })
            .collect();
        matches.sort_by(|left, right| {
            right
                .similarity
                .total_cmp(&left.similarity)
                .then_with(|| left.record.id.cmp(&right.record.id))
        });
        matches.truncate(limit);
        Ok(matches)
    }
}

fn validate_vector(vector: &[f32], dimensions: usize) -> Result<()> {
    ensure!(
        vector.len() == dimensions,
        "unexpected embedding dimensions"
    );
    ensure!(
        vector.iter().all(|value| value.is_finite()),
        "non-finite embedding"
    );
    ensure!(vector.iter().any(|value| *value != 0.0), "zero embedding");
    Ok(())
}

/// Shared cosine implementation for text retrieval and direct vector queries.
pub(crate) fn cosine(left: &[f32], right: &[f32]) -> f64 {
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
        (dot / left_norm.sqrt() / right_norm.sqrt()).clamp(-1.0, 1.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn space() -> EmbeddingSpace {
        EmbeddingSpace {
            namespace: "speakers".into(),
            recipe: "fixture-v1".into(),
            dimensions: 2,
        }
    }

    fn record(id: &str, vector: Vec<f32>) -> VectorRecord<&'static str> {
        VectorRecord {
            id: id.into(),
            vector,
            metadata: "artifact provenance",
        }
    }

    #[test]
    fn filters_before_ranking_and_preserves_provenance() {
        let index = VectorCollection::new(
            space(),
            vec![
                record("private", vec![1.0, 0.0]),
                record("b", vec![2.0, 2.0]),
                record("a", vec![1.0, 1.0]),
                record("opposite", vec![-1.0, 0.0]),
            ],
        )
        .unwrap();
        let matches = index
            .search(&space(), &[1.0, 0.0], 2, |r| r.id != "private")
            .unwrap();
        assert_eq!(
            matches
                .iter()
                .map(|m| m.record.id.as_str())
                .collect::<Vec<_>>(),
            ["a", "b"]
        );
        assert!((matches[0].similarity - std::f64::consts::FRAC_1_SQRT_2).abs() < 1e-12);
        assert_eq!(matches[0].record.metadata, "artifact provenance");
        assert!(
            index
                .search(&space(), &[1.0, 0.0], 10, |_| false)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn rejects_other_models_and_namespaces_even_at_same_dimension() {
        let index = VectorCollection::<()>::new(space(), vec![]).unwrap();
        let mut other = space();
        other.namespace = "documents".into();
        assert!(index.search(&other, &[1.0, 0.0], 1, |_| true).is_err());
        other = space();
        other.recipe = "fixture-v2".into();
        assert!(index.search(&other, &[1.0, 0.0], 1, |_| true).is_err());
    }

    #[test]
    fn rejects_invalid_records_and_queries() {
        let index = VectorCollection::<()>::new(space(), vec![]).unwrap();
        for vector in [
            vec![],
            vec![1.0],
            vec![0.0, 0.0],
            vec![f32::NAN, 1.0],
            vec![f32::INFINITY, 1.0],
        ] {
            assert!(VectorCollection::new(space(), vec![record("a", vector.clone())]).is_err());
            assert!(index.search(&space(), &vector, 1, |_| true).is_err());
        }
        assert!(
            VectorCollection::new(
                space(),
                vec![record("a", vec![1.0, 0.0]), record("a", vec![0.0, 1.0])]
            )
            .is_err()
        );
        assert!(
            index
                .search(&space(), &[1.0, 0.0], 1, |_| true)
                .unwrap()
                .is_empty()
        );
    }
}
