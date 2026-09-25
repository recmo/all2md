//! Git-backed Markdown storage, validation, indexing, and MCP services.

#[cfg(feature = "server")]
mod chunk;
mod config;
#[cfg(feature = "server")]
mod git;
#[cfg(feature = "server")]
mod hashline;
mod markdown;
mod markdown_lint;
#[cfg(feature = "server")]
mod mcp;
#[cfg(feature = "server")]
mod provider;
#[cfg(feature = "server")]
mod search;
#[cfg(feature = "server")]
mod sidecar;
#[cfg(feature = "server")]
mod store;
mod structure;
mod template;
mod template_script;

pub use config::{
    ChunkConfig, Config, DateOrder, DocumentConfig, GitConfig, LinkConfig, ProviderConfig,
    RelationLinkSyntax, RelationRule, RelationSelector, SearchConfig, SectionListRule,
    ServerConfig,
};
#[cfg(feature = "server")]
pub use git::PushState;
#[cfg(feature = "server")]
pub use hashline::{EditOperation, short_hash};
pub use markdown::{Edge, Finding};
#[cfg(feature = "server")]
pub use mcp::{
    LEGACY_MCP_PROTOCOL_VERSION, MCP_PROTOCOL_VERSION, serve, serve_listener, tool_names,
};
#[cfg(feature = "server")]
pub use provider::{InputType, RerankResult, RetrievalProvider, ZeroEntropyProvider};
#[cfg(feature = "server")]
pub use search::{SearchResponse, SearchResult, VectorCoverage};
#[cfg(feature = "server")]
pub use store::{
    ApplyEditsRequest, ApplyEditsResponse, ApplyStatus, PageResponse, ReplicationStatus,
    StatusResponse, Store, ValidationError,
};

/// Reusable document parsing and schema validation.
pub mod validation;
