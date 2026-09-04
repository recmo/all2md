use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    error::Error,
    fmt, fs,
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
    chunk::{Chunk, chunk_page},
    config::{
        Config, ProviderConfig, ensure_repository_path_safe, is_config_resource_path,
        validate_repo_path,
    },
    git::{self, PushState},
    hashline::{ChangedRange, EditOperation, apply_operations_with_ranges, render},
    markdown::{Edge, Finding, ParsedPage, project_metadata, validate_corpus},
    provider::{InputType, RetrievalProvider, ZeroEntropyProvider},
    search::{SearchIndex, SearchResponse},
    sidecar::{self, Sidecar},
};

/// Daemon-owned coherent view of one Git-backed Markdown repository.
pub struct Store {
    root: PathBuf,
    state: RwLock<StoreState>,
    provider_factory: Option<fn(ProviderConfig) -> Arc<dyn RetrievalProvider>>,
    reindex_lock: tokio::sync::Mutex<()>,
    git_dir: PathBuf,
    head: RwLock<String>,
    blocked: RwLock<Option<String>>,
}

impl fmt::Debug for Store {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Store")
            .field("root", &self.root)
            .finish_non_exhaustive()
    }
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
/// One required-summary atomic edit request.
pub struct ApplyEditsRequest {
    /// Non-empty Git commit message.
    pub edit_summary: String,
    /// Hashline operations resolved against one pre-edit snapshot.
    pub edits: Vec<EditOperation>,
}

#[derive(Debug, Clone, Serialize)]
/// Result of accepting or replaying an edit request.
pub struct ApplyEditsResponse {
    /// Whether a new commit was accepted or the request was already applied.
    pub status: ApplyStatus,
    /// Ordered push state after the request.
    pub push: PushState,
    /// Repository-relative paths addressed by the request.
    pub touched_paths: Vec<String>,
    /// Current hashline windows for changed regions.
    pub fresh_hashlines: HashMap<String, String>,
    /// Structured corpus findings when applicable.
    pub validation_findings: Vec<Finding>,
    /// Current or pending derived embedding state.
    pub embedding_state: String,
}

#[derive(Debug)]
/// Corpus validation failure with structured findings.
pub struct ValidationError {
    /// All findings from validating the proposed complete tree.
    pub findings: Vec<Finding>,
}

impl fmt::Display for ValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "validation failed")?;
        if let Ok(findings) = serde_json::to_string_pretty(&self.findings) {
            write!(formatter, ":\n{findings}")?;
        }
        Ok(())
    }
}

impl Error for ValidationError {}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
/// Idempotency status of an edit request.
pub enum ApplyStatus {
    /// A new commit was created.
    Accepted,
    /// The exact request was already represented by repository history.
    AlreadyApplied,
}

#[derive(Serialize, Deserialize)]
struct PendingReceipt {
    base_head: String,
    tree: String,
    touched_paths: Vec<String>,
    preimages: Images,
    postimages: Images,
}

#[derive(Serialize, Deserialize)]
struct StoredReceipt {
    touched_paths: Vec<String>,
    preimages: Images,
    postimages: Images,
}

type Images = BTreeMap<String, Option<String>>;

enum ReplayState {
    Applied,
    Reverted,
    Partial,
}

struct ReindexContext {
    config: Config,
    pages: HashMap<String, String>,
    parsed: HashMap<String, ParsedPage>,
    edges: Vec<Edge>,
    provider: Arc<dyn RetrievalProvider>,
    provider_identity: String,
    model: String,
    dimensions: usize,
    actions: Vec<ReindexAction>,
}

enum ReindexAction {
    Remove(PathBuf),
    Embed {
        text: String,
        chunks: Vec<Chunk>,
        sidecar_path: PathBuf,
    },
}

const STARTUP_SNAPSHOT_ATTEMPTS: usize = 8;

#[derive(Debug, Clone, Serialize)]
/// Hashline-rendered page or configuration resource.
pub struct PageResponse {
    /// Exact repository-relative path.
    pub path: String,
    /// Raw Markdown or configuration text with hashline prefixes.
    pub content: String,
    /// Repository-configured projected frontmatter metadata.
    pub metadata: serde_json::Value,
    /// Typed relations authored by this page.
    pub relations: Vec<Edge>,
}

