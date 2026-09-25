//! Document validation primitives shared by servers and independent clients.
use sha2::{Digest, Sha256};

pub use crate::config::{Config, is_config_resource_path, validate_repo_path};
pub use crate::markdown::{
    CorpusValidation, Edge, Finding, FindingSource, Heading, LinkSyntax, ParsedPage, RawLink,
    SourceRange, ValidationBaseline, parse_frontmatter, parse_page, validate_corpus,
    validate_incremental,
};
pub use crate::template::{Templates, is_template};
pub use crate::template_script::extract as extract_starlark;

/// Computes the SHA-256 identity of a document source.
pub fn source_hash(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}
