use std::{collections::BTreeMap, fs, path::Path};

use anyhow::{Context, Result, bail};
use globset::{Glob, GlobSet, GlobSetBuilder};
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
pub struct Config {
    pub documents: DocumentConfig,
    #[serde(default)]
    pub schemas: Vec<SchemaRule>,
    #[serde(default)]
    pub sections: Vec<SectionRule>,
    #[serde(default)]
    pub metadata: BTreeMap<String, String>,
    #[serde(default)]
    pub links: LinkConfig,
    #[serde(default)]
    pub relations: Vec<RelationRule>,
    #[serde(default)]
    pub chunking: ChunkConfig,
    #[serde(default)]
    pub search: SearchConfig,
    #[serde(default)]
    pub provider: ProviderConfig,
    #[serde(default)]
    pub git: GitConfig,
    #[serde(default)]
    pub server: ServerConfig,
}

impl Config {
    pub fn load(root: &Path) -> Result<Self> {
        let path = root.join(".mdstore/config.yaml");
        let text = fs::read_to_string(&path)
            .with_context(|| format!("read required configuration {}", path.display()))?;
        Self::from_yaml(&text)
    }

    pub fn from_yaml(text: &str) -> Result<Self> {
        let value: Self = serde_yaml::from_str(text).context("parse .mdstore/config.yaml")?;
        value.validate()?;
        Ok(value)
    }

    pub fn validate(&self) -> Result<()> {
        if self.documents.include.is_empty() {
            bail!("documents.include must contain at least one glob");
        }
        let _ = self.document_globs()?;
        for schema in &self.schemas {
            let _ = compile_globs(std::slice::from_ref(&schema.include))?;
            validate_repo_path(&schema.schema)?;
        }
        for pointer in self.metadata.values() {
            if !pointer.starts_with('/') && !pointer.is_empty() {
                bail!("metadata JSON pointer must be empty or start with '/': {pointer}");
            }
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
        if self.provider.dimensions == 0 {
            bail!("provider.dimensions must be non-zero");
        }
        for relation in &self.relations {
            if relation.name.trim().is_empty() {
                bail!("relation names must be non-empty");
            }
            relation.selector.validate()?;
        }
        Ok(())
    }

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

pub fn validate_repo_path(path: &str) -> Result<()> {
    let candidate = Path::new(path);
    if candidate.is_absolute()
        || candidate
            .components()
            .any(|part| matches!(part, std::path::Component::ParentDir))
    {
        bail!("path must stay within the repository: {path}");
    }
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DocumentConfig {
    pub include: Vec<String>,
    #[serde(default)]
    pub exclude: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SchemaRule {
    pub include: String,
    pub schema: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SectionRule {
    pub heading: String,
    #[serde(default)]
    pub required: bool,
    #[serde(default)]
    pub minimum: Option<usize>,
    #[serde(default)]
    pub maximum: Option<usize>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LinkConfig {
    #[serde(default = "default_true")]
    pub markdown: bool,
    #[serde(default)]
    pub wiki: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RelationRule {
    pub name: String,
    #[serde(default)]
    pub reciprocal: Option<String>,
    pub selector: RelationSelector,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum RelationSelector {
    MarkdownLinks,
    Frontmatter {
        array_pointer: String,
        target_pointer: String,
        #[serde(default)]
        type_pointer: Option<String>,
        #[serde(default)]
        type_value: Option<serde_json::Value>,
    },
}

impl RelationSelector {
    fn validate(&self) -> Result<()> {
        if let Self::Frontmatter {
            array_pointer,
            target_pointer,
            type_pointer,
            type_value,
        } = self
        {
            for pointer in [
                Some(array_pointer),
                Some(target_pointer),
                type_pointer.as_ref(),
            ]
            .into_iter()
            .flatten()
            {
                if !pointer.starts_with('/') && !pointer.is_empty() {
                    bail!("relation JSON pointer must be empty or start with '/': {pointer}");
                }
            }
            if type_value.is_some() != type_pointer.is_some() {
                bail!("frontmatter relation type_pointer and type_value must be set together");
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChunkConfig {
    #[serde(default = "default_chunk_tokens")]
    pub target_tokens: usize,
    #[serde(default = "default_overlap_percent")]
    pub overlap_percent: usize,
    #[serde(default = "default_max_chars")]
    pub max_chars: usize,
    #[serde(default)]
    pub exclude_sections: Vec<String>,
    #[serde(default)]
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
pub struct SearchConfig {
    #[serde(default = "default_limit")]
    pub limit: usize,
    #[serde(default = "default_candidates")]
    pub candidates: usize,
    #[serde(default = "default_rrf_k")]
    pub rrf_k: f64,
    #[serde(default = "default_graph_weight")]
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

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderConfig {
    #[serde(default = "default_base_url")]
    pub base_url: String,
    #[serde(default = "default_api_key_env")]
    pub api_key_env: String,
    #[serde(default = "default_embed_model")]
    pub embedding_model: String,
    #[serde(default = "default_rerank_model")]
    pub rerank_model: String,
    #[serde(default = "default_dimensions")]
    pub dimensions: usize,
    #[serde(default)]
    pub batch_size: Option<usize>,
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
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GitConfig {
    #[serde(default = "default_true")]
    pub push: bool,
    #[serde(default)]
    pub remote: Option<String>,
}

impl Default for GitConfig {
    fn default() -> Self {
        Self {
            push: true,
            remote: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerConfig {
    #[serde(default = "default_listen")]
    pub listen: String,
    #[serde(default)]
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