#[derive(Debug, Clone, Serialize)]
/// Operational daemon and derived-index status.
pub struct StatusResponse {
    /// Number of indexed Markdown pages.
    pub pages: usize,
    /// Number of searchable chunks.
    pub chunks: usize,
    /// Chunks with valid vectors.
    pub vectors_ready: usize,
    /// Total searchable chunks.
    pub vectors_total: usize,
    /// Whether local commits are absent from the upstream.
    pub unpushed: bool,
    /// Persisted reason that writes are blocked, when present.
    pub blocked: Option<String>,
}

impl Store {
    /// Opens a repository with its configured ZeroEntropy provider.
    pub fn open(root: impl AsRef<Path>) -> Result<Arc<Self>> {
        let (root, git_dir) = Self::prepare_repository(root)?;
        Self::open_stable(&root, &git_dir, None)
    }

    /// Opens a repository with an injected retrieval provider.
    pub fn open_with_provider(
        root: impl AsRef<Path>,
        provider: Arc<dyn RetrievalProvider>,
    ) -> Result<Arc<Self>> {
        let (root, git_dir) = Self::prepare_repository(root)?;
        let provider = Some(provider);
        Self::open_stable(&root, &git_dir, provider.as_ref())
    }

    fn prepare_repository(root: impl AsRef<Path>) -> Result<(PathBuf, PathBuf)> {
        let root = root
            .as_ref()
            .canonicalize()
            .context("resolve repository root")?;
        git::ensure_repository(&root)?;
        let git_dir = git::git_dir(&root)?;
        for _ in 0..STARTUP_SNAPSHOT_ATTEMPTS {
            let head = git::head(&root)?;
            let config = Self::load_config(&root, &head);
            if git::head(&root)? == head {
                config?;
                git::recover_worktree(&root)?;
                return Ok((root, git_dir));
            }
        }
        bail!("repository HEAD kept changing during startup")
    }

    fn open_stable(
        root: &Path,
        git_dir: &Path,
        injected_provider: Option<&Arc<dyn RetrievalProvider>>,
    ) -> Result<Arc<Self>> {
        Self::open_stable_with(root, git_dir, injected_provider, |_, _| {})
    }

    fn open_stable_with(
        root: &Path,
        git_dir: &Path,
        injected_provider: Option<&Arc<dyn RetrievalProvider>>,
        mut after_capture: impl FnMut(&Path, &str),
    ) -> Result<Arc<Self>> {
        for _ in 0..STARTUP_SNAPSHOT_ATTEMPTS {
            let head = git::head(root)?;
            after_capture(root, &head);
            let result = Self::load_config(root, &head).and_then(|config| {
                let (provider, provider_factory) = if let Some(provider) = injected_provider {
                    (Arc::clone(provider), None)
                } else {
                    let factory: fn(ProviderConfig) -> Arc<dyn RetrievalProvider> =
                        zeroentropy_provider;
                    (zeroentropy_provider(config.provider.clone()), Some(factory))
                };
                Self::open_inner(
                    root.to_path_buf(),
                    head.clone(),
                    config,
                    provider,
                    git_dir.to_path_buf(),
                    provider_factory,
                )
            });
            if git::head(root)? == head {
                return result;
            }
        }
        bail!("repository HEAD kept changing during startup")
    }

    fn load_config(root: &Path, head: &str) -> Result<Config> {
        let config_text = git::read_text(root, head, ".mdstore/config.yaml")
            .map_err(|_| anyhow!(".mdstore/config.yaml must be tracked by Git"))?;
        Config::from_yaml(&config_text)
    }

    fn open_inner(
        root: PathBuf,
        head: String,
        config: Config,
        provider: Arc<dyn RetrievalProvider>,
        git_dir: PathBuf,
        provider_factory: Option<fn(ProviderConfig) -> Arc<dyn RetrievalProvider>>,
    ) -> Result<Arc<Self>> {
        let pages = load_pages(&root, &head, &config)?;
        let extra = load_config_files(&root, &head)?;
        let (parsed, edges) = validate_corpus(&config, &pages, &extra).map_err(|findings| {
            anyhow!(serde_json::to_string_pretty(&findings).unwrap_or_default())
        })?;
        ensure_sidecars_ignored(&root, pages.keys().map(String::as_str))?;
        let index = build_index(&root, &config, &pages, &parsed, &edges, provider.as_ref());
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
    /// Returns the canonical repository worktree root.
    pub fn root(&self) -> &Path {
        &self.root
    }

    #[must_use]
    /// Returns the currently published repository configuration.
    pub fn config(&self) -> Config {
        self.state.read().config.clone()
    }

    /// Revalidates the currently published corpus snapshot.
    pub fn validate(&self) -> std::result::Result<(), Vec<Finding>> {
        let state = self.state.read();
        validate_corpus(&state.config, &state.pages, &state.config_files).map(|_| ())
    }

    /// Reads an exact page or configuration resource as hashlines.
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
        if is_config_resource_path(path)
            && let Some(text) = state.config_files.get(path)
        {
            return Ok(PageResponse {
                path: path.into(),
                content: render(text, window),
                metadata: serde_json::json!({}),
                relations: Vec::new(),
            });
        }
        bail!("page or configuration resource not found: {path}")
    }

