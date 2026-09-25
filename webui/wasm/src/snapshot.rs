use mdstore::validation::{Edge, ParsedPage};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
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
