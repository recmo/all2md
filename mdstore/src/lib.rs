//! Git-backed Markdown storage, validation, indexing, and MCP services.

/// Markdown chunk construction.
pub mod chunk;
/// Repository-defined document and daemon configuration.
pub mod config;
/// Git plumbing and worktree recovery.
pub mod git;
/// Hashline rendering and atomic edit application.
pub mod hashline;
/// Markdown parsing, links, relations, and validation.
pub mod markdown;
/// MCP HTTP transport and tool definitions.
pub mod mcp;
/// Embedding and reranking provider abstractions.
pub mod provider;
/// Hybrid retrieval and ranking.
pub mod search;
/// Adjacent binary embedding sidecars.
pub mod sidecar;
/// Coherent repository state and edit transactions.
pub mod store;

pub use config::Config;
pub use store::{ApplyEditsRequest, ApplyEditsResponse, Store};
