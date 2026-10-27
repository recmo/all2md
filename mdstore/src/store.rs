use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    error::Error,
    fmt, fs,
    io::Write,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
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
    hashline::{EditOperation, apply_operations, render},
    markdown::{Edge, Finding, ParsedPage, validate_corpus},
    provider::{InputType, RetrievalProvider, ZeroEntropyProvider},
    search::{PageChunks, SearchIndex, SearchResponse, SearchResult},
    sidecar::{self, Sidecar},
};

/// Daemon-owned coherent view of one Git-backed Markdown repository.
pub struct Store {
    root: PathBuf,
    state: RwLock<Arc<StoreState>>,
    provider_factory: Option<fn(ProviderConfig) -> Arc<dyn RetrievalProvider>>,
    reindex_lock: tokio::sync::Mutex<()>,
    git_dir: PathBuf,
    reindex_notify: tokio::sync::Notify,
    blocked: RwLock<Option<WriteBlock>>,
    sync_lock: parking_lot::Mutex<()>,
    sync_notify: tokio::sync::Notify,
    replication: RwLock<ReplicationStatus>,
}

impl fmt::Debug for Store {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Store")
            .field("root", &self.root)
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "kind", content = "detail", rename_all = "snake_case")]
enum WriteBlock {
    Diverged,
    ExternalCommit(String),
}

impl fmt::Display for WriteBlock {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Diverged => formatter.write_str("remote history diverged"),
            Self::ExternalCommit(detail) => {
                write!(formatter, "external commit is invalid: {detail}")
            }
        }
    }
}

#[derive(Clone)]
struct StoreState {
    head: String,
    templates: Arc<crate::template::Templates>,
    config: Config,
    config_files: Arc<HashMap<String, String>>,
    pages: Arc<HashMap<String, String>>,
    parsed: Arc<HashMap<String, ParsedPage>>,
    edges: Arc<Vec<Edge>>,
    index: Arc<SearchIndex>,
    provider: Arc<dyn RetrievalProvider>,
    generation: u64,
}

#[derive(Debug, Clone, Serialize)]
/// One required-summary atomic edit request.
pub struct ApplyEditsRequest {
    /// Non-empty Git commit message.
    pub summary: String,
    /// Hashline operations resolved against one pre-edit snapshot.
    pub edits: Vec<EditOperation>,
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
    snapshot: Arc<StoreState>,
    provider_identity: String,
    model: String,
    dimensions: usize,
    actions: Vec<ReindexAction>,
    ready: PageChunks,
}

enum ReindexAction {
    Remove(PathBuf),
    Embed {
        path: String,
        text: String,
        chunks: Vec<Chunk>,
        sidecar_path: PathBuf,
    },
}

const STARTUP_SNAPSHOT_ATTEMPTS: usize = 8;

