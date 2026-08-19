use std::{
    collections::{BTreeSet, HashMap},
    fs,
    path::{Path, PathBuf},
    sync::Arc,
};

use anyhow::{Context, Result, anyhow, bail};
use fs2::FileExt;
use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::{
    chunk::chunk_page,
    config::{Config, validate_repo_path},
    git::{self, PushState},
    hashline::{EditOperation, apply_operations, render},
    markdown::{Edge, Finding, ParsedPage, project_metadata, validate_corpus},
    provider::{InputType, RetrievalProvider, ZeroEntropyProvider},
    search::{SearchIndex, SearchResponse},
    sidecar::{self, Sidecar},
};

pub struct Store {
    root: PathBuf,
    config: RwLock<Config>,
    pages: RwLock<HashMap<String, String>>,
    parsed: RwLock<HashMap<String, ParsedPage>>,
    edges: RwLock<Vec<Edge>>,
    index: RwLock<SearchIndex>,
    provider: Arc<dyn RetrievalProvider>,
    git_dir: PathBuf,
    head: RwLock<String>,
    blocked: RwLock<Option<String>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApplyEditsRequest {
    pub edit_summary: String,
    pub edits: Vec<EditOperation>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ApplyEditsResponse {
    pub status: ApplyStatus,
    pub push: PushState,
    pub touched_paths: Vec<String>,
    pub fresh_hashlines: HashMap<String, String>,
    pub validation_findings: Vec<Finding>,
    pub embedding_state: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ApplyStatus {
    Accepted,
    AlreadyApplied,
}

#[derive(Debug, Clone, Serialize)]
pub struct PageResponse {
    pub path: String,
    pub content: String,
    pub metadata: serde_json::Value,
    pub relations: Vec<Edge>,
}

#[derive(Debug, Clone, Serialize)]
pub struct StatusResponse {
    pub pages: usize,
    pub chunks: usize,
    pub vectors_ready: usize,
    pub vectors_total: usize,
    pub unpushed: bool,
    pub blocked: Option<String>,
}

impl Store {
    pub fn open(root: impl AsRef<Path>) -> Result<Arc<Self>> {
        let root = root
            .as_ref()
            .canonicalize()
            .context("resolve repository root")?;
        git::ensure_repository(&root)?;
        let git_dir = git::git_dir(&root)?;
        git::recover_worktree(&root)?;
        if !git::is_tracked(&root, ".mdstore/config.yaml")? {
            bail!(".mdstore/config.yaml must be tracked by Git");
        }
        let config = Config::load(&root)?;
        let provider = Arc::new(ZeroEntropyProvider::new(config.provider.clone()));
        Self::open_with_provider(root, config, provider, git_dir)
    }

    pub fn open_with_provider(
        root: PathBuf,
        config: Config,
        provider: Arc<dyn RetrievalProvider>,
        git_dir: PathBuf,
    ) -> Result<Arc<Self>> {
        let pages = load_pages(&root, &config)?;
        let extra = load_config_files(&root)?;
        let (parsed, edges) =
            validate_corpus(&root, &config, &pages, &extra).map_err(|findings| {
                anyhow!(serde_json::to_string_pretty(&findings).unwrap_or_default())
            })?;
        let index = build_index(&root, &config, &pages, &parsed, &edges);
        let head = git::head(&root)?;
        Ok(Arc::new(Self {
            root,
            config: RwLock::new(config),
            pages: RwLock::new(pages),
            parsed: RwLock::new(parsed),
            edges: RwLock::new(edges),
            index: RwLock::new(index),
            provider,
            git_dir,
            head: RwLock::new(head),
            blocked: RwLock::new(None),
        }))
    }

    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    #[must_use]
    pub fn config(&self) -> Config {
        self.config.read().clone()
    }

    pub fn validate(&self) -> std::result::Result<(), Vec<Finding>> {
        let config = self.config.read().clone();
        let pages = self.pages.read().clone();
        let extra = load_config_files(&self.root).unwrap_or_default();
        validate_corpus(&self.root, &config, &pages, &extra).map(|_| ())
    }

    pub fn get_page(&self, path: &str, window: Option<(usize, usize)>) -> Result<PageResponse> {
        validate_repo_path(path)?;
        let pages = self.pages.read();
        if let Some(text) = pages.get(path) {
            let parsed = self.parsed.read();
            let page = parsed.get(path).context("page was not parsed")?;
            let config = self.config.read();
            return Ok(PageResponse {
                path: path.into(),
                content: render(text, window),
                metadata: project_metadata(&config, &page.frontmatter),
                relations: self
                    .edges
                    .read()
                    .iter()
                    .filter(|edge| edge.source == path || edge.target == path)
                    .cloned()
                    .collect(),
            });
        }
        drop(pages);
        let config_resource = path.starts_with(".mdstore/")
            && Path::new(path).extension().is_some_and(|extension| {
                extension.eq_ignore_ascii_case("yaml")
                    || extension.eq_ignore_ascii_case("yml")
                    || extension.eq_ignore_ascii_case("json")
            });
        if config_resource && git::is_tracked(&self.root, path)? {
            let text = fs::read_to_string(self.root.join(path))?;
            return Ok(PageResponse {
                path: path.into(),
                content: render(&text, window),
                metadata: serde_json::json!({}),
                relations: Vec::new(),
            });
        }
        bail!("page or configuration resource not found: {path}")
    }

    pub async fn search(&self, query: &str, variants: &[String]) -> Result<SearchResponse> {
        if query.trim().is_empty() {
            bail!("query must be non-empty");
        }
        let config = self.config.read().clone();
        let index = self.index.read().clone();
        Ok(index
            .search(&config, self.provider.as_ref(), query, variants)
            .await)
    }

    pub fn apply_edits(&self, request: &ApplyEditsRequest) -> Result<ApplyEditsResponse> {
        if request.edit_summary.trim().is_empty() {
            bail!("edit_summary must be non-empty");
        }
        if request.edits.is_empty() {
            bail!("edits must contain at least one operation");
        }
        if let Some(reason) = self.blocked.read().clone() {
            bail!("writes are blocked: {reason}");
        }
        let digest = request_digest(request)?;
        let lock_path = self.git_dir.join("mdstore/write.lock");
        fs::create_dir_all(lock_path.parent().expect("lock parent"))?;
        let lock = fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(false)
            .open(lock_path)?;
        lock.lock_exclusive()?;
        git::recover_worktree(&self.root)?;
        self.refresh_external_commit()?;
        if let Some(response) = self.read_receipt(&digest)? {
            return Ok(response);
        }

        let mut paths = BTreeSet::new();
        let mut has_config = false;
        let mut has_content = false;
        for edit in &request.edits {
            let path = edit.path();
            validate_repo_path(path)?;
            if path.ends_with(".mdstore") {
                bail!("embedding sidecars cannot be edited");
            }
            if path.starts_with(".mdstore/") {
                has_config = true;
                if !(path.ends_with(".yaml") || path.ends_with(".yml") || path.ends_with(".json")) {
                    bail!("configuration edits are limited to YAML and JSON files");
                }
            } else if path.ends_with(".md") {
                has_content = true;
            } else {
                bail!("edits may target configured Markdown or .mdstore YAML/JSON only");
            }
            paths.insert(path.to_owned());
        }
        if has_config && has_content {
            bail!("configuration and Markdown edits must be separate batches");
        }

        let mut originals = HashMap::new();
        for path in &paths {
            if let Ok(text) = fs::read_to_string(self.root.join(path)) {
                originals.insert(path.clone(), text);
            }
        }
        let changes = apply_operations(&originals, &request.edits)?;
        let mut pages = self.pages.read().clone();
        let mut extra = load_config_files(&self.root)?;
        for (path, content) in &changes {
            if path.ends_with(".md") && !path.starts_with(".mdstore/") {
                match content {
                    Some(text) => {
                        pages.insert(path.clone(), text.clone());
                    }
                    None => {
                        pages.remove(path);
                    }
                }
            } else {
                match content {
                    Some(text) => {
                        extra.insert(path.clone(), text.clone());
                    }
                    None => {
                        extra.remove(path);
                    }
                }
            }
        }
        let config = if let Some(text) = extra.get(".mdstore/config.yaml") {
            Config::from_yaml(text)?
        } else {
            bail!(".mdstore/config.yaml cannot be removed");
        };
        if has_config {
            pages = load_pages(&self.root, &config)?;
        }
        ensure_pages_match_config(&config, &pages)?;
        let (parsed, edges) = match validate_corpus(&self.root, &config, &pages, &extra) {
            Ok(value) => value,
            Err(findings) => {
                bail!(
                    "validation failed:\n{}",
                    serde_json::to_string_pretty(&findings)?
                );
            }
        };

        let ordered: Vec<(String, Option<String>)> = paths
            .iter()
            .map(|path| (path.clone(), changes.get(path).cloned().flatten()))
            .collect();
        let originally_present: Vec<String> = originals.keys().cloned().collect();
        git::write_changes(&self.root, &ordered)?;
        let path_list: Vec<String> = paths.iter().cloned().collect();
        let committed = match git::commit(&self.root, request.edit_summary.trim(), &path_list) {
            Ok(value) => value,
            Err(error) => {
                let _ = git::rollback(&self.root, &path_list, &originally_present);
                return Err(error);
            }
        };
        if committed {
            *self.head.write() = git::head(&self.root)?;
        }
        let push = if committed {
            git::push(&self.root, &config)?
        } else {
            PushState::Disabled
        };
        if matches!(push, PushState::Diverged) {
            *self.blocked.write() = Some("remote history diverged".into());
        }
        let index = build_index(&self.root, &config, &pages, &parsed, &edges);
        *self.config.write() = config;
        *self.pages.write() = pages.clone();
        *self.parsed.write() = parsed;
        *self.edges.write() = edges;
        *self.index.write() = index;
        let fresh_hashlines = path_list
            .iter()
            .filter_map(|path| {
                pages
                    .get(path)
                    .map(|text| (path.clone(), compact_fresh(text)))
            })
            .collect();
        let response = ApplyEditsResponse {
            status: if committed {
                ApplyStatus::Accepted
            } else {
                ApplyStatus::AlreadyApplied
            },
            push,
            touched_paths: path_list,
            fresh_hashlines,
            validation_findings: Vec::new(),
            embedding_state: "pending".into(),
        };
        self.write_receipt(&digest, &response)?;
        Ok(response)
    }

    pub async fn reindex(&self) -> Result<()> {
        let paths: Vec<String> = self.pages.read().keys().cloned().collect();
        self.reindex_paths(&paths).await
    }

    pub async fn reindex_after_changes(&self, paths: &[String]) -> Result<()> {
        if paths.iter().any(|path| path.starts_with(".mdstore/")) {
            self.reindex().await
        } else {
            self.reindex_paths(paths).await
        }
    }

    pub async fn reindex_paths(&self, paths: &[String]) -> Result<()> {
        let config = self.config.read().clone();
        let pages = self.pages.read().clone();
        let parsed = self.parsed.read().clone();
        for path in paths {
            let Some(text) = pages.get(path) else {
                let path = sidecar::sidecar_path(&self.root.join(path));
                if path.exists() {
                    fs::remove_file(path)?;
                }
                continue;
            };
            let page = parsed.get(path).context("missing parsed page")?;
            let context = embedding_context(&config, page);
            let chunks = chunk_page(text, page, &config.chunking, &context);
            let mut vectors = Vec::new();
            let batch_size = config.provider.batch_size.unwrap_or(64).max(1);
            for batch in chunks.chunks(batch_size) {
                let input: Vec<String> = batch
                    .iter()
                    .map(|chunk| chunk.embedding_text.clone())
                    .collect();
                vectors.extend(self.provider.embed(InputType::Document, &input).await?);
            }
            let sidecar = Sidecar::new(
                text,
                self.provider.model(),
                self.provider.dimensions(),
                &chunks,
                &vectors,
            );
            let sidecar_path = sidecar::sidecar_path(&self.root.join(path));
            let relative_sidecar = sidecar_path
                .strip_prefix(&self.root)?
                .to_string_lossy()
                .replace('\\', "/");
            if !git::is_ignored(&self.root, &relative_sidecar)? {
                bail!("embedding sidecar is not ignored by Git: {relative_sidecar}");
            }
            sidecar::write_atomic(&sidecar_path, &sidecar)?;
        }
        let index = build_index(&self.root, &config, &pages, &parsed, &self.edges.read());
        *self.index.write() = index;
        Ok(())
    }

    pub fn status(&self) -> Result<StatusResponse> {
        let index = self.index.read();
        Ok(StatusResponse {
            pages: self.pages.read().len(),
            chunks: index.chunks.len(),
            vectors_ready: index
                .chunks
                .iter()
                .filter(|chunk| chunk.vector.is_some())
                .count(),
            vectors_total: index.chunks.len(),
            unpushed: git::has_unpushed(&self.root)?,
            blocked: self.blocked.read().clone(),
        })
    }

    pub fn push(&self) -> Result<PushState> {
        let state = git::push(&self.root, &self.config.read())?;
        if matches!(state, PushState::Pushed) {
            *self.blocked.write() = None;
        } else if matches!(state, PushState::Diverged) {
            *self.blocked.write() = Some("remote history diverged".into());
        }
        Ok(state)
    }

    fn refresh_external_commit(&self) -> Result<()> {
        let current = git::head(&self.root)?;
        if current == *self.head.read() {
            return Ok(());
        }
        let config = Config::load(&self.root)?;
        let pages = load_pages(&self.root, &config)?;
        let extra = load_config_files(&self.root)?;
        let (parsed, edges) = match validate_corpus(&self.root, &config, &pages, &extra) {
            Ok(value) => value,
            Err(findings) => {
                let reason = format!(
                    "external commit is invalid:\n{}",
                    serde_json::to_string_pretty(&findings).unwrap_or_default()
                );
                *self.blocked.write() = Some(reason.clone());
                bail!(reason);
            }
        };
        let index = build_index(&self.root, &config, &pages, &parsed, &edges);
        *self.config.write() = config;
        *self.pages.write() = pages;
        *self.parsed.write() = parsed;
        *self.edges.write() = edges;
        *self.index.write() = index;
        *self.head.write() = current;
        Ok(())
    }

    fn receipt_path(&self, digest: &str) -> PathBuf {
        self.git_dir
            .join("mdstore/receipts")
            .join(format!("{digest}.json"))
    }

    fn read_receipt(&self, digest: &str) -> Result<Option<ApplyEditsResponse>> {
        let path = self.receipt_path(digest);
        if !path.exists() {
            return Ok(None);
        }
        #[derive(Deserialize)]
        struct StoredReceipt {
            touched_paths: Vec<String>,
            fresh_hashlines: HashMap<String, String>,
        }
        let stored: StoredReceipt = serde_json::from_slice(&fs::read(path)?)?;
        Ok(Some(ApplyEditsResponse {
            status: ApplyStatus::AlreadyApplied,
            push: if git::has_unpushed(&self.root)? {
                PushState::Queued
            } else {
                PushState::Pushed
            },
            touched_paths: stored.touched_paths,
            fresh_hashlines: stored.fresh_hashlines,
            validation_findings: Vec::new(),
            embedding_state: "pending_or_ready".into(),
        }))
    }

    fn write_receipt(&self, digest: &str, response: &ApplyEditsResponse) -> Result<()> {
        let path = self.receipt_path(digest);
        fs::create_dir_all(path.parent().expect("receipt parent"))?;
        let value = serde_json::json!({
            "touched_paths": response.touched_paths,
            "fresh_hashlines": response.fresh_hashlines,
        });
        fs::write(path, serde_json::to_vec(&value)?)?;
        Ok(())
    }
}

fn load_pages(root: &Path, config: &Config) -> Result<HashMap<String, String>> {
    git::tracked_markdown(root, config)?
        .into_iter()
        .map(|path| {
            fs::read_to_string(root.join(&path))
                .with_context(|| format!("read tracked Markdown {path}"))
                .map(|text| (path, text))
        })
        .collect()
}

fn load_config_files(root: &Path) -> Result<HashMap<String, String>> {
    git::tracked_config_files(root)?
        .into_iter()
        .map(|path| {
            fs::read_to_string(root.join(&path))
                .map(|text| (path, text))
                .map_err(Into::into)
        })
        .collect()
}

fn ensure_pages_match_config(config: &Config, pages: &HashMap<String, String>) -> Result<()> {
    let (include, exclude) = config.document_globs()?;
    for path in pages.keys() {
        if !include.is_match(path) || exclude.is_match(path) {
            bail!("edited page is outside configured document globs: {path}");
        }
    }
    Ok(())
}

fn build_index(
    root: &Path,
    config: &Config,
    pages: &HashMap<String, String>,
    parsed: &HashMap<String, ParsedPage>,
    edges: &[Edge],
) -> SearchIndex {
    let mut all_chunks = HashMap::new();
    for (path, text) in pages {
        let Some(page) = parsed.get(path) else {
            continue;
        };
        let context = embedding_context(config, page);
        let chunks = chunk_page(text, page, &config.chunking, &context);
        let vectors = sidecar::read(&sidecar::sidecar_path(&root.join(path)))
            .ok()
            .and_then(|stored| {
                stored.vectors_for(
                    text,
                    &config.provider.embedding_model,
                    config.provider.dimensions,
                    &chunks,
                )
            });
        let values = chunks
            .into_iter()
            .enumerate()
            .map(|(index, chunk)| {
                let vector = vectors
                    .as_ref()
                    .and_then(|vectors| vectors.get(index))
                    .cloned();
                (chunk, vector)
            })
            .collect();
        all_chunks.insert(path.clone(), values);
    }
    SearchIndex::build(config, pages, parsed, edges, all_chunks)
}

fn embedding_context(config: &Config, page: &ParsedPage) -> Vec<String> {
    config
        .chunking
        .context_pointers
        .iter()
        .filter_map(|pointer| page.frontmatter.pointer(pointer))
        .filter_map(|value| match value {
            serde_json::Value::String(value) => Some(value.clone()),
            serde_json::Value::Number(value) => Some(value.to_string()),
            _ => None,
        })
        .collect()
}

fn compact_fresh(text: &str) -> String {
    let count = text.lines().count();
    if count <= 24 {
        render(text, None)
    } else {
        format!(
            "{}\n…\n{}",
            render(text, Some((1, 12))),
            render(text, Some((count - 11, count)))
        )
    }
}

fn request_digest(request: &ApplyEditsRequest) -> Result<String> {
    let bytes = serde_json::to_vec(request)?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}
