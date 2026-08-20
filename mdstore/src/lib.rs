//! Git-backed Markdown storage, validation, indexing, and MCP services.

mod chunk;
mod config;
mod git;
mod hashline;
mod markdown;
mod mcp;
mod provider;
mod search;
mod sidecar;
mod store;

use std::path::Path;

use anyhow::Result;

pub use config::{
    ChunkConfig, Config, DocumentConfig, GitConfig, LinkConfig, ProviderConfig, RelationLinkSyntax,
    RelationRule, RelationSelector, SchemaRule, SearchConfig, SectionRule, ServerConfig,
};
pub use git::PushState;
pub use hashline::{EditOperation, short_hash};
pub use markdown::{Edge, Finding};
pub use mcp::{LEGACY_MCP_PROTOCOL_VERSION, MCP_PROTOCOL_VERSION, router, serve, tool_names};
pub use provider::{InputType, RerankResult, RetrievalProvider, ZeroEntropyProvider};
pub use search::{SearchResponse, SearchResult, VectorCoverage};
pub use store::{
    ApplyEditsRequest, ApplyEditsResponse, ApplyStatus, PageResponse, StatusResponse, Store,
    ValidationError,
};

/// Loads and validates the configuration from the repository's committed `HEAD`.
pub fn load_repository_config(root: &Path) -> Result<Config> {
    let head = git::head(root)?;
    Config::from_yaml(&git::read_text(root, &head, ".mdstore/config.yaml")?)
}