#[derive(Debug)]
pub(crate) struct PageSource {
    pub template: Option<String>,
    pub text: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
/// Hashline-rendered page or configuration resource.
pub struct PageResponse {
    /// Governing schema as a repository-rooted path.
    pub template: Option<String>,
    /// Exact repository-relative path.
    pub path: String,
    /// Markdown or configuration text with hashline prefixes; absent for a missing file.
    pub content: Option<String>,
    /// Exact source text for full-page reads, absent for line windows.
    #[serde(skip_serializing)]
    pub text: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
/// Operational daemon and derived-index status.
pub struct StatusResponse {
    /// Local-to-remote replication progress, independent of embedding state.
    pub replication: ReplicationStatus,
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

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
/// Last observed Git replication state. Pending counts are refreshed on status reads.
pub struct ReplicationStatus {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    /// Fingerprint of the destination whose progress is reported.
    pub destination: Option<String>,
    /// Local commits not present at the last observed remote revision.
    pub pending_commits: usize,
    /// Unix timestamp of the last completed successful synchronization.
    pub last_success: Option<u64>,
    /// Most recent synchronization error, cleared on success.
    pub last_error: Option<String>,
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
                let store = Self::open_inner(
                    root.to_path_buf(),
                    head.clone(),
                    config,
                    provider,
                    git_dir.to_path_buf(),
                    provider_factory,
                )?;
                store.activate_staged_restart()?;
                Ok(store)
            });
            if git::head(root)? == head {
                return result;
            }
        }
        bail!("repository HEAD kept changing during startup")
    }

    fn load_config(root: &Path, head: &str) -> Result<Config> {
        let config_text = git::read_text(root, head, "config.yaml")
            .map_err(|_| anyhow!("config.yaml must be tracked by Git"))?;
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
        let snapshot = load_snapshot(&root, head, config, provider, 0)?;
        let blocked = read_blocked(&git_dir)?;
        let replication_path = git_dir.join("mdstore/replication.json");
        let replication = if replication_path.exists() {
            serde_json::from_slice(&fs::read(replication_path)?)?
        } else {
            ReplicationStatus::default()
        };
        Ok(Arc::new(Self {
            root,
            state: RwLock::new(Arc::new(snapshot)),
            provider_factory,
            reindex_lock: tokio::sync::Mutex::new(()),
            git_dir,
            reindex_notify: tokio::sync::Notify::new(),
            blocked: RwLock::new(blocked),
            sync_lock: parking_lot::Mutex::new(()),
            sync_notify: tokio::sync::Notify::new(),
            replication: RwLock::new(replication),
        }))
    }

    #[must_use]
    /// Returns the currently published repository configuration.
    pub fn config(&self) -> Config {
        self.state.read().config.clone()
    }

    /// Revalidates the currently published corpus snapshot.
    pub fn validate(&self) -> std::result::Result<(), Vec<Finding>> {
        let state = self.state.read();
        validate_corpus(&state.pages, &state.templates).map(|_| ())
    }

    /// Lists direct children of a directory in the published document tree.
    /// Root is `/`; other directory paths must end with `/`.
    pub fn get_directory(&self, path: &str) -> Result<Vec<String>> {
        self.list_directory(path, false, false)
    }

    /// Lists all descendants, excluding the starting directory itself.
    pub fn get_subtree(&self, path: &str, directories_only: bool) -> Result<Vec<String>> {
        self.list_directory(path, true, directories_only)
    }

    fn list_directory(
        &self,
        path: &str,
        recursive: bool,
        directories_only: bool,
    ) -> Result<Vec<String>> {
        let prefix = if path == "/" {
            ""
        } else {
            let directory = path
                .strip_suffix('/')
                .context("directory path must end with /")?;
            validate_repo_path(directory)?;
            path
        };
        let state = self.state.read();
        let mut children = std::collections::BTreeSet::new();
        let mut found = prefix.is_empty();
        for file in state.pages.keys().chain(state.config_files.keys()) {
            let Some(relative) = file.strip_prefix(prefix) else {
                continue;
            };
            found = true;
            if recursive {
                for (index, _) in relative.match_indices('/') {
                    children.insert(format!("{prefix}{}", &relative[..=index]));
                }
                if !directories_only {
                    children.insert(file.clone());
                }
            } else {
                let child = relative.split_once('/').map_or_else(
                    || file.clone(),
                    |(directory, _)| format!("{prefix}{directory}/"),
                );
                children.insert(child);
            }
        }
        if !found {
            bail!("directory not found: {path}");
        }
        Ok(children.into_iter().collect())
    }

    /// Reads an exact page or configuration resource as hashlines.
    pub fn get_page(&self, path: &str, window: Option<(usize, usize)>) -> Result<PageResponse> {
        let source = self.get_source(path)?;
        Ok(PageResponse {
            template: source.template,
            path: path.into(),
            content: source.text.as_ref().map(|text| render(text, window)),
            text: if window.is_none() { source.text } else { None },
        })
    }

    pub(crate) fn get_source(&self, path: &str) -> Result<PageSource> {
        validate_repo_path(path)?;
        let state = self.state.read();
        if let Some(text) = state.pages.get(path) {
            return Ok(PageSource {
                template: state.templates.template_path(path),
                text: Some(text.clone()),
            });
        }
        if (is_config_resource_path(path) || crate::template::is_template(path))
            && let Some(text) = state.config_files.get(path)
        {
            return Ok(PageSource {
                template: None,
                text: Some(text.clone()),
            });
        }
        if (path.ends_with(".md")
            || (crate::config::is_lint_config(path) && state.config.server.allow_config_edits))
            && (!crate::template::is_template(path) || state.config.server.allow_template_edits)
        {
            if !crate::template::is_template(path) && !crate::config::is_lint_config(path) {
                ensure_paths_match_config(&state.config, [&path.to_owned()])?;
            }
            return Ok(PageSource {
                template: state.templates.template_path(path),
                text: None,
            });
        }
        bail!("page or configuration resource not found: {path}")
    }

    /// Searches one published snapshot and returns anchored source excerpts.
    pub async fn search(&self, query: &str, variants: &[String]) -> Result<SearchResponse> {
        if query.trim().is_empty() {
            bail!("query must be non-empty");
        }
        let snapshot = self.state.read().clone();
        let response = snapshot
            .index
            .search(
                &snapshot.config,
                snapshot.provider.as_ref(),
                query,
                variants,
            )
            .await?;
        let results = response
            .results
            .into_iter()
            .map(|result| {
                let text = snapshot
                    .pages
                    .get(&result.path)
                    .context("search result source is missing")?;
                Ok(SearchResult {
                    path: format!("/{}:{}-{}", result.path, result.start_line, result.end_line),
                    excerpt: render(text, Some((result.start_line, result.end_line))),
                })
            })
            .collect::<Result<_>>()?;
        Ok(SearchResponse {
            results,
            degraded: response.degraded,
        })
    }

    /// Validates and commits one atomic hashline edit batch, then notifies replication.
    pub fn apply_edits(&self, request: &ApplyEditsRequest) -> Result<Vec<String>> {
        if request.summary.trim().is_empty() {
            bail!("summary must be non-empty");
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
        if let Some(reason) = self.blocked.read().as_ref() {
            bail!("writes are blocked: {reason}");
        }

        let current = self.state.read().clone();
        let mut existing: std::collections::HashSet<_> = current.pages.keys().cloned().collect();
        // Reserve explicitly named creates too, regardless of their position in the batch.
        for edit in &request.edits {
            if let EditOperation::Replace {
                path, anchor: None, ..
            } = edit
                && !path.contains(['{', '}'])
            {
                existing.insert(path.clone());
            }
        }
        let mut edits = request.edits.clone();
        for edit in &mut edits {
            if let EditOperation::Replace {
                path, anchor: None, ..
            } = edit
            {
                *path = current.templates.allocate_path(path, &existing)?;
                existing.insert(path.clone());
            } else if edit.path().contains(['{', '}']) {
                bail!("path placeholders are supported only for creation");
            }
        }
        let mut paths = BTreeSet::new();
        for edit in &edits {
            let path = edit.path();
            validate_repo_path(path)?;
            let allowed = if is_config_resource_path(path) {
                current.config.server.allow_config_edits
            } else if crate::template::is_template(path) {
                current.config.server.allow_template_edits
            } else {
                path.ends_with(".md")
            };
            if !allowed {
                bail!("path is read-only for this server: {path}");
            }
            paths.insert(path.to_owned());
        }

        let mut base_head = current.head.clone();
        ensure_paths_match_config(
            &current.config,
            paths.iter().filter(|path| {
                !crate::template::is_template(path) && !is_config_resource_path(path)
            }),
        )?;

        let mut originals = HashMap::new();
        for path in &paths {
            ensure_repository_path_safe(&self.root, path)?;
            let original = current
                .pages
                .get(path)
                .or_else(|| current.config_files.get(path));
            if let Some(original) = original {
                originals.insert(path.clone(), original.clone());
            }
        }
        for path in &paths {
            if !originals.contains_key(path) && fs::symlink_metadata(self.root.join(path)).is_ok() {
                bail!("untracked repository path already exists: {path}");
            }
        }
        let applied = apply_operations(&originals, &edits)?;
        let changes = &applied;
        let mut pages = (*current.pages).clone();
        let mut extra = (*current.config_files).clone();
        for (path, content) in changes {
            let destination = if crate::template::is_template(path) || is_config_resource_path(path)
            {
                &mut extra
            } else {
                &mut pages
            };
            match content {
                Some(text) => {
                    destination.insert(path.clone(), text.clone());
                }
                None => {
                    destination.remove(path);
                }
            }
        }
        let config = Config::from_yaml(
            extra
                .get("config.yaml")
                .context("config.yaml cannot be removed")?,
        )
        .map_err(|error| ValidationError {
            findings: vec![crate::markdown::Finding {
                source: None,
                path: "config.yaml".into(),
                line: None,
                message: format!("invalid configuration: {error:#}"),
            }],
        })?;
        if paths.contains("config.yaml") {
            // Re-select the complete tracked corpus under the proposed configuration.
            pages = load_pages(&self.root, &current.head, &config)?;
            for (path, content) in changes.iter().filter(|(path, _)| {
                !crate::template::is_template(path) && !is_config_resource_path(path)
            }) {
                match content {
                    Some(text) => {
                        pages.insert(path.clone(), text.clone());
                    }
                    None => {
                        pages.remove(path);
                    }
                }
            }
        }
        ensure_pages_match_config(&config, &pages)?;
        if config.provider != current.config.provider && self.provider_factory.is_none() {
            bail!("provider changes require a daemon with a configurable provider");
        }
        let templates_changed = paths
            .iter()
            .any(|path| crate::template::is_template(path) || crate::config::is_lint_config(path));
        let templates = if templates_changed {
            Arc::new(
                crate::template::Templates::compile(&extra)
                    .map_err(|findings| ValidationError { findings })?,
            )
        } else {
            Arc::clone(&current.templates)
        };
        let (parsed, edges) = crate::markdown::validate_incremental(
            &pages,
            &templates,
            crate::markdown::ValidationBaseline {
                pages: &current.pages,
                parsed: &current.parsed,
                edges: &current.edges,
                templates: &current.templates,
            },
        )
        .map_err(|findings| ValidationError { findings })?;
        current
            .templates
            .validate_changes(&current.pages, &pages)
            .map_err(|findings| ValidationError { findings })?;
        if templates_changed {
            templates
                .validate_changes(&current.pages, &pages)
                .map_err(|findings| ValidationError { findings })?;
        }
        ensure_sidecars_ignored(&self.root, pages.keys().map(String::as_str))?;
        let provider = if config.provider != current.config.provider {
            let factory = self
                .provider_factory
                .context("provider changes require a daemon with a configurable provider")?;
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
        if let Err(error) = git::write_changes(&self.root, &ordered) {
            return Err(rollback_failure(&self.root, &path_list, error));
        }
        let tree = match git::stage_tree(&self.root, &base_head, &ordered) {
            Ok(value) => value,
            Err(error) => {
                return Err(rollback_failure(&self.root, &path_list, error));
            }
        };
        if let Some(tree) = tree {
            let pending = PendingReceipt {
                base_head: base_head.clone(),
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
                &request.summary,
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
            base_head = commit.clone();
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
        *self.state.write() = Arc::new(StoreState {
            head: base_head,
            templates,
            config,
            config_files: if extra == *current.config_files {
                Arc::clone(&current.config_files)
            } else {
                Arc::new(extra)
            },
            pages: Arc::new(pages),
            parsed: Arc::new(parsed),
            edges: Arc::new(edges),
            index: Arc::new(index),
            provider,
            generation: current.generation + 1,
        });
        self.write_receipt(&digest, &path_list, &preimages, &postimages)?;
        self.remove_pending(&digest)?;
        self.sync_notify.notify_one();
        self.reindex_notify.notify_one();
        Ok(path_list)
    }

    /// Rebuilds every embedding sidecar and the search index.
    pub async fn reindex(self: &Arc<Self>) -> Result<()> {
        self.spawn_reindex(true).await
    }

    /// Rebuilds missing or stale embedding sidecars and the search index.
    pub async fn reindex_missing(self: &Arc<Self>) -> Result<()> {
        self.spawn_reindex(false).await
    }

    pub(crate) async fn maintain_embeddings(self: Arc<Self>) {
        let mut retry_ceiling_ms = 1_000;
        self.reindex_notify.notify_one();
        self.reindex_notify.notified().await;
        loop {
            if self.reindex_missing().await.is_ok() {
                retry_ceiling_ms = 1_000;
                self.reindex_notify.notified().await;
            } else {
                let delay = embedding_retry_delay(&mut retry_ceiling_ms);
                tokio::select! {
                    _ = self.reindex_notify.notified() => {},
                    _ = tokio::time::sleep(delay) => {},
                }
            }
        }
    }

    async fn spawn_reindex(self: &Arc<Self>, force: bool) -> Result<()> {
        // Cancellation must not release the job lock while blocking sidecar I/O is running.
        let store = Arc::clone(self);
        tokio::spawn(async move {
            let result = store.reindex_all(force).await;
            if let Err(error) = &result {
                tracing::error!(%error, "reindex failed");
            }
            result
        })
        .await
        .context("reindex task failed")?
    }

    async fn reindex_all(&self, force: bool) -> Result<()> {
        let _guard = self.reindex_lock.lock().await;
        let snapshot = self.state.read().clone();
        let generation = snapshot.generation;
        let root = self.root.clone();
        let mut context = blocking(move || prepare_reindex(&root, snapshot, force)).await?;
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
                    path,
                    text,
                    chunks,
                    sidecar_path,
                } => {
                    let mut vectors = Vec::new();
                    let batch_size = context
                        .snapshot
                        .config
                        .provider
                        .batch_size
                        .unwrap_or(64)
                        .max(1);
                    for batch in chunks.chunks(batch_size) {
                        let input: Vec<String> = batch
                            .iter()
                            .map(|chunk| chunk.embedding_text.clone())
                            .collect();
                        vectors.extend(
                            context
                                .snapshot
                                .provider
                                .embed(InputType::Document, &input)
                                .await?,
                        );
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
                    let values = blocking(move || {
                        let sidecar = Sidecar::new(
                            &text,
                            &provider_identity,
                            &model,
                            dimensions,
                            &chunks,
                            &vectors,
                        )?;
                        sidecar::write_atomic(&sidecar_path, &sidecar)?;
                        Ok(chunks
                            .into_iter()
                            .zip(vectors.into_iter().map(Some))
                            .collect())
                    })
                    .await?;
                    context.ready.insert(path, values);
                }
            }
        }
        if self.state.read().generation != generation {
            return Ok(());
        }
        let snapshot = context.snapshot;
        let index = blocking(move || {
            Ok(SearchIndex::build(
                &snapshot.parsed,
                &snapshot.edges,
                context.ready,
            ))
        })
        .await?;
        let mut current = self.state.write();
        if current.generation == generation {
            Arc::make_mut(&mut current).index = Arc::new(index);
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
        let mut replication = self.current_replication();
        replication.pending_commits = git::pending_commits(&self.root)?;
        let unpushed = replication.pending_commits > 0;
        let blocked = self.blocked.read().as_ref().map(ToString::to_string);
        Ok(StatusResponse {
            replication,
            pages,
            chunks,
            vectors_ready,
            vectors_total: chunks,
            unpushed,
            blocked,
        })
    }

    /// Attempts one synchronization; network operations never hold the edit lock.
    pub fn push(&self) -> Result<PushState> {
        let _sync = self.sync_lock.lock();
        let mut progress = self.current_replication();
        let result = self.synchronize_once();
        let mut status = self.replication.write();
        std::mem::swap(&mut *status, &mut progress);
        status.pending_commits = git::pending_commits(&self.root)?;
        match &result {
            Ok(PushState::Pushed) => {
                status.last_success = Some(SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs());
                status.last_error = None;
            }
            Ok(PushState::Diverged) => status.last_error = Some("remote history diverged".into()),
            Err(error) => status.last_error = Some(error.to_string()),
            _ => {}
        }
        write_atomic(
            &self.git_dir.join("mdstore/replication.json"),
            &serde_json::to_vec(&*status)?,
        )?;
        result
    }

    fn synchronize_once(&self) -> Result<PushState> {
        let config = {
            let _lock = self.lock_repository()?;
            git::recover_worktree(&self.root)?;
            self.refresh_external_commit()?;
            self.state.read().config.clone()
        };
        if !config.git.push {
            return Ok(PushState::Disabled);
        }
        let target = git::sync_target(&self.root, &config)?;
        let incoming = git::fetch_candidate(&self.root, &target, config.git.push_timeout_seconds)?;
        let local = {
            let _lock = self.lock_repository()?;
            if self.state.read().config.git != config.git {
                bail!("Git settings changed during synchronization; retry");
            }
            let mut local = git::head(&self.root)?;
            if local != self.state.read().head {
                bail!("live branch changed during synchronization; retry");
            }
            if let Some(incoming) = incoming {
                git::acknowledge_remote(&self.root, &target, &incoming)?;
                if incoming != local && git::is_ancestor(&self.root, &local, &incoming)? {
                    let state = self.load_candidate(&incoming, true)?;
                    if state.config.server != self.state.read().config.server {
                        write_atomic(
                            &self.git_dir.join("mdstore/restart.json"),
                            &serde_json::to_vec(&(&local, &incoming))?,
                        )?;
                        bail!(
                            "validated server configuration staged; restart required to activate"
                        );
                    }
                    git::activate_candidate(&self.root, &local, &incoming)?;
                    *self.state.write() = Arc::new(state);
                    self.reindex_notify.notify_one();
                    local = incoming;
                } else if !git::is_ancestor(&self.root, &incoming, &local)? {
                    self.set_blocked(Some(WriteBlock::Diverged))?;
                    return Ok(PushState::Diverged);
                }
            }
            if matches!(*self.blocked.read(), Some(WriteBlock::Diverged)) {
                self.set_blocked(None)?;
            }
            local
        };
        git::push_snapshot(&self.root, &target, &local, config.git.push_timeout_seconds)?;
        Ok(PushState::Pushed)
    }

    pub(crate) async fn synchronize(self: Arc<Self>) {
        let mut retry = 1_u64;
        loop {
            let store = Arc::clone(&self);
            let enabled = self.state.read().config.git.push;
            let result = if enabled {
                blocking(move || store.push()).await
            } else {
                Ok(PushState::Disabled)
            };
            if matches!(result, Ok(PushState::Pushed | PushState::Disabled)) {
                retry = 1;
                tokio::select! {
                    _ = self.sync_notify.notified() => {},
                    _ = tokio::time::sleep(Duration::from_secs(30)) => {},
                }
            } else {
                tokio::time::sleep(Duration::from_secs(retry)).await;
                retry = (retry * 2).min(300);
            }
        }
    }

    fn refresh_external_commit(&self) -> Result<()> {
        let current = git::head(&self.root)?;
        if current == self.state.read().head {
            return Ok(());
        }
        let state = self.load_candidate(&current, false).inspect_err(|error| {
            let _ = self.set_external_blocked(error.to_string());
        })?;
        *self.state.write() = Arc::new(state);
        self.reindex_notify.notify_one();
        if matches!(*self.blocked.read(), Some(WriteBlock::ExternalCommit(_))) {
            self.set_blocked(None)?;
        }
        Ok(())
    }

    fn load_candidate(&self, current: &str, allow_restart: bool) -> Result<StoreState> {
        let config = Config::from_yaml(&git::read_text(&self.root, current, "config.yaml")?)?;
        let current_state = self.state.read().clone();
        if !allow_restart && config.server != current_state.config.server {
            bail!("external commit changes server configuration; restart required");
        }
        let provider = if config.provider != current_state.config.provider {
            let Some(factory) = self.provider_factory else {
                let reason = "external commit changes provider configuration; restart required";
                bail!(reason);
            };
            factory(config.provider.clone())
        } else {
            current_state.provider.clone()
        };
        load_snapshot(
            &self.root,
            current.to_owned(),
            config,
            provider,
            current_state.generation + 1,
        )
    }

    fn activate_staged_restart(&self) -> Result<()> {
        let _lock = self.lock_repository()?;
        let path = self.git_dir.join("mdstore/restart.json");
        if !path.exists() {
            return Ok(());
        }
        let (base, candidate): (String, String) = serde_json::from_slice(&fs::read(&path)?)?;
        if git::head(&self.root)? == base {
            if !git::is_ancestor(&self.root, &base, &candidate)? {
                bail!("staged restart is not a fast-forward");
            }
            let _ = self.load_candidate(&candidate, true)?;
            git::activate_candidate(&self.root, &base, &candidate)?;
        }
        fs::remove_file(path)?;
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
        if matches!(*self.blocked.read(), Some(WriteBlock::Diverged)) {
            return Ok(());
        }
        self.set_blocked(Some(WriteBlock::ExternalCommit(reason)))
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

    fn read_receipt(&self, digest: &str) -> Result<Option<Vec<String>>> {
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
        Ok(Some(stored.touched_paths))
    }

    fn write_receipt(
        &self,
        digest: &str,
        touched_paths: &[String],
        preimages: &Images,
        postimages: &Images,
    ) -> Result<()> {
        let stored = StoredReceipt {
            touched_paths: touched_paths.to_vec(),
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

    fn recover_pending(&self, digest: &str) -> Result<Option<Vec<String>>> {
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
        self.write_receipt(
            digest,
            &pending.touched_paths,
            &pending.preimages,
            &pending.postimages,
        )?;
        self.remove_pending(digest)?;
        Ok(Some(pending.touched_paths))
    }

    fn replay_state(&self, preimages: &Images, postimages: &Images) -> Result<ReplayState> {
        if !preimages.keys().eq(postimages.keys()) {
            bail!("receipt preimages and postimages do not cover the same paths");
        }
        if preimages.is_empty() {
            return Ok(ReplayState::Applied);
        }
        let head = self.state.read().head.clone();
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

    fn current_replication(&self) -> ReplicationStatus {
        let destination = git::sync_target(&self.root, &self.state.read().config)
            .ok()
            .map(|target| target.identity);
        let status = self.replication.read();
        if status.destination == destination {
            status.clone()
        } else {
            ReplicationStatus {
                destination,
                ..ReplicationStatus::default()
            }
        }
    }

    fn set_blocked(&self, reason: Option<WriteBlock>) -> Result<()> {
        let path = self.git_dir.join("mdstore/blocked");
        if let Some(reason) = &reason {
            write_atomic(&path, &serde_json::to_vec(reason)?)?;
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

fn load_snapshot(
    root: &Path,
    head: String,
    config: Config,
    provider: Arc<dyn RetrievalProvider>,
    generation: u64,
) -> Result<StoreState> {
    let pages = load_pages(root, &head, &config)?;
    let config_files = load_config_files(root, &head)?;
    let templates = crate::template::Templates::compile(&config_files)
        .map_err(|findings| ValidationError { findings })?;
    let (parsed, edges) =
        validate_corpus(&pages, &templates).map_err(|findings| ValidationError { findings })?;
    let sidecars: Vec<_> = pages
        .keys()
        .map(|path| {
            sidecar::sidecar_path(Path::new(path))
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    git::ensure_ignored_at(root, &head, sidecars.iter().map(String::as_str))?;
    let index = build_index(root, &config, &pages, &parsed, &edges, provider.as_ref());
    Ok(StoreState {
        head,
        config,
        provider,
        generation,
        templates: Arc::new(templates),
        config_files: Arc::new(config_files),
        pages: Arc::new(pages),
        parsed: Arc::new(parsed),
        edges: Arc::new(edges),
        index: Arc::new(index),
    })
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

fn prepare_reindex(root: &Path, snapshot: Arc<StoreState>, force: bool) -> Result<ReindexContext> {
    let config = &snapshot.config;
    let pages = &snapshot.pages;
    let parsed = &snapshot.parsed;
    let provider = &snapshot.provider;
    let mut paths: BTreeSet<String> = pages.keys().cloned().collect();
    paths.extend(orphaned_sidecar_sources(root, &paths)?);
    let provider_identity = provider.embedding_provider_identity();
    let model = provider.model().to_owned();
    let dimensions = provider.dimensions();
    ensure_sidecars_ignored(root, paths.iter().map(String::as_str))?;
    let mut ready = load_chunks(root, config, pages, parsed, provider.as_ref(), force);
    let mut actions = Vec::new();
    for path in paths {
        let sidecar_path = sidecar::sidecar_path(&root.join(&path));
        let Some(text) = pages.get(&path) else {
            actions.push(ReindexAction::Remove(sidecar_path));
            continue;
        };
        if !force && ready[&path].iter().all(|(_, vector)| vector.is_some()) {
            continue;
        }
        let chunks = ready
            .remove(&path)
            .expect("prepared page")
            .into_iter()
            .map(|(chunk, _)| chunk)
            .collect();
        actions.push(ReindexAction::Embed {
            path,
            text: text.clone(),
            chunks,
            sidecar_path,
        });
    }
    Ok(ReindexContext {
        snapshot,
        provider_identity,
        model,
        dimensions,
        actions,
        ready,
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
    SearchIndex::build(
        parsed,
        edges,
        load_chunks(root, config, pages, parsed, provider, false),
    )
}

fn load_chunks(
    root: &Path,
    config: &Config,
    pages: &HashMap<String, String>,
    parsed: &HashMap<String, ParsedPage>,
    provider: &dyn RetrievalProvider,
    force: bool,
) -> PageChunks {
    let mut all_chunks = HashMap::new();
    let provider_identity = provider.embedding_provider_identity();
    for (path, text) in pages {
        let Some(page) = parsed.get(path) else {
            continue;
        };
        let context = embedding_context(config, page);
        let chunks = chunk_page(text, page, &config.chunking, &context);
        let vectors = if force {
            None
        } else {
            sidecar::read(&sidecar::sidecar_path(&root.join(path)))
                .ok()
                .and_then(|stored| {
                    stored.vectors_for(
                        text,
                        &provider_identity,
                        provider.model(),
                        provider.dimensions(),
                        &chunks,
                    )
                })
        };
        let mut vectors = vectors.unwrap_or_default().into_iter();
        let values = chunks
            .into_iter()
            .map(|chunk| (chunk, vectors.next()))
            .collect();
        all_chunks.insert(path.clone(), values);
    }
    all_chunks
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

fn zeroentropy_provider(config: ProviderConfig) -> Arc<dyn RetrievalProvider> {
    Arc::new(ZeroEntropyProvider::new(config))
}

fn read_blocked(git_dir: &Path) -> Result<Option<WriteBlock>> {
    let path = git_dir.join("mdstore/blocked");
    if path.exists() {
        Ok(Some(serde_json::from_slice(&fs::read(path)?)?))
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

fn embedding_retry_delay(ceiling_ms: &mut u64) -> Duration {
    // Equal jitter avoids both synchronized retries and near-zero retry loops.
    let delay = fastrand::u64(*ceiling_ms / 2..=*ceiling_ms);
    *ceiling_ms = ceiling_ms.saturating_mul(2).min(3_600_000);
    Duration::from_millis(delay)
}

fn request_digest(request: &ApplyEditsRequest) -> Result<String> {
    let bytes = serde_json::to_vec(request)?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}
