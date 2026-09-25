//! Document validation primitives shared by servers and independent clients.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;

pub use crate::config::{Config, is_config_resource_path, validate_repo_path};
pub use crate::markdown::{
    CorpusValidation, Edge, Finding, FindingSource, Heading, LinkSyntax, ParsedPage, RawLink,
    SourceRange, ValidationBaseline, parse_frontmatter, parse_page, validate_corpus,
    validate_incremental,
};
pub use crate::template::{Templates, is_template};
pub use crate::template_script::extract as extract_starlark;

/// Version of the portable validation snapshot format.
pub const SNAPSHOT_VERSION: u32 = 1;
/// Parsed facts and exact source identity for one document.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotDocument {
    /// SHA-256 of the original UTF-8 source.
    pub hash: String,
    /// Parsed facts from the validated revision.
    pub parsed: ParsedPage,
}
/// A validated document inventory, with schema resources and relation edges.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ValidationSnapshot {
    /// Snapshot format version.
    pub version: u32,
    /// Repository revision.
    pub revision: String,
    /// Configuration and template sources.
    pub files: HashMap<String, String>,
    /// Document identities and parsed facts.
    pub documents: HashMap<String, SnapshotDocument>,
    /// Validated relation graph.
    pub edges: Vec<Edge>,
}
/// Computes the source identity used in validation snapshots.
pub fn source_hash(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}