    /// Searches the published exact, vector, and graph index.
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
        index
            .search(&config, provider.as_ref(), query, variants)
            .await
    }

    /// Validates, commits, and pushes one atomic hashline edit batch.
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
            if is_config_resource_path(path) {
                has_config = true;
            } else if path.ends_with(".md") {
                has_content = true;
            } else if path.starts_with(".mdstore/") {
                bail!("configuration edits are limited to YAML and JSON files");
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
            let original = if path.ends_with(".md") {
                current.pages.get(path)
            } else {
                current.config_files.get(path)
            };
            if let Some(original) = original {
                originals.insert(path.clone(), original.clone());
            }
        }
        for path in &paths {
            if !originals.contains_key(path) && fs::symlink_metadata(self.root.join(path)).is_ok() {
                bail!("untracked repository path already exists: {path}");
            }
        }
        let applied = apply_operations_with_ranges(&originals, &request.edits)?;
        let changes = &applied.changes;
        let mut pages = current.pages.clone();
        let mut extra = current.config_files.clone();
        for (path, content) in changes {
            if path.ends_with(".md") {
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
            pages = load_pages(&self.root, &base_head, &config)?;
        }
        ensure_pages_match_config(&config, &pages)?;
        let (parsed, edges) = validate_corpus(&config, &pages, &extra)
            .map_err(|findings| ValidationError { findings })?;
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
        let (preimages, postimages) = edit_images(&originals, changes);
        let fresh_hashlines = fresh_windows(changes, &applied.changed_ranges);
        if let Err(error) = git::write_changes(&self.root, &ordered) {
            return Err(rollback_failure(&self.root, &path_list, error));
        }
        let tree = match git::stage_tree(&self.root, &base_head, &ordered) {
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
                preimages: preimages.clone(),
                postimages: postimages.clone(),
            };
            if let Err(error) = self.write_pending(&digest, &pending) {
                return Err(rollback_failure(&self.root, &path_list, error));
            }
            let commit = match git::commit_tree(
                &self.root,
                &pending.tree,
                &pending.base_head,
                &request.edit_summary,
            ) {
                Ok(commit) => commit,
                Err(error) => {
                    let error = rollback_failure(&self.root, &path_list, error);
                    return match self.remove_pending(&digest) {
                        Ok(()) => Err(error),
                        Err(cleanup) => {
                            Err(anyhow!("{error}; pending cleanup also failed: {cleanup}"))
                        }
                    };
                }
            };
            if let Err(error) = git::sync_index(&self.root, &commit, &path_list) {
                return Err(rollback_failure(&self.root, &path_list, error));
            }
            *self.head.write() = commit.clone();
            if git::head(&self.root)? != commit {
                return Err(rollback_failure(
                    &self.root,
                    &path_list,
                    anyhow!("repository HEAD advanced during edit publication; retry the request"),
                ));
            }
        }
        let index = build_index(
            &self.root,
            &config,
            &pages,
            &parsed,
            &edges,
            provider.as_ref(),
        );
        let push_config = config.clone();
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
        let push = git::push(&self.root, &push_config);
        if matches!(push, PushState::Diverged) {
            self.set_blocked(Some("remote history diverged".into()))?;
        }
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
        self.write_receipt(&digest, &response, &preimages, &postimages)?;
        self.remove_pending(&digest)?;
        Ok(response)
    }

    /// Rebuilds every embedding sidecar and the search index.
    pub async fn reindex(self: &Arc<Self>) -> Result<()> {
        self.spawn_reindex(true).await
    }

    /// Rebuilds missing or stale embedding sidecars and the search index.
    pub async fn reindex_missing(self: &Arc<Self>) -> Result<()> {
        self.spawn_reindex(false).await
    }

    async fn spawn_reindex(self: &Arc<Self>, force: bool) -> Result<()> {
        let store = Arc::clone(self);
        tokio::spawn(async move { store.reindex_all(force).await })
            .await
            .context("reindex task failed")?
    }

    async fn reindex_all(&self, force: bool) -> Result<()> {
        let mut paths: BTreeSet<String> = self.state.read().pages.keys().cloned().collect();
        let root = self.root.clone();
        let current = paths.clone();
        paths.extend(blocking(move || orphaned_sidecar_sources(&root, &current)).await?);
        let paths: Vec<String> = paths.into_iter().collect();
        self.reindex_paths_mode(&paths, force).await
    }

    async fn reindex_paths_mode(&self, paths: &[String], force: bool) -> Result<()> {
        let _guard = self.reindex_lock.lock().await;
        let snapshot = self.state.read().clone();
        let generation = snapshot.generation;
        let root = self.root.clone();
        let paths = paths.to_vec();
        let context = blocking(move || prepare_reindex(&root, snapshot, paths, force)).await?;
        if self.state.read().generation != generation {
            return Ok(());
        }
        for action in context.actions {
            match action {
                ReindexAction::Remove(path) => {
                    if self.state.read().generation != generation {
                        return Ok(());
                    }
                    blocking(move || {
                        if path.exists() {
                            fs::remove_file(path)?;
                        }
                        Ok(())
                    })
                    .await?;
                }
                ReindexAction::Embed {
                    text,
                    chunks,
                    sidecar_path,
                } => {
                    let mut vectors = Vec::new();
                    let batch_size = context.config.provider.batch_size.unwrap_or(64).max(1);
                    for batch in chunks.chunks(batch_size) {
                        let input: Vec<String> = batch
                            .iter()
                            .map(|chunk| chunk.embedding_text.clone())
                            .collect();
                        vectors.extend(context.provider.embed(InputType::Document, &input).await?);
                        if self.state.read().generation != generation {
                            return Ok(());
                        }
                    }
                    if self.state.read().generation != generation {
                        return Ok(());
                    }
                    let provider_identity = context.provider_identity.clone();
                    let model = context.model.clone();
                    let dimensions = context.dimensions;
                    blocking(move || {
                        let sidecar = Sidecar::new(
                            &text,
                            &provider_identity,
                            &model,
                            dimensions,
                            &chunks,
                            &vectors,
                        )?;
                        sidecar::write_atomic(&sidecar_path, &sidecar)
                    })
                    .await?;
                }
            }
        }
        if self.state.read().generation != generation {
            return Ok(());
        }
        let root = self.root.clone();
        let config = context.config;
        let pages = context.pages;
        let parsed = context.parsed;
        let edges = context.edges;
        let provider = context.provider;
        let index = blocking(move || {
            Ok(build_index(
                &root,
                &config,
                &pages,
                &parsed,
                &edges,
                provider.as_ref(),
            ))
        })
        .await?;
        let mut current = self.state.write();
        if current.generation == generation {
            current.index = Arc::new(index);
        }
        Ok(())
    }

    /// Returns current corpus, vector, push, and block status.
    pub fn status(&self) -> Result<StatusResponse> {
        let (pages, chunks, vectors_ready) = {
            let state = self.state.read();
            (
                state.pages.len(),
                state.index.chunks.len(),
                state
                    .index
                    .chunks
                    .iter()
                    .filter(|chunk| chunk.vector.is_some())
                    .count(),
            )
        };
        let unpushed = git::has_unpushed(&self.root)?;
        let blocked = self.blocked.read().clone();
        Ok(StatusResponse {
            pages,
            chunks,
            vectors_ready,
            vectors_total: chunks,
            unpushed,
            blocked,
        })
    }

    /// Refreshes external state and attempts one ordered push.
    pub fn push(&self) -> Result<PushState> {
        let _lock = self.lock_repository()?;
        git::recover_worktree(&self.root)?;
        self.refresh_external_commit()?;
        let config = self.state.read().config.clone();
        let state = git::push(&self.root, &config);
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
        let config = Config::from_yaml(&git::read_text(
            &self.root,
            &current,
            ".mdstore/config.yaml",
        )?)?;
        let pages = load_pages(&self.root, &current, &config)?;
        let extra = load_config_files(&self.root, &current)?;
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
        let stored: StoredReceipt = serde_json::from_slice(&fs::read(path)?)?;
        match self.replay_state(&stored.preimages, &stored.postimages)? {
            ReplayState::Applied => {}
            ReplayState::Reverted => return Ok(None),
            ReplayState::Partial => {
                bail!(
                    "the previously applied batch was only partially superseded; read fresh hashlines and submit a new atomic batch"
                )
            }
        }
        let fresh_hashlines = self.current_hashlines(&stored.touched_paths);
        Ok(Some(ApplyEditsResponse {
            status: ApplyStatus::AlreadyApplied,
            push: self.current_push_state()?,
            touched_paths: stored.touched_paths,
            fresh_hashlines,
            validation_findings: Vec::new(),
            embedding_state: "pending_or_ready".into(),
        }))
    }

    fn write_receipt(
        &self,
        digest: &str,
        response: &ApplyEditsResponse,
        preimages: &Images,
        postimages: &Images,
    ) -> Result<()> {
        let stored = StoredReceipt {
            touched_paths: response.touched_paths.clone(),
            preimages: preimages.clone(),
            postimages: postimages.clone(),
        };
        write_atomic(&self.receipt_path(digest), &serde_json::to_vec(&stored)?)
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
        match self.replay_state(&pending.preimages, &pending.postimages)? {
            ReplayState::Applied => {}
            ReplayState::Reverted => {
                fs::remove_file(path)?;
                return Ok(None);
            }
            ReplayState::Partial => {
                bail!(
                    "the previously applied batch was only partially superseded; read fresh hashlines and submit a new atomic batch"
                )
            }
        }
        let config = self.state.read().config.clone();
        let push = git::push(&self.root, &config);
        if matches!(push, PushState::Diverged) {
            self.set_blocked(Some("remote history diverged".into()))?;
        }
        let response = ApplyEditsResponse {
            status: ApplyStatus::AlreadyApplied,
            push,
            fresh_hashlines: self.current_hashlines(&pending.touched_paths),
            touched_paths: pending.touched_paths,
            validation_findings: Vec::new(),
            embedding_state: "pending_or_ready".into(),
        };
        self.write_receipt(digest, &response, &pending.preimages, &pending.postimages)?;
        self.remove_pending(digest)?;
        Ok(Some(response))
    }

    fn current_hashlines(&self, paths: &[String]) -> HashMap<String, String> {
        let state = self.state.read();
        paths
            .iter()
            .filter_map(|path| {
                state
                    .pages
                    .get(path)
                    .or_else(|| state.config_files.get(path))
                    .map(|text| (path.clone(), render(text, None)))
            })
            .collect()
    }

    fn replay_state(&self, preimages: &Images, postimages: &Images) -> Result<ReplayState> {
        if !preimages.keys().eq(postimages.keys()) {
            bail!("receipt preimages and postimages do not cover the same paths");
        }
        if preimages.is_empty() {
            return Ok(ReplayState::Applied);
        }
        let head = self.head.read().clone();
        let mut matches_preimages = true;
        let mut matches_postimages = true;
        for (path, preimage) in preimages {
            let current = if git::is_tracked(&self.root, path)? {
                Some(git::read_text(&self.root, &head, path)?)
            } else {
                None
            };
            matches_preimages &= current == *preimage;
            matches_postimages &= current == postimages[path];
        }
        Ok(if matches_postimages {
            ReplayState::Applied
        } else if matches_preimages {
            ReplayState::Reverted
        } else {
            ReplayState::Partial
        })
    }

    fn current_push_state(&self) -> Result<PushState> {
        let push_enabled = self.state.read().config.git.push;
        if !push_enabled {
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

fn edit_images(
    originals: &HashMap<String, String>,
    changes: &HashMap<String, Option<String>>,
) -> (Images, Images) {
    let mut preimages = Images::new();
    let mut postimages = Images::new();
    for (path, updated) in changes {
        let original = originals.get(path).cloned();
        preimages.insert(path.clone(), original);
        postimages.insert(path.clone(), updated.clone());
    }
    (preimages, postimages)
}

fn load_pages(root: &Path, revision: &str, config: &Config) -> Result<HashMap<String, String>> {
    git::tracked_markdown(root, revision, config)?
        .into_iter()
        .map(|path| {
            git::read_text(root, revision, &path)
                .with_context(|| format!("read tracked Markdown {path}"))
                .map(|text| (path, text))
        })
        .collect()
}

fn load_config_files(root: &Path, revision: &str) -> Result<HashMap<String, String>> {
    git::tracked_config_files(root, revision)?
        .into_iter()
        .map(|path| git::read_text(root, revision, &path).map(|text| (path, text)))
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

async fn blocking<T>(operation: impl FnOnce() -> Result<T> + Send + 'static) -> Result<T>
where
    T: Send + 'static,
{
    tokio::task::spawn_blocking(operation)
        .await
        .context("blocking repository operation failed")?
}

fn prepare_reindex(
    root: &Path,
    snapshot: StoreState,
    paths: Vec<String>,
    force: bool,
) -> Result<ReindexContext> {
    let StoreState {
        config,
        config_files: _,
        pages,
        parsed,
        edges,
        index: _,
        provider,
        generation: _,
    } = snapshot;
    let provider_identity = provider.embedding_provider_identity();
    let model = provider.model().to_owned();
    let dimensions = provider.dimensions();
    ensure_sidecars_ignored(
        root,
        pages
            .keys()
            .map(String::as_str)
            .chain(paths.iter().map(String::as_str)),
    )?;
    let mut actions = Vec::new();
    for path in paths {
        let sidecar_path = sidecar::sidecar_path(&root.join(&path));
        let Some(text) = pages.get(&path) else {
            actions.push(ReindexAction::Remove(sidecar_path));
            continue;
        };
        let page = parsed.get(&path).context("missing parsed page")?;
        let context = embedding_context(&config, page);
        let chunks = chunk_page(text, page, &config.chunking, &context);
        if !force
            && sidecar::read(&sidecar_path).ok().is_some_and(|stored| {
                stored
                    .vectors_for(text, &provider_identity, &model, dimensions, &chunks)
                    .is_some()
            })
        {
            continue;
        }
        actions.push(ReindexAction::Embed {
            text: text.clone(),
            chunks,
            sidecar_path,
        });
    }
    Ok(ReindexContext {
        config,
        pages,
        parsed,
        edges,
        provider,
        provider_identity,
        model,
        dimensions,
        actions,
    })
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

fn orphaned_sidecar_sources(root: &Path, pages: &BTreeSet<String>) -> Result<Vec<String>> {
    Ok(git::untracked_sidecars(root)?
        .into_iter()
        .filter_map(|path| {
            path.strip_suffix(".mdstore")
                .map(|stem| format!("{stem}.md"))
        })
        .filter(|path| !pages.contains(path))
        .collect())
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

#[cfg(test)]
mod tests {
    use std::{
        process::Command,
        sync::atomic::{AtomicBool, Ordering},
    };

    use super::*;

    fn run_git(root: &Path, arguments: &[&str]) -> String {
        let output = Command::new("git")
            .current_dir(root)
            .args(arguments)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap().trim().into()
    }

    fn config(document: &str) -> String {
        format!(
            "documents:\n  include: ['{document}']\nprovider:\n  dimensions: 2\ngit:\n  push: false\n"
        )
    }

    struct AdvancingProvider {
        root: PathBuf,
        advanced: AtomicBool,
    }

    #[async_trait::async_trait]
    impl RetrievalProvider for AdvancingProvider {
        async fn embed(&self, _: InputType, _: &[String]) -> Result<Vec<Vec<f32>>> {
            unreachable!()
        }

        async fn rerank(
            &self,
            _: &str,
            _: &[String],
            _: usize,
        ) -> Result<Vec<crate::provider::RerankResult>> {
            unreachable!()
        }

        fn model(&self) -> &str {
            "advancing"
        }

        fn dimensions(&self) -> usize {
            2
        }

        fn embedding_provider_identity(&self) -> String {
            if !self.advanced.swap(true, Ordering::SeqCst) {
                fs::write(self.root.join(".mdstore/config.yaml"), config("new.md")).unwrap();
                fs::write(self.root.join("new.md"), "new snapshot\n").unwrap();
                run_git(&self.root, &["add", ".mdstore/config.yaml", "new.md"]);
                run_git(&self.root, &["commit", "-q", "-m", "new snapshot"]);
            }
            "advancing-provider".into()
        }
    }

    #[test]
    fn receipt_images_cover_every_edited_path() {
        let originals = HashMap::from([
            ("changed.md".into(), "old".into()),
            ("removed.md".into(), "removed".into()),
            ("same.md".into(), "same".into()),
        ]);
        let changes = HashMap::from([
            ("changed.md".into(), Some("new".into())),
            ("same.md".into(), Some("same".into())),
            ("created.md".into(), Some("created".into())),
            ("removed.md".into(), None),
        ]);

        let (preimages, postimages) = edit_images(&originals, &changes);
        assert_eq!(
            preimages,
            BTreeMap::from([
                ("changed.md".into(), Some("old".into())),
                ("created.md".into(), None),
                ("removed.md".into(), Some("removed".into())),
                ("same.md".into(), Some("same".into())),
            ])
        );
        assert_eq!(
            postimages,
            BTreeMap::from([
                ("changed.md".into(), Some("new".into())),
                ("created.md".into(), Some("created".into())),
                ("removed.md".into(), None),
                ("same.md".into(), Some("same".into())),
            ])
        );
    }

    #[test]
    fn startup_snapshot_never_mixes_configuration_and_corpus_commits() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        run_git(root, &["init", "-q", "-b", "main"]);
        run_git(root, &["config", "user.name", "mdstore test"]);
        run_git(root, &["config", "user.email", "mdstore@example.invalid"]);
        run_git(root, &["config", "commit.gpgsign", "false"]);
        fs::create_dir(root.join(".mdstore")).unwrap();
        fs::write(root.join(".gitignore"), "*.mdstore\n!.mdstore/\n").unwrap();
        fs::write(root.join(".mdstore/config.yaml"), config("old.md")).unwrap();
        fs::write(root.join("old.md"), "old snapshot\n").unwrap();
        run_git(
            root,
            &["add", ".gitignore", ".mdstore/config.yaml", "old.md"],
        );
        run_git(root, &["commit", "-q", "-m", "old snapshot"]);

        let provider: Arc<dyn RetrievalProvider> = Arc::new(AdvancingProvider {
            root: root.to_path_buf(),
            advanced: AtomicBool::new(false),
        });
        let store = Store::open_with_provider(root, provider).unwrap();
        assert_eq!(store.config().documents.include, ["new.md"]);
        assert!(store.get_page("old.md", None).is_err());
        assert!(store.get_page("new.md", None).is_ok());
    }

    #[test]
    fn startup_retries_a_failed_superseded_snapshot() {
        let repository = tempfile::tempdir().unwrap();
        let root = repository.path();
        run_git(root, &["init", "-q", "-b", "main"]);
        run_git(root, &["config", "user.name", "mdstore test"]);
        run_git(root, &["config", "user.email", "mdstore@example.invalid"]);
        run_git(root, &["config", "commit.gpgsign", "false"]);
        fs::create_dir(root.join(".mdstore")).unwrap();
        fs::write(root.join(".gitignore"), "*.mdstore\n!.mdstore/\n").unwrap();
        fs::write(root.join(".mdstore/config.yaml"), config("old.md")).unwrap();
        fs::write(root.join("old.md"), "---\nunterminated\n").unwrap();
        run_git(
            root,
            &["add", ".gitignore", ".mdstore/config.yaml", "old.md"],
        );
        run_git(root, &["commit", "-q", "-m", "invalid old snapshot"]);

        let (root, git_dir) = Store::prepare_repository(root).unwrap();
        let provider: Arc<dyn RetrievalProvider> = Arc::new(AdvancingProvider {
            root: root.clone(),
            advanced: AtomicBool::new(true),
        });
        let mut advanced = false;
        let store =
            Store::open_stable_with(&root, &git_dir, Some(&provider), |root, _captured_head| {
                if !advanced {
                    fs::write(root.join(".mdstore/config.yaml"), config("new.md")).unwrap();
                    fs::write(root.join("new.md"), "new snapshot\n").unwrap();
                    run_git(root, &["add", ".mdstore/config.yaml", "new.md"]);
                    run_git(root, &["commit", "-q", "-m", "valid new snapshot"]);
                    advanced = true;
                }
            })
            .unwrap();

        assert!(advanced);
        assert_eq!(store.config().documents.include, ["new.md"]);
        assert!(store.get_page("old.md", None).is_err());
        assert!(store.get_page("new.md", None).is_ok());
    }
}
