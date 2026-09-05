use std::{fs, io::Write, path::Path};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::{chunk::Chunk, provider::validate_vectors};

const MAGIC: &[u8; 8] = b"MDSTORE\0";
const VERSION: u16 = 2;

#[derive(Debug, Clone, Serialize, Deserialize)]
/// Versioned embedding state derived from one Markdown page.
pub(crate) struct Sidecar {
    /// Binary schema version.
    pub version: u16,
    /// Fingerprint of the complete source page.
    pub source_fingerprint: Vec<u8>,
    /// Credential-free embedding backend identity.
    pub provider_identity: String,
    /// Embedding model identifier.
    pub model: String,
    /// Number of vector components per chunk.
    pub dimensions: usize,
    /// Stored chunk metadata and vector bytes.
    pub chunks: Vec<SidecarChunk>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
/// Stored metadata and vector bytes for one chunk.
pub(crate) struct SidecarChunk {
    /// First source line, one-based and inclusive.
    pub start_line: usize,
    /// Last source line, one-based and inclusive.
    pub end_line: usize,
    /// Heading breadcrumb captured when chunked.
    pub heading: Vec<String>,
    /// Fingerprint of the embedding input.
    pub text_fingerprint: Vec<u8>,
    /// Vector components encoded as little-endian `f32` values.
    pub vector_le_f32: Vec<u8>,
}

impl Sidecar {
    /// Builds a sidecar after validating the supplied vectors.
    pub(crate) fn new(
        source: &str,
        provider_identity: &str,
        model: &str,
        dimensions: usize,
        chunks: &[Chunk],
        vectors: &[Vec<f32>],
    ) -> Result<Self> {
        validate_vectors(vectors, chunks.len(), dimensions)?;
        let chunks = chunks
            .iter()
            .zip(vectors)
            .map(|(chunk, vector)| SidecarChunk {
                start_line: chunk.start_line,
                end_line: chunk.end_line,
                heading: chunk.heading.clone(),
                text_fingerprint: fingerprint(&chunk.embedding_text),
                vector_le_f32: vector
                    .iter()
                    .flat_map(|value| value.to_le_bytes())
                    .collect(),
            })
            .collect();
        Ok(Self {
            version: VERSION,
            source_fingerprint: fingerprint(source),
            provider_identity: provider_identity.into(),
            model: model.into(),
            dimensions,
            chunks,
        })
    }

    /// Returns vectors only when all source, provider, and chunk metadata match.
    #[must_use]
    pub(crate) fn vectors_for(
        &self,
        source: &str,
        provider_identity: &str,
        model: &str,
        dimensions: usize,
        chunks: &[Chunk],
    ) -> Option<Vec<Vec<f32>>> {
        if self.version != VERSION
            || self.source_fingerprint != fingerprint(source)
            || self.provider_identity != provider_identity
            || self.model != model
            || self.dimensions != dimensions
            || self.chunks.len() != chunks.len()
        {
            return None;
        }
        let vectors: Option<Vec<Vec<f32>>> = self
            .chunks
            .iter()
            .zip(chunks)
            .map(|(stored, chunk)| {
                if stored.start_line != chunk.start_line
                    || stored.end_line != chunk.end_line
                    || stored.text_fingerprint != fingerprint(&chunk.embedding_text)
                    || stored.vector_le_f32.len() != dimensions * 4
                {
                    return None;
                }
                Some(
                    stored
                        .vector_le_f32
                        .chunks_exact(4)
                        .map(|bytes| f32::from_le_bytes(bytes.try_into().expect("four bytes")))
                        .collect(),
                )
            })
            .collect();
        let vectors = vectors?;
        validate_vectors(&vectors, chunks.len(), dimensions).ok()?;
        Some(vectors)
    }
}

#[must_use]
/// Returns the adjacent sidecar path for a Markdown page.
pub(crate) fn sidecar_path(page: &Path) -> std::path::PathBuf {
    page.with_extension("mdstore")
}

/// Reads and decodes a sidecar.
pub(crate) fn read(path: &Path) -> Result<Sidecar> {
    let bytes = fs::read(path).with_context(|| format!("read sidecar {}", path.display()))?;
    if bytes.len() < MAGIC.len() || &bytes[..MAGIC.len()] != MAGIC {
        bail!("invalid mdstore sidecar magic");
    }
    serde_cbor::from_slice(&bytes[MAGIC.len()..]).context("decode mdstore CBOR sidecar")
}

/// Atomically replaces a sidecar with a versioned CBOR encoding.
pub(crate) fn write_atomic(path: &Path, sidecar: &Sidecar) -> Result<()> {
    let parent = path.parent().context("sidecar has no parent")?;
    fs::create_dir_all(parent)?;
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    temp.write_all(MAGIC)?;
    serde_cbor::to_writer(&mut temp, sidecar)?;
    temp.as_file().sync_all()?;
    temp.persist(path)
        .map_err(|error| error.error)
        .with_context(|| format!("replace sidecar {}", path.display()))?;
    Ok(())
}

#[must_use]
/// Computes a stable source or embedding-input fingerprint.
pub(crate) fn fingerprint(text: &str) -> Vec<u8> {
    Sha256::digest(text.as_bytes()).to_vec()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn binary_round_trip() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("note.mdstore");
        let chunks = vec![Chunk {
            start_line: 1,
            end_line: 1,
            heading: vec!["Demo".into()],
            text: "hello".into(),
            embedding_text: "Demo\n\nhello".into(),
        }];
        let mut sidecar = Sidecar::new(
            "hello",
            "test-provider",
            "test",
            2,
            &chunks,
            &[vec![1.0, -2.0]],
        )
        .unwrap();
        write_atomic(&path, &sidecar).unwrap();
        let loaded = read(&path).unwrap();
        assert_eq!(
            loaded
                .vectors_for("hello", "test-provider", "test", 2, &chunks)
                .unwrap(),
            [vec![1.0, -2.0]]
        );
        assert!(
            loaded
                .vectors_for("hello", "other-provider", "test", 2, &chunks)
                .is_none()
        );
        sidecar.chunks[0].vector_le_f32 = f32::NAN.to_le_bytes().to_vec();
        assert!(
            sidecar
                .vectors_for("hello", "test-provider", "test", 2, &chunks)
                .is_none()
        );
        assert!(
            Sidecar::new(
                "hello",
                "test-provider",
                "test",
                2,
                &chunks,
                &[vec![f32::INFINITY, 0.0]],
            )
            .is_err()
        );
    }
}
