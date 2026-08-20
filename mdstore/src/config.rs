use std::{
    collections::{BTreeMap, HashSet},
    fs,
    net::SocketAddr,
    path::Path,
};

use anyhow::{Context, Result, bail};
use globset::{Glob, GlobSet, GlobSetBuilder};
use regex::Regex;
use serde::{Deserialize, Serialize};

fn default_true() -> bool {
    true
}

fn default_chunk_tokens() -> usize {
    400
}

fn default_overlap_percent() -> usize {
    15
}

fn default_max_chars() -> usize {
    2_000
}

fn default_dimensions() -> usize {
    1_280
}

fn default_embed_model() -> String {
    "zembed-1".into()
}

fn default_rerank_model() -> String {
    "zerank-2".into()
}

fn default_base_url() -> String {
    "https://api.zeroentropy.dev/v1".into()
}

fn default_api_key_env() -> String {
    "ZEROENTROPY_API_KEY".into()
}

fn default_request_timeout_seconds() -> u64 {
    30
}

fn default_push_timeout_seconds() -> u64 {
    30
}

fn default_limit() -> usize {
    10
}

fn default_candidates() -> usize {
    30
}

fn default_rrf_k() -> f64 {
    60.0
}

fn default_listen() -> String {
    "127.0.0.1:3131".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Complete repository-defined mdstore configuration.
pub struct Config {
    /// Markdown corpus selection rules.
    pub documents: DocumentConfig,
    #[serde(default)]
    /// Frontmatter schema assignments.
    pub schemas: Vec<SchemaRule>,
    #[serde(default)]
    /// Required or bounded heading rules.
    pub sections: Vec<SectionRule>,
    #[serde(default)]
    /// Output metadata names mapped to JSON pointers.
    pub metadata: BTreeMap<String, String>,
    #[serde(default)]
    /// Enabled Markdown and wiki-link syntaxes.
    pub links: LinkConfig,
    #[serde(default)]
    /// Typed relation projections and reciprocal rules.
    pub relations: Vec<RelationRule>,
    #[serde(default)]
    /// Chunk construction settings.
    pub chunking: ChunkConfig,
    #[serde(default)]
    /// Retrieval and ranking settings.
    pub search: SearchConfig,
    #[serde(default)]
    /// Embedding and reranking provider settings.
    pub provider: ProviderConfig,
    #[serde(default)]
    /// Git push settings.
    pub git: GitConfig,
    #[serde(default)]
    /// HTTP server settings.
    pub server: ServerConfig,
}

impl Config {
    /// Parses and validates repository YAML configuration.
    pub fn from_yaml(text: &str) -> Result<Self> {
        let value: Self = serde_yaml::from_str(text).context("parse .mdstore/config.yaml")?;
        value.validate()?;
        Ok(value)
    }

    /// Validates all configuration invariants.
    pub fn validate(&self) -> Result<()> {
        if self.documents.include.is_empty() {
            bail!("documents.include must contain at least one glob");
        }
        let _ = self.document_globs()?;
        for schema in &self.schemas {
            let _ = compile_globs(std::slice::from_ref(&schema.include))?;
            validate_repo_path(&schema.schema)?;
        }
        for section in &self.sections {
            if let Some(include) = &section.include {
                let _ = compile_globs(std::slice::from_ref(include))?;
            }
            let minimum = section.minimum();
            if section.maximum.is_some_and(|maximum| maximum < minimum) {
                bail!(
                    "section {:?} maximum must be at least its effective minimum {minimum}",
                    section.heading
                );
            }
        }
        for wiki in &self.links.wiki {
            let pattern =
                Regex::new(wiki).with_context(|| format!("invalid wiki-link pattern {wiki:?}"))?;
            if !pattern
                .capture_names()
                .flatten()
                .any(|name| name == "target")
            {
                bail!("wiki-link pattern must define a named target capture");
            }
        }
        for pointer in self.metadata.values() {
            validate_json_pointer(pointer, "metadata")?;
        }
        for pointer in &self.chunking.context_pointers {
            validate_json_pointer(pointer, "chunking context")?;
        }
        if self.chunking.target_tokens == 0 || self.chunking.max_chars == 0 {
            bail!("chunking limits must be greater than zero");
        }
        if self.chunking.overlap_percent >= 100 {
            bail!("chunking.overlap_percent must be less than 100");
        }
        if self.search.limit == 0 || self.search.candidates < self.search.limit {
            bail!("search.candidates must be at least search.limit, both non-zero");
        }
        if !self.search.rrf_k.is_finite() || self.search.rrf_k <= 0.0 {
            bail!("search.rrf_k must be finite and greater than zero");
        }
        if !self.search.graph_weight.is_finite() || self.search.graph_weight < 0.0 {
            bail!("search.graph_weight must be finite and non-negative");
        }
        if self.provider.dimensions == 0 {
            bail!("provider.dimensions must be non-zero");
        }
        if self.provider.request_timeout_seconds == 0 {
            bail!("provider.request_timeout_seconds must be non-zero");
        }
        if self.git.push_timeout_seconds == 0 {
            bail!("git.push_timeout_seconds must be non-zero");
        }
        let listen: SocketAddr = self
            .server
            .listen
            .parse()
            .context("server.listen must be an IP socket address")?;
        if listen.port() == 0 {
            bail!("server.listen port must be non-zero");
        }
        let mut relation_names = HashSet::new();
        for relation in &self.relations {
            if relation.name.trim().is_empty() {
                bail!("relation names must be non-empty");
            }
            if !relation_names.insert(relation.name.as_str()) {
                bail!("duplicate relation name: {}", relation.name);
            }
            relation.selector.validate()?;
        }
        for relation in &self.relations {
            if let Some(reciprocal) = &relation.reciprocal
                && !relation_names.contains(reciprocal.as_str())
            {
                bail!(
                    "relation {} references unknown reciprocal relation {reciprocal}",
                    relation.name
                );
            }
        }
        Ok(())
    }

    /// Compiles the document include and exclude glob sets.
    pub fn document_globs(&self) -> Result<(GlobSet, GlobSet)> {
        Ok((
            compile_globs(&self.documents.include)?,
            compile_globs(&self.documents.exclude)?,
        ))
    }
}

fn compile_globs(patterns: &[String]) -> Result<GlobSet> {
    let mut builder = GlobSetBuilder::new();
    for pattern in patterns {
        builder.add(Glob::new(pattern).with_context(|| format!("invalid glob {pattern:?}"))?);
    }
    builder.build().context("compile glob set")
}

fn validate_json_pointer(pointer: &str, kind: &str) -> Result<()> {
    if pointer.is_empty() {
        return Ok(());
    }
    if !pointer.starts_with('/') {
        bail!("{kind} JSON pointer must be empty or start with '/': {pointer}");
    }
    if pointer.split('/').skip(1).any(|segment| {
        segment
            .split('~')
            .skip(1)
            .any(|suffix| !suffix.starts_with(['0', '1']))
    }) {
        bail!("{kind} JSON pointer contains an invalid escape: {pointer}");
    }
    Ok(())
}

/// Validates a repository-relative path without accessing the filesystem.
pub(crate) fn validate_repo_path(path: &str) -> Result<()> {
    let candidate = Path::new(path);
    if path.is_empty() || candidate.is_absolute() {
        bail!("path must stay within the repository: {path}");
    }
    for component in candidate.components() {
        let std::path::Component::Normal(value) = component else {
            bail!("path must contain only normal repository components: {path}");
        };
        if value.to_string_lossy().eq_ignore_ascii_case(".git") {
            bail!("path may not enter Git metadata: {path}");
        }
    }
    Ok(())
}

pub(crate) fn is_config_resource_path(path: &str) -> bool {
    path.starts_with(".mdstore/")
        && Path::new(path).extension().is_some_and(|extension| {
            extension.eq_ignore_ascii_case("yaml")
                || extension.eq_ignore_ascii_case("yml")
                || extension.eq_ignore_ascii_case("json")
        })
}

/// Rejects repository paths whose existing ancestors contain symlinks.
pub(crate) fn ensure_repository_path_safe(root: &Path, path: &str) -> Result<()> {
    validate_repo_path(path)?;
    let mut current = root.to_path_buf();
    for component in Path::new(path).components() {
        current.push(component);
        if let Ok(metadata) = fs::symlink_metadata(&current)
            && metadata.file_type().is_symlink()
        {
            bail!("repository path may not traverse a symlink: {path}");
        }
    }
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Selects tracked Markdown files for the canonical corpus.
pub struct DocumentConfig {
    /// Glob patterns included in the corpus.
    pub include: Vec<String>,
    #[serde(default)]
    /// Glob patterns removed from the included set.
    pub exclude: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Assigns a JSON Schema resource to matching pages.
pub struct SchemaRule {
    /// Glob pattern selecting pages governed by the schema.
    pub include: String,
    /// Repository-relative JSON Schema resource path.
    pub schema: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Constrains occurrences of a heading in selected pages.
pub struct SectionRule {
    #[serde(default)]
    /// Optional glob selecting pages governed by the rule.
    pub include: Option<String>,
    /// Exact heading text to count.
    pub heading: String,
    #[serde(default)]
    /// Requires at least one occurrence when true.
    pub required: bool,
    #[serde(default)]
    /// Explicit minimum occurrence count.
    pub minimum: Option<usize>,
    #[serde(default)]
    /// Optional maximum occurrence count.
    pub maximum: Option<usize>,
}

impl SectionRule {
    pub(crate) fn minimum(&self) -> usize {
        self.minimum
            .unwrap_or_default()
            .max(usize::from(self.required))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Configures authored link syntaxes.
pub struct LinkConfig {
    #[serde(default = "default_true")]
    /// Parses standard Markdown links when true.
    pub markdown: bool,
    #[serde(default)]
    /// Regex patterns with a named `target` capture for wiki links.
    pub wiki: Vec<String>,
}

impl Default for LinkConfig {
    fn default() -> Self {
        Self {
            markdown: true,
            wiki: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Defines one typed relation and its optional reciprocal type.
pub struct RelationRule {
    /// Stable relation name.
    pub name: String,
    #[serde(default)]
    /// Relation name required on the reverse edge.
    pub reciprocal: Option<String>,
    /// Authored data selected as relation targets.
    pub selector: RelationSelector,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
/// Selects relation targets from links or frontmatter.
pub enum RelationSelector {
    /// Selects resolved links from matching pages or sections.
    MarkdownLinks {
        #[serde(default)]
        /// Optional source-page glob.
        include: Option<String>,
        #[serde(default)]
        /// Optional containing heading text.
        section: Option<String>,
        #[serde(default)]
        /// Optional required authored link syntax.
        syntax: Option<RelationLinkSyntax>,
    },
    /// Selects target records from a frontmatter array.
    Frontmatter {
        /// JSON pointer to the array of relation records.
        array_pointer: String,
        /// JSON pointer within each record to its target string.
        target_pointer: String,
        #[serde(default)]
        /// Optional JSON pointer used to discriminate record types.
        type_pointer: Option<String>,
        #[serde(default)]
        /// Required value at `type_pointer` for selected records.
        type_value: Option<serde_json::Value>,
    },
}

impl RelationSelector {
    fn validate(&self) -> Result<()> {
        match self {
            Self::MarkdownLinks { include, .. } => {
                if let Some(include) = include {
                    let _ = compile_globs(std::slice::from_ref(include))?;
                }
            }
            Self::Frontmatter {
                array_pointer,
                target_pointer,
                type_pointer,
                type_value,
            } => {
                for pointer in [
                    Some(array_pointer),
                    Some(target_pointer),
                    type_pointer.as_ref(),
                ]
                .into_iter()
                .flatten()
                {
                    validate_json_pointer(pointer, "relation")?;
                }
                if type_value.is_some() != type_pointer.is_some() {
                    bail!("frontmatter relation type_pointer and type_value must be set together");
                }
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
/// Authored link syntax used by a relation selector.
pub enum RelationLinkSyntax {
    /// Standard Markdown link.
    Markdown,
    /// Configured wiki-link form.
    Wiki,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Controls structure-aware Markdown chunking.
pub struct ChunkConfig {
    #[serde(default = "default_chunk_tokens")]
    /// Approximate target chunk size in tokens.
    pub target_tokens: usize,
    #[serde(default = "default_overlap_percent")]
    /// Percentage of the prior excerpt carried into embedding context.
    pub overlap_percent: usize,
    #[serde(default = "default_max_chars")]
    /// Hard character ceiling for each embedding input.
    pub max_chars: usize,
    #[serde(default)]
    /// Heading texts whose complete sections are excluded.
    pub exclude_sections: Vec<String>,
    #[serde(default)]
    /// Frontmatter JSON pointers prepended to embedding context.
    pub context_pointers: Vec<String>,
}

impl Default for ChunkConfig {
    fn default() -> Self {
        Self {
            target_tokens: default_chunk_tokens(),
            overlap_percent: default_overlap_percent(),
            max_chars: default_max_chars(),
            exclude_sections: Vec::new(),
            context_pointers: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Controls retrieval, fusion, graph signals, and result counts.
pub struct SearchConfig {
    #[serde(default = "default_limit")]
    /// Maximum number of returned results.
    pub limit: usize,
    #[serde(default = "default_candidates")]
    /// Candidate count retained before reranking.
    pub candidates: usize,
    #[serde(default = "default_rrf_k")]
    /// Reciprocal-rank-fusion offset.
    pub rrf_k: f64,
    #[serde(default = "default_graph_weight")]
    /// Weight applied to degree-normalized graph neighbors.
    pub graph_weight: f64,
}

fn default_graph_weight() -> f64 {
    0.15
}

impl Default for SearchConfig {
    fn default() -> Self {
        Self {
            limit: default_limit(),
            candidates: default_candidates(),
            rrf_k: default_rrf_k(),
            graph_weight: default_graph_weight(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Configures the external embedding and reranking provider.
pub struct ProviderConfig {
    #[serde(default = "default_base_url")]
    /// Provider API base URL.
    pub base_url: String,
    #[serde(default = "default_api_key_env")]
    /// Environment variable containing the API key.
    pub api_key_env: String,
    #[serde(default = "default_embed_model")]
    /// Embedding model identifier.
    pub embedding_model: String,
    #[serde(default = "default_rerank_model")]
    /// Reranking model identifier.
    pub rerank_model: String,
    #[serde(default = "default_dimensions")]
    /// Requested embedding dimensions.
    pub dimensions: usize,
    #[serde(default)]
    /// Optional document embedding batch size.
    pub batch_size: Option<usize>,
    #[serde(default = "default_request_timeout_seconds")]
    /// Per-request network timeout in seconds.
    pub request_timeout_seconds: u64,
}

impl Default for ProviderConfig {
    fn default() -> Self {
        Self {
            base_url: default_base_url(),
            api_key_env: default_api_key_env(),
            embedding_model: default_embed_model(),
            rerank_model: default_rerank_model(),
            dimensions: default_dimensions(),
            batch_size: Some(64),
            request_timeout_seconds: default_request_timeout_seconds(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Controls ordered pushes of accepted commits.
pub struct GitConfig {
    #[serde(default = "default_true")]
    /// Pushes accepted commits when true.
    pub push: bool,
    #[serde(default)]
    /// Optional remote used to establish or update the upstream.
    pub remote: Option<String>,
    #[serde(default = "default_push_timeout_seconds")]
    /// Maximum synchronous push duration in seconds.
    pub push_timeout_seconds: u64,
}

impl Default for GitConfig {
    fn default() -> Self {
        Self {
            push: true,
            remote: None,
            push_timeout_seconds: default_push_timeout_seconds(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
/// Configures the daemon HTTP listener and authentication source.
pub struct ServerConfig {
    #[serde(default = "default_listen")]
    /// Fixed IP socket address on which the daemon listens.
    pub listen: String,
    #[serde(default)]
    /// Optional environment variable containing the bearer token.
    pub bearer_token_env: Option<String>,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            listen: default_listen(),
            bearer_token_env: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config_with_relations(relations: &str) -> String {
        format!(
            "documents:\n  include: ['**/*.md']\nrelations:\n{relations}\nprovider:\n  dimensions: 2\n"
        )
    }

    #[test]
    fn rejects_git_metadata_and_non_normal_paths() {
        for path in [
            "",
            ".",
            "../note.md",
            ".git/hooks/note.md",
            ".GIT/hooks/note.md",
            "a/.git/note.md",
        ] {
            assert!(validate_repo_path(path).is_err(), "accepted {path:?}");
        }
        assert!(validate_repo_path("notes/page.md").is_ok());
    }

    #[test]
    fn relation_names_and_reciprocals_are_closed() {
        let duplicate = config_with_relations(
            "  - name: related\n    selector: {kind: markdown_links}\n  - name: related\n    selector: {kind: markdown_links}",
        );
        assert!(Config::from_yaml(&duplicate).is_err());
        let unknown = config_with_relations(
            "  - name: parent\n    reciprocal: child\n    selector: {kind: markdown_links}",
        );
        assert!(Config::from_yaml(&unknown).is_err());
    }

    #[test]
    fn search_weights_are_finite_and_non_negative() {
        let config = |search: &str| {
            Config::from_yaml(&format!(
                "documents:\n  include: ['**/*.md']\nsearch:\n{search}\nprovider:\n  dimensions: 2\n"
            ))
        };
        assert!(config("  rrf_k: 0").is_err());
        assert!(config("  rrf_k: .inf").is_err());
        assert!(config("  graph_weight: -0.1").is_err());
        assert!(config("  graph_weight: .nan").is_err());
        assert!(config("  rrf_k: 60\n  graph_weight: 0").is_ok());
    }

    #[test]
    fn git_push_timeout_must_be_nonzero() {
        let error = Config::from_yaml(
            "documents:\n  include: ['**/*.md']\nprovider:\n  dimensions: 2\ngit:\n  push_timeout_seconds: 0\n",
        )
        .unwrap_err();
        assert!(error.to_string().contains("git.push_timeout_seconds"));
    }

    #[test]
    fn chunk_context_pointers_are_validated() {
        let config = |pointer: &str| {
            Config::from_yaml(&format!(
                "documents:\n  include: ['**/*.md']\nchunking:\n  context_pointers: [{pointer:?}]\nprovider:\n  dimensions: 2\n"
            ))
        };
        assert!(config("/title").is_ok());
        assert!(config("/escaped~0name/~1path").is_ok());
        assert!(config("title").is_err());
        assert!(config("/bad~2escape").is_err());
        assert!(config("/trailing~").is_err());
    }

    #[test]
    fn section_bounds_are_coherent() {
        let config = |section: &str| {
            Config::from_yaml(&format!(
                "documents:\n  include: ['**/*.md']\nsections:\n  - heading: Notes\n{section}\nprovider:\n  dimensions: 2\n"
            ))
        };
        assert!(config("    required: true\n    minimum: 0\n    maximum: 1").is_ok());
        assert!(config("    minimum: 2\n    maximum: 1").is_err());
        assert!(config("    required: true\n    maximum: 0").is_err());
    }

    #[test]
    fn wiki_link_patterns_define_their_grammar() {
        let config = |wiki: &str| {
            Config::from_yaml(&format!(
                "documents:\n  include: ['**/*.md']\nlinks:\n  wiki: [{wiki:?}]\nprovider:\n  dimensions: 2\n"
            ))
        };
        assert!(config(r"\{\{(?P<target>[^}]+)\}\}").is_ok());
        assert!(config("[").is_err());
        assert!(config(r"\{\{([^}]+)\}\}").is_err());
    }

    #[test]
    fn server_listener_is_a_fixed_socket_address() {
        let config = |listen: &str| {
            Config::from_yaml(&format!(
                "documents:\n  include: ['**/*.md']\nprovider:\n  dimensions: 2\nserver:\n  listen: {listen}\n"
            ))
        };
        assert!(config("127.0.0.1:3131").is_ok());
        assert!(config("'[::1]:3131'").is_ok());
        assert!(config("127.0.0.1:0").is_err());
        assert!(config("localhost:3131").is_err());
    }
}
