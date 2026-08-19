use std::{
    collections::{BTreeSet, HashMap},
    fs,
    io::Write,
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
    config::{
        Config, ProviderConfig, ensure_repository_path_safe, read_repository_text,
        validate_repo_path,
    },
    git::{self, PushState},
    hashline::{ChangedRange, EditOperation, apply_operations_with_ranges, render},
    markdown::{Edge, Finding, ParsedPage, project_metadata, validate_corpus},
    provider::{InputType, RetrievalProvider, ZeroEntropyProvider},
    search::{SearchIndex, SearchResponse},
    sidecar::{self, Sidecar},
};

pub struct Store {
    root: PathBuf,
    state: RwLock<StoreState>,
    provider_factory: Option<fn(ProviderConfig) -> Arc<dyn RetrievalProvider>>,
    reindex_lock: tokio::sync::Mutex<()>,
    git_dir: PathBuf,
    head: RwLock<String>,
    blocked: RwLock<Option<String>>,
}

#[derive(Clone)]
struct StoreState {
    config: Config,
    config_files: HashMap<String, String>,
    pages: HashMap<String, String>,
    parsed: HashMap<String, ParsedPage>,
    edges: Vec<Edge>,
    index: Arc<SearchIndex>,
    provider: Arc<dyn RetrievalProvider>,
    generation: u64,
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

#[derive(Serialize, Deserialize)]
struct PendingReceipt {
    base_head: String,
    tree: String,
    touched_paths: Vec<String>,
    fresh_hashlines: HashMap<String, String>,
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
        let provider = zeroentropy_provider(config.provider.clone());
        Self::open_inner(root, config, provider, git_dir, Some(zeroentropy_provider))
    }

    pub fn open_with_provider(
        root: PathBuf,
        config: Config,
        provider: Arc<dyn RetrievalProvider>,
        git_dir: PathBuf,
    ) -> Result<Arc<Self>> {
        Self::open_inner(root, config, provider, git_dir, None)
    }

    fn open_inner(
        root: PathBuf,
        config: Config,
        provider: Arc<dyn RetrievalProvider>,
        git_dir: PathBuf,
        provider_factory: Option<fn(ProviderConfig) -> Arc<dyn RetrievalProvider>>,
    ) -> Result<Arc<Self>> {
        let pages = load_pages(&root, &config)?;
        let extra = load_config_files(&root)?;
        let (parsed, edges) = validate_corpus(&config, &pages, &extra).map_err(|findings| {
            anyhow!(serde_json::to_string_pretty(&findings).unwrap_or_default())
        })?;
        ensure_sidecars_ignored(&root, pages.keys().map(String::as_str))?;
        let index = build_index(&root, &config, &pages, &parsed, &edges, provider.as_ref());
        let head = git::head(&root)?;
        let blocked = read_blocked(&git_dir)?;
        Ok(Arc::new(Self {
            root,
            state: RwLock::new(StoreState {
                config,
                config_files: extra,
                pages,
                parsed,
                edges,
                index: Arc::new(index),
                provider,
                generation: 0,
            }),
            provider_factory,
            reindex_lock: tokio::sync::Mutex::new(()),
            git_dir,
            head: RwLock::new(head),
            blocked: RwLock::new(blocked),
        }))
    }

    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    #[must_use]
    pub fn config(&self) -> Config {
        self.state.read().config.clone()
    }

    pub fn validate(&self) -> std::result::Result<(), Vec<Finding>> {
        let state = self.state.read();
        validate_corpus(&state.config, &state.pages, &state.config_files).map(|_| ())
    }

