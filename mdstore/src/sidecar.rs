use std::{fs, io::Write, path::Path};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::chunk::Chunk;

const MAGIC: &[u8; 8] = b"MDSTORE\0";
const VERSION: u16 = 2;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Sidecar {
    pub version: u16,
    pub source_fingerprint: Vec<u8>,
    pub provider_identity: String,
    pub model: String,
    pub dimensions: usize,
    pub chunks: Vec<SidecarChunk>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SidecarChunk {
    pub start_line: usize,
    pub end_line: usize,
    pub heading: Vec<String>,
    pub text_fingerprint: Vec<u8>,
    pub vector_le_f32: Vec<u8>,
}

impl Sidecar {
    #[must_use]
    pub fn new(
        source: &str,
        provider_identity: &str,
        model: &str,
        dimensions: usize,
        chunks: &[Chunk],
        vectors: &[Vec<f32>],
    ) -> Self {
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
        Self {
            version: VERSION,
            source_fingerprint: fingerprint(source),
            provider_identity: provider_identity.into(),
            model: model.into(),
            dimensions,
            chunks,
        }
    }

    pub fn vectors_for(
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
        self.chunks
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
            .collect()
    }
}

#[must_use]
pub fn sidecar_path(page: &Path) -> std::path::PathBuf {
    page.with_extension("mdstore")
}

pub fn read(path: &Path) -> Result<Sidecar> {
    let bytes = fs::read(path).with_context(|| format!("read sidecar {}", path.display()))?;
    if bytes.len() < MAGIC.len() || &bytes[..MAGIC.len()] != MAGIC {
        bail!("invalid mdstore sidecar magic");
    }
    serde_cbor::from_slice(&bytes[MAGIC.len()..]).context("decode mdstore CBOR sidecar")
}

pub fn write_atomic(path: &Path, sidecar: &Sidecar) -> Result<()> {
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
pub fn fingerprint(text: &str) -> Vec<u8> {
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
        let sidecar = Sidecar::new(
            "hello",
            "test-provider",
            "test",
            2,
            &chunks,
            &[vec![1.0, -2.0]],
        );
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
    }
}
