//! Repository-owned assets, derivations, and durable worker leases.
use super::*;
use anyhow::ensure;
use serde_json::Value;
use std::io::Read;

const MANIFEST: &str = ".mdstore-artifacts.json";
const LEASE_SECONDS: u64 = 300;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Asset {
    pub oid: String,
    pub size: u64,
}

impl Asset {
    pub(crate) fn pointer(&self) -> String {
        format!(
            "version https://git-lfs.github.com/spec/v1\noid sha256:{}\nsize {}\n",
            self.oid, self.size
        )
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Derivation {
    pub source: String,
    pub recipe: String,
    /// Top-level frontmatter properties consumed by the recipe.
    pub fields: Vec<String>,
    /// Explicit repository-relative content-addressed asset dependencies.
    pub inputs: Vec<String>,
    /// Exact output paths reserved for this derivation.
    pub outputs: Vec<String>,
    #[serde(default)]
    pub reference_namespace: Option<String>,
    #[serde(default)]
    pub publication: Option<Publication>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Publication {
    pub fingerprint: String,
    pub source_revision: String,
    pub attempt: String,
    pub hashes: BTreeMap<String, String>,
    #[serde(default)]
    pub references: BTreeMap<String, Asset>,
}

#[derive(Debug, Default, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Manifest {
    pub assets: BTreeMap<String, Asset>,
    pub derivations: BTreeMap<String, Derivation>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct Job {
    pub id: String,
    pub source: String,
    pub recipe: String,
    pub fingerprint: String,
    pub source_revision: String,
    pub status: String,
    pub attempt: Option<String>,
    pub expires: u64,
    pub error: Option<String>,
    pub progress: Option<Value>,
    pub failures: u32,
    #[serde(default)]
    pub references: BTreeMap<String, Asset>,
    #[serde(default)]
    pub rerun: bool,
}

#[derive(Debug, Serialize)]
pub(crate) struct Assignment {
    pub job: Job,
    pub recording: String,
    pub inputs: BTreeMap<String, Asset>,
    pub outputs: Vec<String>,
    pub references: BTreeMap<String, Asset>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Completion {
    pub attempt: String,
    pub outputs: BTreeMap<String, String>,
    #[serde(default)]
    pub assets: BTreeMap<String, Asset>,
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

pub(crate) fn secret() -> Result<String> {
    let mut bytes = [0_u8; 32];
    fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

pub(super) fn load_manifest(root: &Path, revision: &str) -> Result<Manifest> {
    // A missing manifest is distinct from an invalid or unreadable tracked one.
    let output = std::process::Command::new("git")
        .current_dir(root)
        .args(["ls-tree", "--name-only", revision, "--", MANIFEST])
        .output()?;
    ensure!(output.status.success(), "cannot inspect artifact manifest");
    if output.stdout.is_empty() {
        return Ok(Manifest::default());
    }
    let manifest: Manifest = serde_json::from_str(&git::read_text(root, revision, MANIFEST)?)?;
    let mut owned = BTreeSet::new();
    for (path, asset) in &manifest.assets {
        validate_repo_path(path)?;
        validate_oid(&asset.oid)?;
        let stored = git::read_text(root, revision, path)?;
        if stored != asset.pointer() {
            ensure!(
                crate::media::git_text(path)
                    && stored.len() as u64 == asset.size
                    && crate::validation::source_hash(&stored) == asset.oid,
                "asset content mismatch: {path}"
            );
            // Inline Git contents can rebuild the local object cache after a clone.
            let object = git::git_dir(root)?
                .join("lfs/objects")
                .join(&asset.oid[..2])
                .join(&asset.oid[2..4])
                .join(&asset.oid);
            if !object.exists() {
                write_atomic(&object, stored.as_bytes())?;
            }
        }
    }
    for derivation in manifest.derivations.values() {
        validate_repo_path(&derivation.source)?;
        for path in &derivation.outputs {
            validate_repo_path(path)?;
            ensure!(owned.insert(path), "output has multiple owners: {path}");
        }
        if let Some(publication) = &derivation.publication {
            ensure!(
                publication.hashes.keys().collect::<BTreeSet<_>>()
                    == derivation.outputs.iter().collect(),
                "publication output mismatch"
            );
            for (path, hash) in &publication.hashes {
                ensure!(
                    crate::validation::source_hash(&git::read_text(root, revision, path)?) == *hash,
                    "derived output was modified outside publication: {path}"
                );
            }
        }
    }
    Ok(manifest)
}

pub(crate) fn validate_oid(oid: &str) -> Result<()> {
    ensure!(
        oid.len() == 64
            && oid
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "invalid SHA-256 object ID"
    );
    Ok(())
}

fn fingerprint(definition: &Derivation, state: &StoreState, manifest: &Manifest) -> Result<String> {
    let page = state
        .parsed
        .get(&definition.source)
        .context("derivation source is missing")?;
    let input: BTreeMap<_, _> = definition
        .fields
        .iter()
        .map(|key| {
            (
                key,
                page.frontmatter.get(key).cloned().unwrap_or(Value::Null),
            )
        })
        .collect();
    let assets: BTreeMap<_, _> = definition
        .inputs
        .iter()
        .map(|path| {
            Ok((
                path,
                manifest.assets.get(path).context("missing input asset")?,
            ))
        })
        .collect::<Result<_>>()?;
    Ok(crate::validation::source_hash(&serde_json::to_string(&(
        &definition.recipe,
        input,
        assets,
    ))?))
}

fn reference_is_current(path: &str, state: &StoreState, manifest: &Manifest) -> Result<bool> {
    if let Some(definition) = manifest
        .derivations
        .values()
        .find(|d| d.outputs.iter().any(|p| p == path))
    {
        return Ok(definition.publication.as_ref().is_some_and(|p| {
            fingerprint(definition, state, manifest).is_ok_and(|f| p.fingerprint == f)
        }));
    }
    Ok(false)
}

impl Manifest {
    pub(super) fn check_pages(
        &self,
        pages: &std::collections::HashMap<String, String>,
    ) -> Result<()> {
        for definition in self.derivations.values() {
            ensure!(
                pages.contains_key(&definition.source),
                "derivation source must remain in the corpus: {}",
                definition.source
            );
            if definition.publication.is_some() {
                for path in definition.outputs.iter().filter(|p| p.ends_with(".md")) {
                    ensure!(
                        pages.contains_key(path),
                        "derived document must remain in the corpus: {path}"
                    );
                }
            }
        }
        Ok(())
    }

    pub(super) fn owned(&self, path: &str) -> bool {
        self.derivations
            .values()
            .any(|d| d.outputs.iter().any(|p| p == path))
    }
    pub(super) fn check_edits(&self, changes: &HashMap<String, Option<String>>) -> Result<()> {
        for (path, content) in changes {
            ensure!(!self.owned(path), "derived document is read-only: {path}");
            ensure!(
                content.is_some() || !self.derivations.values().any(|d| d.source == *path),
                "remove the derivation before deleting its source: {path}"
            );
        }
        Ok(())
    }
}

#[derive(Debug)]
pub(crate) struct PreconditionFailed;
impl std::fmt::Display for PreconditionFailed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("file precondition failed")
    }
}
impl std::error::Error for PreconditionFailed {}

impl Store {
    pub(crate) fn derivations(&self) -> Result<Value> {
        Ok(serde_json::to_value(
            &self.state.read().artifacts.derivations,
        )?)
    }

    pub(crate) fn object_path(&self, oid: &str) -> Result<PathBuf> {
        validate_oid(oid)?;
        Ok(self
            .git_dir
            .join("lfs/objects")
            .join(&oid[..2])
            .join(&oid[2..4])
            .join(oid))
    }

    pub(crate) fn object_temporary(&self) -> Result<tempfile::NamedTempFile> {
        let dir = self.git_dir.join("lfs/tmp");
        fs::create_dir_all(&dir)?;
        Ok(tempfile::NamedTempFile::new_in(dir)?)
    }

    pub(crate) fn store_object(&self, mut file: tempfile::NamedTempFile) -> Result<Asset> {
        use std::io::{Seek, SeekFrom};
        file.seek(SeekFrom::Start(0))?;
        let mut hash = Sha256::new();
        let mut size = 0;
        let mut buffer = [0_u8; 65536];
        loop {
            let count = file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            hash.update(&buffer[..count]);
            size += count as u64;
        }
        let asset = Asset {
            oid: format!("{:x}", hash.finalize()),
            size,
        };
        let path = self.object_path(&asset.oid)?;
        fs::create_dir_all(path.parent().context("object parent")?)?;
        file.as_file().sync_all()?;
        file.persist(path).map_err(|error| error.error)?;
        Ok(asset)
    }

    fn asset_representation(&self, path: &str, asset: &Asset) -> Result<String> {
        if crate::media::git_text(path) {
            ensure!(
                asset.size <= 16 * 1024 * 1024,
                "text file exceeds upload limit"
            );
            let content = fs::read_to_string(self.object_path(&asset.oid)?)?;
            ensure!(
                crate::validation::source_hash(&content) == asset.oid,
                "asset content mismatch"
            );
            Ok(content)
        } else {
            Ok(asset.pointer())
        }
    }

    pub(crate) fn put_asset(
        &self,
        path: String,
        asset: Asset,
        expected: Option<String>,
    ) -> Result<()> {
        let _guard = self.lock_repository()?;
        self.refresh_external_commit()?;
        validate_repo_path(&path)?;
        ensure!(
            !path.ends_with(".md") && !is_config_resource_path(&path) && !path.starts_with('.'),
            "reserved asset path"
        );
        let current = self.state.read().clone();
        let mut manifest = (*current.artifacts).clone();
        ensure!(!manifest.owned(&path), "derived asset is read-only");
        if manifest.assets.get(&path).map(|a| &a.oid) != expected.as_ref() {
            return Err(PreconditionFailed.into());
        }
        if manifest.assets.get(&path) == Some(&asset) {
            return Ok(());
        }
        ensure!(
            manifest.assets.contains_key(&path) || !git::is_tracked(&self.root, &path)?,
            "path already exists"
        );
        ensure!(
            fs::metadata(self.object_path(&asset.oid)?)?.len() == asset.size,
            "object size mismatch"
        );
        let pointer = self.asset_representation(&path, &asset)?;
        manifest.assets.insert(path.clone(), asset);
        self.commit_artifacts(
            &current,
            &manifest,
            vec![(path, Some(pointer))],
            "Write asset",
        )?;
        Ok(())
    }

    pub(crate) fn delete_asset(&self, path: &str, expected: &str) -> Result<()> {
        let _guard = self.lock_repository()?;
        self.refresh_external_commit()?;
        let current = self.state.read().clone();
        let mut manifest = (*current.artifacts).clone();
        if manifest.assets.get(path).map(|a| a.oid.as_str()) != Some(expected) {
            return Err(PreconditionFailed.into());
        }
        ensure!(!manifest.owned(path), "derived asset is read-only");
        ensure!(
            !manifest
                .derivations
                .values()
                .any(|d| d.inputs.iter().any(|p| p == path)),
            "asset is an input to a derivation"
        );
        manifest.assets.remove(path);
        self.commit_artifacts(
            &current,
            &manifest,
            vec![(path.into(), None)],
            "Delete asset",
        )?;
        Ok(())
    }

    pub(crate) fn asset(&self, path: &str) -> Result<Asset> {
        self.state
            .read()
            .artifacts
            .assets
            .get(path)
            .cloned()
            .context("unknown asset")
    }

    pub(crate) fn create_derivation(&self, mut definition: Derivation) -> Result<String> {
        let _guard = self.lock_repository()?;
        self.refresh_external_commit()?;
        let current = self.state.read().clone();
        ensure!(
            definition.publication.is_none(),
            "publication is server-owned"
        );
        ensure!(
            current.pages.contains_key(&definition.source),
            "source document is missing"
        );
        ensure!(
            !definition.recipe.is_empty() && !definition.fields.is_empty(),
            "recipe and input fields required"
        );
        ensure!(!definition.outputs.is_empty(), "outputs required");
        definition.outputs.sort();
        definition.outputs.dedup();
        let mut manifest = (*current.artifacts).clone();
        for path in &definition.outputs {
            validate_repo_path(path)?;
            ensure!(
                !crate::template::is_template(path)
                    && !is_config_resource_path(path)
                    && !path.starts_with('.')
                    && !definition.inputs.contains(path),
                "reserved output path"
            );
            ensure!(
                !manifest.owned(path) && !git::is_tracked(&self.root, path)?,
                "output already exists or is reserved: {path}"
            );
        }
        for path in &definition.inputs {
            ensure!(
                manifest.assets.contains_key(path),
                "unknown input asset: {path}"
            );
        }
        let id = crate::validation::source_hash(&serde_json::to_string(&definition)?);
        manifest.derivations.insert(id.clone(), definition);
        self.commit_artifacts(&current, &manifest, vec![], "Register derivation")?;
        Ok(id)
    }

    pub(crate) fn update_derivation_inputs(&self, id: &str, inputs: Vec<String>) -> Result<()> {
        let _guard = self.lock_repository()?;
        self.refresh_external_commit()?;
        let current = self.state.read().clone();
        let mut manifest = (*current.artifacts).clone();
        for path in &inputs {
            ensure!(
                manifest.assets.contains_key(path),
                "unknown input asset: {path}"
            );
        }
        let definition = manifest
            .derivations
            .get_mut(id)
            .context("unknown derivation")?;
        ensure!(
            !inputs.iter().any(|p| definition.outputs.contains(p)),
            "output cannot be an input"
        );
        if definition.inputs == inputs {
            return Ok(());
        }
        definition.inputs = inputs;
        self.commit_artifacts(&current, &manifest, vec![], "Update derivation inputs")?;
        Ok(())
    }

    fn commit_artifacts(
        &self,
        current: &StoreState,
        manifest: &Manifest,
        mut changes: Vec<(String, Option<String>)>,
        summary: &str,
    ) -> Result<String> {
        ensure!(
            self.blocked.read().is_none(),
            "repository writes are blocked"
        );
        let mut attributes = if git::is_tracked(&self.root, ".gitattributes")? {
            git::read_text(&self.root, &current.head, ".gitattributes")?
        } else {
            String::new()
        };
        for path in manifest.assets.keys() {
            let pattern = path
                .chars()
                .flat_map(|c| {
                    if "*?[\\".contains(c) {
                        vec!['\\', c]
                    } else {
                        vec![c]
                    }
                })
                .collect::<String>();
            let line = format!(
                "{} filter=lfs diff=lfs merge=lfs -text",
                serde_json::to_string(&pattern)?
            );
            if crate::media::git_text(path) {
                attributes = attributes
                    .lines()
                    .filter(|existing| *existing != line)
                    .map(|line| format!("{line}\n"))
                    .collect();
                continue;
            }
            if !attributes.lines().any(|existing| existing == line) {
                if !attributes.is_empty() && !attributes.ends_with('\n') {
                    attributes.push('\n');
                }
                attributes.push_str(&line);
                attributes.push('\n');
            }
        }
        if !attributes.is_empty() || git::is_tracked(&self.root, ".gitattributes")? {
            changes.push((".gitattributes".into(), Some(attributes)));
        }
        changes.push((
            MANIFEST.into(),
            Some(serde_json::to_string_pretty(manifest)?),
        ));
        for (path, _) in &changes {
            ensure_repository_path_safe(&self.root, path)?;
            ensure!(
                git::is_tracked(&self.root, path)?
                    || fs::symlink_metadata(self.root.join(path)).is_err(),
                "untracked repository path already exists: {path}"
            );
        }
        let tree = git::stage_tree(&self.root, &current.head, &changes)?
            .context("empty artifact publication")?;
        let candidate = self.load_candidate(&tree, false)?;
        current
            .templates
            .validate_changes(&current.pages, &candidate.pages)
            .map_err(|findings| ValidationError { findings })?;
        let paths = changes.iter().map(|(p, _)| p.clone()).collect::<Vec<_>>();
        git::write_changes(&self.root, &changes)
            .map_err(|error| rollback_failure(&self.root, &paths, error))?;
        let commit = git::commit_tree(&self.root, &tree, &current.head, summary)
            .map_err(|error| rollback_failure(&self.root, &paths, error))?;
        git::sync_index(&self.root, &commit, &paths)?;
        let mut state = candidate;
        state.head = commit.clone();
        *self.state.write() = Arc::new(state);
        self.publish_revision();
        self.reindex_notify.notify_one();
        self.sync_notify.notify_one();
        Ok(commit)
    }

    fn jobs_path(&self) -> PathBuf {
        self.git_dir.join("mdstore/jobs.json")
    }

    fn save_jobs(&self, jobs: &BTreeMap<String, Job>) -> Result<()> {
        let bytes = serde_json::to_vec(jobs)?;
        let previous = match fs::read(self.jobs_path()) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
            Err(error) => return Err(error.into()),
        };
        if previous == bytes {
            return Ok(());
        }
        write_atomic(&self.jobs_path(), &bytes)?;
        self.changes
            .send_modify(|change| change.jobs = change.jobs.wrapping_add(1));
        Ok(())
    }

    fn reconcile_jobs(&self) -> Result<(Manifest, BTreeMap<String, Job>)> {
        let state = self.state.read();
        let manifest = (*state.artifacts).clone();
        let path = self.jobs_path();
        let mut jobs: BTreeMap<String, Job> = if path.exists() {
            serde_json::from_slice(&fs::read(path)?)?
        } else {
            BTreeMap::new()
        };
        jobs.retain(|id, _| manifest.derivations.contains_key(id));
        for (id, definition) in &manifest.derivations {
            let fingerprint = fingerprint(definition, &state, &manifest)?;
            let published = definition
                .publication
                .as_ref()
                .is_some_and(|p| p.fingerprint == fingerprint);
            let job = jobs.entry(id.clone()).or_insert_with(|| Job {
                id: id.clone(),
                source: definition.source.clone(),
                recipe: definition.recipe.clone(),
                fingerprint: fingerprint.clone(),
                source_revision: state.head.clone(),
                status: "queued".into(),
                attempt: None,
                expires: 0,
                error: None,
                progress: None,
                failures: 0,
                references: BTreeMap::new(),
                rerun: false,
            });
            if job.fingerprint != fingerprint {
                job.fingerprint = fingerprint;
                job.status = "queued".into();
                job.attempt = None;
                job.error = None;
                job.failures = 0;
                job.progress = None;
                job.expires = 0;
            }
            if published
                && (!job.rerun
                    || definition
                        .publication
                        .as_ref()
                        .is_some_and(|p| Some(&p.attempt) == job.attempt.as_ref()))
            {
                job.status = "current".into();
                job.attempt = None;
                job.rerun = false;
            } else if job.status == "running" && job.expires <= now() {
                job.status = "queued".into();
                job.attempt = None;
            }
        }
        Ok((manifest, jobs))
    }

    pub(crate) fn jobs(&self) -> Result<Vec<Job>> {
        let _guard = self.lock_repository()?;
        self.refresh_external_commit()?;
        let (_, jobs) = self.reconcile_jobs()?;
        self.save_jobs(&jobs)?;
        Ok(jobs
            .into_values()
            .map(|mut j| {
                j.attempt = None;
                j
            })
            .collect())
    }

    pub(crate) fn retry_job(&self, id: &str) -> Result<()> {
        let _guard = self.lock_repository()?;
        self.refresh_external_commit()?;
        let (_, mut jobs) = self.reconcile_jobs()?;
        let job = jobs.get_mut(id).context("unknown job")?;
        job.rerun = true;
        job.status = "queued".into();
        job.attempt = None;
        job.failures = 0;
        job.expires = 0;
        job.error = None;
        job.progress = None;
        self.save_jobs(&jobs)
    }

    pub(crate) fn remove_derivation(&self, id: &str) -> Result<()> {
        let _guard = self.lock_repository()?;
        self.refresh_external_commit()?;
        let current = self.state.read().clone();
        let mut manifest = (*current.artifacts).clone();
        let definition = manifest
            .derivations
            .remove(id)
            .context("unknown derivation")?;
        let mut changes = vec![];
        for path in definition.outputs {
            manifest.assets.remove(&path);
            if git::is_tracked(&self.root, &path)? {
                changes.push((path, None));
            }
        }
        self.commit_artifacts(
            &current,
            &manifest,
            changes,
            "Remove derivation and its published outputs",
        )?;
        Ok(())
    }

    pub(crate) fn claim_job(&self, recipes: &[String]) -> Result<Option<Assignment>> {
        let _guard = self.lock_repository()?;
        self.refresh_external_commit()?;
        let (manifest, mut jobs) = self.reconcile_jobs()?;
        let assignment = if let Some(job) = jobs
            .values_mut()
            .find(|j| j.status == "queued" && j.expires <= now() && recipes.contains(&j.recipe))
        {
            job.source_revision = self.state.read().head.clone();
            job.status = "running".into();
            job.attempt = Some(secret()?);
            job.expires = now() + LEASE_SECONDS;
            let definition = &manifest.derivations[&job.id];
            job.references.clear();
            if let Some(namespace) = &definition.reference_namespace {
                for (path, asset) in &manifest.assets {
                    if definition.outputs.contains(path)
                        || asset.size > 16 * 1024 * 1024
                        || !reference_is_current(path, &self.state.read(), &manifest)?
                    {
                        continue;
                    }
                    if let Ok(bytes) = fs::read(self.object_path(&asset.oid)?)
                        && let Ok(vectors) = serde_json::from_slice::<VectorArtifact>(&bytes)
                        && vectors.space.namespace == *namespace
                    {
                        job.references.insert(path.clone(), asset.clone());
                    }
                }
            }
            Some(Assignment {
                job: job.clone(),
                recording: self.state.read().pages[&job.source].clone(),
                inputs: definition
                    .inputs
                    .iter()
                    .map(|p| (p.clone(), manifest.assets[p].clone()))
                    .collect(),
                outputs: definition.outputs.clone(),
                references: job.references.clone(),
            })
        } else {
            None
        };
        self.save_jobs(&jobs)?;
        Ok(assignment)
    }

    pub(crate) fn leased_asset(&self, id: &str, attempt: &str, path: &str) -> Result<Asset> {
        let _guard = self.lock_repository()?;
        let (manifest, jobs) = self.reconcile_jobs()?;
        check_lease(jobs.get(id), attempt)?;
        if let Some(asset) = jobs[id].references.get(path) {
            return Ok(asset.clone());
        }
        ensure!(
            manifest.derivations[id].inputs.iter().any(|p| p == path),
            "input is not assigned to this job"
        );
        Ok(manifest.assets[path].clone())
    }

    pub(crate) fn check_output_lease(&self, id: &str, attempt: &str, path: &str) -> Result<()> {
        let _guard = self.lock_repository()?;
        let (manifest, jobs) = self.reconcile_jobs()?;
        check_lease(jobs.get(id), attempt)?;
        ensure!(
            !path.ends_with(".md") && manifest.derivations[id].outputs.iter().any(|p| p == path),
            "output is not assigned to this job"
        );
        Ok(())
    }

    pub(crate) fn heartbeat(
        &self,
        id: &str,
        attempt: &str,
        progress: Value,
        failure: Option<String>,
    ) -> Result<()> {
        let _guard = self.lock_repository()?;
        let (_, mut jobs) = self.reconcile_jobs()?;
        check_lease(jobs.get(id), attempt)?;
        let job = jobs.get_mut(id).context("unknown job")?;
        job.expires = now() + LEASE_SECONDS;
        job.progress = Some(progress);
        if let Some(error) = failure {
            job.failures += 1;
            job.error = Some(error);
            job.attempt = None;
            job.status = if job.failures >= 3 {
                "failed"
            } else {
                "queued"
            }
            .into();
            job.expires = now() + 30 * u64::from(job.failures);
        }
        self.save_jobs(&jobs)
    }

    pub(crate) fn complete_job(&self, id: &str, completion: Completion) -> Result<()> {
        let _guard = self.lock_repository()?;
        self.refresh_external_commit()?;
        let (mut manifest, jobs) = self.reconcile_jobs()?;
        let definition = manifest.derivations.get(id).context("unknown derivation")?;
        if let Some(publication) = definition
            .publication
            .as_ref()
            .filter(|p| p.attempt == completion.attempt)
        {
            let mut hashes: BTreeMap<_, _> = completion
                .outputs
                .iter()
                .map(|(path, text)| (path.clone(), crate::validation::source_hash(text)))
                .collect();
            for (path, asset) in &completion.assets {
                ensure!(
                    manifest.assets.get(path) == Some(asset),
                    "completed attempt has different output bytes"
                );
                hashes.insert(
                    path.clone(),
                    crate::validation::source_hash(&git::read_text(
                        &self.root,
                        &self.state.read().head,
                        path,
                    )?),
                );
            }
            ensure!(
                hashes == publication.hashes,
                "completed attempt has different output bytes"
            );
            return Ok(());
        }
        check_lease(jobs.get(id), &completion.attempt)?;
        for (path, asset) in &jobs[id].references {
            ensure!(
                manifest.assets.get(path) == Some(asset)
                    && reference_is_current(path, &self.state.read(), &manifest)?,
                "reference evidence changed; retry identification"
            );
        }
        ensure!(
            completion.outputs.keys().all(|p| p.ends_with(".md")),
            "text outputs must be Markdown"
        );
        ensure!(
            completion.assets.keys().all(|p| !p.ends_with(".md")),
            "Markdown outputs must be text"
        );
        ensure!(
            completion
                .outputs
                .keys()
                .chain(completion.assets.keys())
                .collect::<BTreeSet<_>>()
                == definition.outputs.iter().collect(),
            "outputs do not match assignment"
        );
        // Preserve rejected candidates for diagnosis/revalidation without another inference run.
        write_atomic(
            &self
                .git_dir
                .join("mdstore/candidates")
                .join(format!("{}.json", completion.attempt)),
            &serde_json::to_vec(&completion)?,
        )?;
        let mut outputs = completion.outputs;
        for (path, asset) in completion.assets {
            ensure!(
                fs::metadata(self.object_path(&asset.oid)?)?.len() == asset.size,
                "output object missing or wrong size"
            );
            outputs.insert(path.clone(), self.asset_representation(&path, &asset)?);
            manifest.assets.insert(path, asset);
        }
        let definition = manifest
            .derivations
            .get_mut(id)
            .context("unknown derivation")?;
        definition.publication = Some(Publication {
            fingerprint: jobs[id].fingerprint.clone(),
            source_revision: jobs[id].source_revision.clone(),
            attempt: completion.attempt,
            references: jobs[id].references.clone(),
            hashes: outputs
                .iter()
                .map(|(p, t)| (p.clone(), crate::validation::source_hash(t)))
                .collect(),
        });
        let current = self.state.read().clone();
        self.commit_artifacts(
            &current,
            &manifest,
            outputs.into_iter().map(|(p, t)| (p, Some(t))).collect(),
            "Publish derived documents",
        )?;
        Ok(())
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::{InputType, RerankResult};
    struct Provider;
    #[async_trait::async_trait]
    impl RetrievalProvider for Provider {
        async fn embed(&self, _: InputType, input: &[String]) -> Result<Vec<Vec<f32>>> {
            Ok(vec![vec![1.0, 0.0]; input.len()])
        }
        async fn rerank(&self, _: &str, _: &[String], _: usize) -> Result<Vec<RerankResult>> {
            Ok(vec![])
        }
        fn model(&self) -> &str {
            "fixture"
        }
        fn dimensions(&self) -> usize {
            2
        }
        fn embedding_provider_identity(&self) -> String {
            "fixture".into()
        }
    }
    fn git(root: &Path, args: &[&str]) {
        let output = std::process::Command::new("git")
            .current_dir(root)
            .args(args)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    pub(crate) fn fixture() -> (tempfile::TempDir, Arc<Store>) {
        let dir = tempfile::tempdir().unwrap();
        git(dir.path(), &["init", "-q"]);
        git(dir.path(), &["config", "user.name", "Fixture"]);
        git(
            dir.path(),
            &["config", "user.email", "fixture@example.invalid"],
        );
        fs::write(
            dir.path().join("config.yaml"),
            "documents:\n  include: ['**/*.md']\nprovider:\n  dimensions: 2\ngit:\n  push: false\n",
        )
        .unwrap();
        fs::write(dir.path().join(".gitignore"), "*.mdstore\n").unwrap();
        fs::write(
            dir.path().join("recording.md"),
            "---\nhotwords: [hello]\n---\n# Recording\n",
        )
        .unwrap();
        git(dir.path(), &["add", "."]);
        git(
            dir.path(),
            &["-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
        );
        let store = Store::open_with_provider(dir.path(), Arc::new(Provider)).unwrap();
        (dir, store)
    }
    fn setup(store: &Store) -> String {
        let mut file = store.object_temporary().unwrap();
        file.write_all(b"audio bytes").unwrap();
        let asset = store.store_object(file).unwrap();
        store.put_asset("audio.m4a".into(), asset, None).unwrap();
        store
            .create_derivation(Derivation {
                source: "recording.md".into(),
                recipe: "fixture-v1".into(),
                fields: vec!["hotwords".into()],
                inputs: vec!["audio.m4a".into()],
                outputs: vec!["transcript.md".into()],
                publication: None,
                reference_namespace: None,
            })
            .unwrap()
    }
    fn completion(attempt: &str, text: &str) -> Completion {
        Completion {
            attempt: attempt.into(),
            outputs: BTreeMap::from([("transcript.md".into(), text.into())]),
            assets: BTreeMap::new(),
        }
    }
    #[test]
    fn text_assets_are_git_blobs_and_binary_media_use_internal_lfs() {
        let (dir, store) = fixture();
        for (path, bytes) in [
            ("capture.json", b"{\"tracks\":[]}".as_slice()),
            ("audio.wav", b"binary fixture".as_slice()),
        ] {
            let mut object = store.object_temporary().unwrap();
            object.write_all(bytes).unwrap();
            let asset = store.store_object(object).unwrap();
            store.put_asset(path.into(), asset.clone(), None).unwrap();
            let committed = git::read_text(dir.path(), &store.state.read().head, path).unwrap();
            if path.ends_with("json") {
                assert_eq!(committed.as_bytes(), bytes);
                fs::remove_file(store.object_path(&asset.oid).unwrap()).unwrap();
                let reopened = Store::open_with_provider(dir.path(), Arc::new(Provider)).unwrap();
                assert_eq!(
                    fs::read(reopened.object_path(&asset.oid).unwrap()).unwrap(),
                    bytes
                );
            } else {
                assert_eq!(committed, asset.pointer());
            }
        }
        let attributes = fs::read_to_string(dir.path().join(".gitattributes")).unwrap();
        assert!(attributes.contains("audio.wav"));
        assert!(!attributes.contains("capture.json"));
    }

    #[test]
    fn publication_is_validated_readonly_and_recoverable() {
        let (dir, store) = fixture();
        let id = setup(&store);
        let assignment = store.claim_job(&["fixture-v1".into()]).unwrap().unwrap();
        let attempt = assignment.job.attempt.unwrap();
        assert!(
            store
                .complete_job(&id, completion(&attempt, "[broken](missing.md)\n"))
                .is_err()
        );
        assert!(!store.get_page("transcript.md", None).unwrap().exists);
        store
            .complete_job(&id, completion(&attempt, "# Transcript\nHello\n"))
            .unwrap();
        store
            .complete_job(&id, completion(&attempt, "# Transcript\nHello\n"))
            .unwrap();
        assert!(
            store
                .complete_job(&id, completion(&attempt, "# Different replay\n"))
                .is_err()
        );
        assert!(store.get_page("transcript.md", None).unwrap().readonly);
        assert!(
            store
                .apply_edits(&ApplyEditsRequest {
                    edit_summary: "overwrite".into(),
                    edits: vec![EditOperation::ReplacePage {
                        path: "transcript.md".into(),
                        base: "# Transcript\nHello\n".into(),
                        content: "# Tampered\n".into()
                    }]
                })
                .is_err()
        );
        drop(store);
        let reopened = Store::open_with_provider(dir.path(), Arc::new(Provider)).unwrap();
        assert_eq!(reopened.jobs().unwrap()[0].status, "current");
        assert!(reopened.get_page("transcript.md", None).unwrap().readonly);
        let revision = reopened.get_page("transcript.md", None).unwrap().revision;
        reopened
            .complete_job(&id, completion(&attempt, "# Transcript\nHello\n"))
            .unwrap();
        assert_eq!(
            reopened.get_page("transcript.md", None).unwrap().revision,
            revision
        );
    }
    #[test]
    fn changes_fence_old_workers_but_notes_do_not_recompute() {
        let (_dir, store) = fixture();
        let id = setup(&store);
        let job = store
            .claim_job(&["fixture-v1".into()])
            .unwrap()
            .unwrap()
            .job;
        let before = store.get_page("recording.md", None).unwrap().text.unwrap();
        let notes = format!("{before}\nHuman notes.\n");
        store
            .apply_edits(&ApplyEditsRequest {
                edit_summary: "notes".into(),
                edits: vec![EditOperation::ReplacePage {
                    path: "recording.md".into(),
                    base: before,
                    content: notes.clone(),
                }],
            })
            .unwrap();
        assert_eq!(store.jobs().unwrap()[0].fingerprint, job.fingerprint);
        store
            .apply_edits(&ApplyEditsRequest {
                edit_summary: "guidance".into(),
                edits: vec![EditOperation::ReplacePage {
                    path: "recording.md".into(),
                    base: notes.clone(),
                    content: notes.replace("hello", "changed"),
                }],
            })
            .unwrap();
        assert!(
            store
                .complete_job(
                    &id,
                    completion(job.attempt.as_deref().unwrap(), "# Stale\n")
                )
                .is_err()
        );
        let fresh = store.claim_job(&["fixture-v1".into()]).unwrap().unwrap();
        assert_ne!(fresh.job.fingerprint, job.fingerprint);
    }
    #[test]
    fn reference_vectors_are_frozen_filtered_and_fenced() {
        let (_dir, store) = fixture();
        let first = setup(&store);
        let mut definition = store.state.read().artifacts.derivations[&first].clone();
        definition.outputs = vec!["reference.json".into()];
        let reference = store.create_derivation(definition).unwrap();
        // Complete both queued jobs, publishing a confirmed reference vector.
        let space = crate::vectors::EmbeddingSpace {
            namespace: "speakers".into(),
            recipe: "model-v1".into(),
            dimensions: 2,
        };
        let mut file = store.object_temporary().unwrap();
        file.write_all(&serde_json::to_vec(&serde_json::json!({"space": space, "records": [
            {"id": "confirmed", "vector": [1.0, 0.0], "metadata": {"confirmed": true, "identity": "/people/alice.md"}},
            {"id": "unknown", "vector": [1.0, 0.0], "metadata": {"confirmed": false}}
        ]})).unwrap()).unwrap();
        let asset = store.store_object(file).unwrap();
        while let Some(assignment) = store.claim_job(&["fixture-v1".into()]).unwrap() {
            let result = if assignment.job.id == reference {
                Completion {
                    attempt: assignment.job.attempt.unwrap(),
                    outputs: BTreeMap::new(),
                    assets: BTreeMap::from([("reference.json".into(), asset.clone())]),
                }
            } else {
                completion(assignment.job.attempt.as_deref().unwrap(), "# Transcript\n")
            };
            store.complete_job(&assignment.job.id, result).unwrap();
        }
        let mut definition = store.state.read().artifacts.derivations[&first].clone();
        definition.publication = None;
        definition.outputs = vec!["next.md".into()];
        definition.reference_namespace = Some("speakers".into());
        let next = store.create_derivation(definition).unwrap();
        let assignment = store.claim_job(&["fixture-v1".into()]).unwrap().unwrap();
        assert_eq!(assignment.references.len(), 1);
        let matches = store
            .search_artifact_vectors(
                space,
                vec![1.0, 0.0],
                5,
                BTreeMap::from([("confirmed".into(), Value::Bool(true))]),
                Some((next.clone(), assignment.job.attempt.clone().unwrap())),
            )
            .unwrap();
        assert_eq!(matches.as_array().unwrap().len(), 1);
        assert_eq!(
            matches[0]["metadata"]["reference"]["identity"],
            "/people/alice.md"
        );
        store.retry_job(&next).unwrap();
        assert!(
            store
                .leased_asset(
                    &next,
                    assignment.job.attempt.as_deref().unwrap(),
                    "reference.json"
                )
                .is_err()
        );
        let fresh = store.claim_job(&["fixture-v1".into()]).unwrap().unwrap();
        assert_ne!(fresh.job.attempt, assignment.job.attempt);
    }

    #[test]
    fn lease_expiry_limits_inputs_and_requeues_after_restart() {
        let (dir, store) = fixture();
        let id = setup(&store);
        assert!(store.claim_job(&["other-model".into()]).unwrap().is_none());
        let job = store
            .claim_job(&["fixture-v1".into()])
            .unwrap()
            .unwrap()
            .job;
        assert!(
            store
                .leased_asset(&id, job.attempt.as_deref().unwrap(), "other.m4a")
                .is_err()
        );
        let mut jobs: BTreeMap<String, Job> =
            serde_json::from_slice(&fs::read(store.jobs_path()).unwrap()).unwrap();
        jobs.get_mut(&id).unwrap().expires = 0;
        write_atomic(&store.jobs_path(), &serde_json::to_vec(&jobs).unwrap()).unwrap();
        drop(store);
        let store = Store::open_with_provider(dir.path(), Arc::new(Provider)).unwrap();
        let next = store.claim_job(&["fixture-v1".into()]).unwrap().unwrap();
        assert_ne!(next.job.attempt, job.attempt);
        assert!(
            store
                .complete_job(&id, completion(job.attempt.as_deref().unwrap(), "# Old\n"))
                .is_err()
        );
    }
}

fn check_lease(job: Option<&Job>, attempt: &str) -> Result<()> {
    let job = job.context("unknown job")?;
    ensure!(
        job.status == "running" && job.expires > now() && job.attempt.as_deref() == Some(attempt),
        "expired or superseded worker lease"
    );
    Ok(())
}

#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct VectorArtifact {
    pub space: crate::vectors::EmbeddingSpace,
    pub records: Vec<crate::vectors::VectorRecord<Value>>,
}

impl Store {
    pub(crate) fn search_artifact_vectors(
        &self,
        space: crate::vectors::EmbeddingSpace,
        query: Vec<f32>,
        limit: usize,
        filter: BTreeMap<String, Value>,
        lease: Option<(String, String)>,
    ) -> Result<Value> {
        use crate::vectors::VectorCollection;
        let _guard = self.lock_repository()?;
        let (manifest, jobs) = self.reconcile_jobs()?;
        let assets = if let Some((id, attempt)) = lease {
            check_lease(jobs.get(&id), &attempt)?;
            jobs[&id].references.clone()
        } else {
            manifest.assets
        };
        let mut records = Vec::new();
        for (path, asset) in assets {
            if asset.size > 16 * 1024 * 1024 {
                continue;
            }
            let Ok(bytes) = fs::read(self.object_path(&asset.oid)?) else {
                continue;
            };
            let Ok(artifact) = serde_json::from_slice::<VectorArtifact>(&bytes) else {
                continue;
            };
            if artifact.space != space {
                continue;
            }
            for mut record in artifact.records {
                if !filter
                    .iter()
                    .all(|(key, value)| record.metadata.get(key) == Some(value))
                {
                    continue;
                }
                record.metadata = serde_json::json!({"artifact": path, "oid": asset.oid, "reference": record.metadata, "record": record.id});
                record.id = format!("{path}:{}", record.id);
                records.push(record);
            }
        }
        let index = VectorCollection::new(space.clone(), records)?;
        let results = index.search(&space, &query, limit.min(100), |_| true)?;
        Ok(serde_json::json!(results.into_iter().map(|m| serde_json::json!({"similarity": m.similarity, "metadata": m.record.metadata})).collect::<Vec<_>>()))
    }
}