    pub fn get_page(&self, path: &str, window: Option<(usize, usize)>) -> Result<PageResponse> {
        validate_repo_path(path)?;
        let state = self.state.read();
        if let Some(text) = state.pages.get(path) {
            let page = state.parsed.get(path).context("page was not parsed")?;
            return Ok(PageResponse {
                path: path.into(),
                content: render(text, window),
                metadata: project_metadata(&state.config, &page.frontmatter),
                relations: state
                    .edges
                    .iter()
                    .filter(|edge| edge.source == path || edge.target == path)
                    .cloned()
                    .collect(),
            });
        }
        let config_resource = path.starts_with(".mdstore/")
            && Path::new(path).extension().is_some_and(|extension| {
                extension.eq_ignore_ascii_case("yaml")
                    || extension.eq_ignore_ascii_case("yml")
                    || extension.eq_ignore_ascii_case("json")
            });
        if config_resource && let Some(text) = state.config_files.get(path) {
            return Ok(PageResponse {
                path: path.into(),
                content: render(text, window),
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
        let (config, index, provider) = {
            let state = self.state.read();
            (
                state.config.clone(),
                state.index.clone(),
                state.provider.clone(),
            )
        };
        Ok(index
            .search(&config, provider.as_ref(), query, variants)
            .await)
    }

    pub fn apply_edits(&self, request: &ApplyEditsRequest) -> Result<ApplyEditsResponse> {
        if request.edit_summary.trim().is_empty() {
            bail!("edit_summary must be non-empty");
        }
        if request.edits.is_empty() {
            bail!("edits must contain at least one operation");
        }
        let digest = request_digest(request)?;
        let _lock = self.lock_repository()?;
        git::recover_worktree(&self.root)?;
        self.refresh_external_commit()?;
        if let Some(response) = self.read_receipt(&digest)? {
            return Ok(response);
        }
        if let Some(response) = self.recover_pending(&digest)? {
            return Ok(response);
        }
        if let Some(reason) = self.blocked.read().clone() {
            bail!("writes are blocked: {reason}");
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

        let current = self.state.read().clone();
        let base_head = self.head.read().clone();
        if has_content {
            ensure_paths_match_config(&current.config, paths.iter())?;
        }

        let mut originals = HashMap::new();
        for path in &paths {
            ensure_repository_path_safe(&self.root, path)?;
            if self.root.join(path).exists() {
                originals.insert(path.clone(), read_repository_text(&self.root, path)?);
            }
        }
        let applied = apply_operations_with_ranges(&originals, &request.edits)?;
        let changes = &applied.changes;
        let mut pages = current.pages.clone();
        let mut extra = current.config_files.clone();
        for (path, content) in changes {
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
        if config.server != current.config.server {
            bail!("server configuration changes require a daemon restart");
        }
        if has_config {
            pages = load_pages(&self.root, &config)?;
        }
        ensure_pages_match_config(&config, &pages)?;
        let (parsed, edges) = match validate_corpus(&config, &pages, &extra) {
            Ok(value) => value,
            Err(findings) => {
                bail!(
                    "validation failed:\n{}",
                    serde_json::to_string_pretty(&findings)?
                );
            }
        };
        ensure_sidecars_ignored(&self.root, pages.keys().map(String::as_str))?;
        let provider = if config.provider != current.config.provider {
            let factory = self
                .provider_factory
                .context("provider configuration changes require a daemon restart")?;
            factory(config.provider.clone())
        } else {
            current.provider.clone()
        };

        let ordered: Vec<(String, Option<String>)> = paths
            .iter()
            .map(|path| (path.clone(), changes.get(path).cloned().flatten()))
            .collect();
        let path_list: Vec<String> = paths.iter().cloned().collect();
        let fresh_hashlines = fresh_windows(changes, &applied.changed_ranges);
        if let Err(error) = git::write_changes(&self.root, &ordered) {
            return Err(rollback_failure(&self.root, &path_list, error));
        }
        let tree = match git::stage_tree(&self.root, &path_list) {
            Ok(value) => value,
            Err(error) => {
                return Err(rollback_failure(&self.root, &path_list, error));
            }
        };
        let committed = tree.is_some();
        if let Some(tree) = tree {
            let pending = PendingReceipt {
                base_head,
                tree,
                touched_paths: path_list.clone(),
                fresh_hashlines: fresh_hashlines.clone(),
            };
            if let Err(error) = self.write_pending(&digest, &pending) {
                return Err(rollback_failure(&self.root, &path_list, error));
            }
            if let Err(error) = git::commit_tree(
                &self.root,
                &pending.tree,
                &pending.base_head,
                &request.edit_summary,
            ) {
                let error = rollback_failure(&self.root, &path_list, error);
                return match self.remove_pending(&digest) {
                    Ok(()) => Err(error),
                    Err(cleanup) => Err(anyhow!("{error}; pending cleanup also failed: {cleanup}")),
                };
            }
        }
        if committed {
            *self.head.write() = git::head(&self.root)?;
        }
        let push = if committed {
            git::push(&self.root, &config)?
        } else {
            PushState::Disabled
        };
        if matches!(push, PushState::Diverged) {
            self.set_blocked(Some("remote history diverged".into()))?;
        }
        let index = build_index(
            &self.root,
            &config,
            &pages,
            &parsed,
            &edges,
            provider.as_ref(),
        );
        *self.state.write() = StoreState {
            config,
            config_files: extra,
            pages,
            parsed,
            edges,
            index: Arc::new(index),
            provider,
            generation: current.generation + 1,
        };
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
        self.remove_pending(&digest)?;
        Ok(response)
    }

    pub async fn reindex(&self) -> Result<()> {
        let paths: Vec<String> = self.state.read().pages.keys().cloned().collect();
        self.reindex_paths_mode(&paths, true).await
    }

    pub async fn reindex_missing(&self) -> Result<()> {
        let paths: Vec<String> = self.state.read().pages.keys().cloned().collect();
        self.reindex_paths_mode(&paths, false).await
    }

    pub async fn reindex_after_changes(&self, paths: &[String]) -> Result<()> {
        if paths.iter().any(|path| path.starts_with(".mdstore/")) {
            self.reindex_missing().await
        } else {
            self.reindex_paths_mode(paths, false).await
        }
    }

    pub async fn reindex_paths(&self, paths: &[String]) -> Result<()> {
        self.reindex_paths_mode(paths, true).await
    }

    async fn reindex_paths_mode(&self, paths: &[String], force: bool) -> Result<()> {
        let _guard = self.reindex_lock.lock().await;
        let snapshot = self.state.read().clone();
        let generation = snapshot.generation;
        let config = snapshot.config;
        let pages = snapshot.pages;
        let parsed = snapshot.parsed;
        let edges = snapshot.edges;
        let provider = snapshot.provider;
        let provider_identity = provider.embedding_provider_identity();
        ensure_sidecars_ignored(
            &self.root,
            pages
                .keys()
                .map(String::as_str)
                .chain(paths.iter().map(String::as_str)),
        )?;
        for path in paths {
            let Some(text) = pages.get(path) else {
                let path = sidecar::sidecar_path(&self.root.join(path));
                if self.state.read().generation != generation {
                    return Ok(());
                }
                if path.exists() {
                    fs::remove_file(path)?;
                }
                continue;
            };
            let page = parsed.get(path).context("missing parsed page")?;
            let context = embedding_context(&config, page);
            let chunks = chunk_page(text, page, &config.chunking, &context);
            let sidecar_path = sidecar::sidecar_path(&self.root.join(path));
            if !force
                && sidecar::read(&sidecar_path).ok().is_some_and(|stored| {
                    stored
                        .vectors_for(
                            text,
                            &provider_identity,
                            provider.model(),
                            provider.dimensions(),
                            &chunks,
                        )
                        .is_some()
                })
            {
                continue;
            }
            let mut vectors = Vec::new();
            let batch_size = config.provider.batch_size.unwrap_or(64).max(1);
            for batch in chunks.chunks(batch_size) {
                let input: Vec<String> = batch
                    .iter()
                    .map(|chunk| chunk.embedding_text.clone())
                    .collect();
                vectors.extend(provider.embed(InputType::Document, &input).await?);
                if self.state.read().generation != generation {
                    return Ok(());
                }
            }
            let sidecar = Sidecar::new(
                text,
                &provider_identity,
                provider.model(),
                provider.dimensions(),
                &chunks,
                &vectors,
            );
            if self.state.read().generation != generation {
                return Ok(());
            }
            sidecar::write_atomic(&sidecar_path, &sidecar)?;
        }
        let index = build_index(
            &self.root,
            &config,
            &pages,
            &parsed,
            &edges,
            provider.as_ref(),
        );
        let mut current = self.state.write();
        if current.generation == generation {
            current.index = Arc::new(index);
        }
        Ok(())
    }

    pub fn status(&self) -> Result<StatusResponse> {
        let state = self.state.read();
        Ok(StatusResponse {
            pages: state.pages.len(),
            chunks: state.index.chunks.len(),
            vectors_ready: state
                .index
                .chunks
                .iter()
                .filter(|chunk| chunk.vector.is_some())
                .count(),
            vectors_total: state.index.chunks.len(),
            unpushed: git::has_unpushed(&self.root)?,
            blocked: self.blocked.read().clone(),
        })
    }

    pub fn push(&self) -> Result<PushState> {
        let _lock = self.lock_repository()?;
        git::recover_worktree(&self.root)?;
        self.refresh_external_commit()?;
        let state = git::push(&self.root, &self.state.read().config)?;
        if matches!(state, PushState::Pushed) {
            self.set_blocked(None)?;
        } else if matches!(state, PushState::Diverged) {
            self.set_blocked(Some("remote history diverged".into()))?;
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
        let (parsed, edges) = match validate_corpus(&config, &pages, &extra) {
            Ok(value) => value,
            Err(findings) => {
                let reason = format!(
                    "external commit is invalid:\n{}",
                    serde_json::to_string_pretty(&findings).unwrap_or_default()
                );
                self.set_external_blocked(reason.clone())?;
                bail!(reason);
            }
        };
        ensure_sidecars_ignored(&self.root, pages.keys().map(String::as_str))?;
        let current_state = self.state.read().clone();
        if config.server != current_state.config.server {
            bail!("external commit changes server configuration; restart required");
        }
        let provider = if config.provider != current_state.config.provider {
            let Some(factory) = self.provider_factory else {
                let reason = "external commit changes provider configuration; restart required";
                self.set_external_blocked(reason.into())?;
                bail!(reason);
            };
            factory(config.provider.clone())
        } else {
            current_state.provider.clone()
        };
        let index = build_index(
            &self.root,
            &config,
            &pages,
            &parsed,
            &edges,
            provider.as_ref(),
        );
        *self.state.write() = StoreState {
            config,
            config_files: extra,
            pages,
            parsed,
            edges,
            index: Arc::new(index),
            provider,
            generation: current_state.generation + 1,
        };
        *self.head.write() = current;
        if self
            .blocked
            .read()
            .as_deref()
            .is_some_and(|reason| reason.starts_with("external commit"))
        {
            self.set_blocked(None)?;
        }
        Ok(())
    }

    fn lock_repository(&self) -> Result<fs::File> {
        let path = self.git_dir.join("mdstore/write.lock");
        fs::create_dir_all(path.parent().expect("lock parent"))?;
        let lock = fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(false)
            .open(path)?;
        lock.lock_exclusive()?;
        Ok(lock)
    }

    fn set_external_blocked(&self, reason: String) -> Result<()> {
        if self.blocked.read().as_deref() == Some("remote history diverged") {
            return Ok(());
        }
        self.set_blocked(Some(reason))
    }

    fn receipt_path(&self, digest: &str) -> PathBuf {
        self.git_dir
            .join("mdstore/receipts")
            .join(format!("{digest}.json"))
    }

    fn pending_path(&self, digest: &str) -> PathBuf {
        self.git_dir
            .join("mdstore/pending")
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
            push: self.current_push_state()?,
            touched_paths: stored.touched_paths,
            fresh_hashlines: stored.fresh_hashlines,
            validation_findings: Vec::new(),
            embedding_state: "pending_or_ready".into(),
        }))
    }

    fn write_receipt(&self, digest: &str, response: &ApplyEditsResponse) -> Result<()> {
        let path = self.receipt_path(digest);
        let value = serde_json::json!({
            "touched_paths": response.touched_paths,
            "fresh_hashlines": response.fresh_hashlines,
        });
        write_atomic(&path, &serde_json::to_vec(&value)?)
    }

    fn write_pending(&self, digest: &str, pending: &PendingReceipt) -> Result<()> {
        write_atomic(&self.pending_path(digest), &serde_json::to_vec(pending)?)
    }

    fn remove_pending(&self, digest: &str) -> Result<()> {
        let path = self.pending_path(digest);
        if path.exists() {
            fs::remove_file(path)?;
        }
        Ok(())
    }

    fn recover_pending(&self, digest: &str) -> Result<Option<ApplyEditsResponse>> {
        let path = self.pending_path(digest);
        if !path.exists() {
            return Ok(None);
        }
        let pending: PendingReceipt = serde_json::from_slice(&fs::read(&path)?)?;
        if !git::history_contains_tree(&self.root, &pending.base_head, &pending.tree)? {
            fs::remove_file(path)?;
            return Ok(None);
        }
        let push = git::push(&self.root, &self.state.read().config)?;
        if matches!(push, PushState::Diverged) {
            self.set_blocked(Some("remote history diverged".into()))?;
        }
        let response = ApplyEditsResponse {
            status: ApplyStatus::AlreadyApplied,
            push,
            touched_paths: pending.touched_paths,
            fresh_hashlines: pending.fresh_hashlines,
            validation_findings: Vec::new(),
            embedding_state: "pending_or_ready".into(),
        };
        self.write_receipt(digest, &response)?;
        self.remove_pending(digest)?;
        Ok(Some(response))
    }

    fn current_push_state(&self) -> Result<PushState> {
        if !self.state.read().config.git.push {
            Ok(PushState::Disabled)
        } else if git::has_unpushed(&self.root)? {
            Ok(PushState::Queued)
        } else {
            Ok(PushState::Pushed)
        }
    }

    fn set_blocked(&self, reason: Option<String>) -> Result<()> {
        let path = self.git_dir.join("mdstore/blocked");
        if let Some(reason) = &reason {
            write_atomic(&path, reason.as_bytes())?;
        } else if path.exists() {
            fs::remove_file(path)?;
        }
        *self.blocked.write() = reason;
        Ok(())
    }
}

fn load_pages(root: &Path, config: &Config) -> Result<HashMap<String, String>> {
    git::tracked_markdown(root, config)?
        .into_iter()
        .map(|path| {
            read_repository_text(root, &path)
                .with_context(|| format!("read tracked Markdown {path}"))
                .map(|text| (path, text))
        })
        .collect()
}

fn load_config_files(root: &Path) -> Result<HashMap<String, String>> {
    git::tracked_config_files(root)?
        .into_iter()
        .map(|path| read_repository_text(root, &path).map(|text| (path, text)))
        .collect()
}

fn ensure_pages_match_config(config: &Config, pages: &HashMap<String, String>) -> Result<()> {
    ensure_paths_match_config(config, pages.keys())
}

fn ensure_paths_match_config<'a>(
    config: &Config,
    paths: impl IntoIterator<Item = &'a String>,
) -> Result<()> {
    let (include, exclude) = config.document_globs()?;
    for path in paths {
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
    provider: &dyn RetrievalProvider,
) -> SearchIndex {
    let mut all_chunks = HashMap::new();
    let provider_identity = provider.embedding_provider_identity();
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
                    &provider_identity,
                    provider.model(),
                    provider.dimensions(),
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

fn ensure_sidecars_ignored<'a>(
    root: &Path,
    pages: impl IntoIterator<Item = &'a str>,
) -> Result<()> {
    let paths: Vec<String> = pages
        .into_iter()
        .map(|path| {
            sidecar::sidecar_path(Path::new(path))
                .to_string_lossy()
                .replace('\\', "/")
        })
        .collect();
    git::ensure_ignored(root, paths.iter().map(String::as_str))
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

fn fresh_windows(
    changes: &HashMap<String, Option<String>>,
    changed_ranges: &HashMap<String, Vec<ChangedRange>>,
) -> HashMap<String, String> {
    changes
        .iter()
        .filter_map(|(path, content)| {
            let text = content.as_ref()?;
            let line_count = text.lines().count();
            if line_count == 0 {
                return Some((path.clone(), String::new()));
            }
            let mut windows: Vec<(usize, usize)> = changed_ranges
                .get(path)
                .into_iter()
                .flatten()
                .map(|range| {
                    (
                        range.start_line.saturating_sub(3).max(1),
                        (range.end_line + 3).min(line_count),
                    )
                })
                .collect();
            windows.sort_unstable();
            let mut merged: Vec<(usize, usize)> = Vec::new();
            for (start, end) in windows {
                if let Some((_, previous_end)) = merged.last_mut()
                    && start <= *previous_end + 1
                {
                    *previous_end = (*previous_end).max(end);
                } else {
                    merged.push((start, end));
                }
            }
            let rendered = merged
                .into_iter()
                .map(|window| render(text, Some(window)))
                .collect::<Vec<_>>()
                .join("\n…\n");
            Some((path.clone(), rendered))
        })
        .collect()
}

fn zeroentropy_provider(config: ProviderConfig) -> Arc<dyn RetrievalProvider> {
    Arc::new(ZeroEntropyProvider::new(config))
}

fn read_blocked(git_dir: &Path) -> Result<Option<String>> {
    let path = git_dir.join("mdstore/blocked");
    if path.exists() {
        Ok(Some(fs::read_to_string(path)?))
    } else {
        Ok(None)
    }
}

fn write_atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().context("state file has no parent")?;
    fs::create_dir_all(parent)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    temporary.write_all(bytes)?;
    temporary.as_file().sync_all()?;
    Ok(temporary
        .persist(path)
        .map_err(|error| error.error)
        .map(|_| ())?)
}

fn rollback_failure(root: &Path, paths: &[String], error: anyhow::Error) -> anyhow::Error {
    match git::rollback(root, paths) {
        Ok(()) => error,
        Err(rollback) => anyhow!("{error}; rollback also failed: {rollback}"),
    }
}

fn request_digest(request: &ApplyEditsRequest) -> Result<String> {
    let bytes = serde_json::to_vec(request)?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}
