//! Durable retrieval requests served by the same pull workers as derivations.
use std::{
    collections::{BTreeMap, HashSet},
    fs,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, bail, ensure};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    provider::{revision, validate_result},
    store::artifacts::secret,
};

pub(crate) const OUTPUT: &str = "retrieval/result.json";
const LEASE_SECONDS: u64 = 300;

#[derive(Debug, Clone)]
pub(crate) struct Queue(PathBuf);

#[derive(Debug, Serialize, Deserialize)]
struct Job {
    id: String,
    recipe: String,
    revision: String,
    parameters: Value,
    document: bool,
    deadline: u64,
    attempt: Option<String>,
    expires: u64,
    failures: u32,
    error: Option<String>,
    complete: bool,
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

impl Queue {
    pub(crate) fn new(git_dir: &Path) -> Self {
        Self(git_dir.join("mdstore/retrieval"))
    }

    fn locked<T>(&self, action: impl FnOnce() -> Result<T>) -> Result<T> {
        fs::create_dir_all(&self.0)?;
        let lock = fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(self.0.join("lock"))?;
        lock.lock_exclusive()?;
        action()
    }

    fn path(&self, id: &str) -> Result<PathBuf> {
        ensure!(
            id.starts_with("retrieval-")
                && id[10..].len() == 64
                && id[10..].bytes().all(|b| b.is_ascii_hexdigit()),
            "invalid retrieval job ID"
        );
        Ok(self.0.join(format!("{id}.json")))
    }

    fn read(&self, id: &str) -> Result<Job> {
        serde_json::from_slice(&fs::read(self.path(id)?).context("retrieval job expired or stale")?)
            .context("read retrieval job")
    }

    fn result_path(&self, id: &str) -> Result<PathBuf> {
        Ok(self.path(id)?.with_extension("result"))
    }

    fn remove(&self, job: &Job) -> Result<()> {
        fs::remove_file(self.path(&job.id)?)?;
        match fs::remove_file(self.result_path(&job.id)?) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }

    fn save(&self, job: &Job) -> Result<()> {
        crate::store::write_atomic(&self.path(&job.id)?, &serde_json::to_vec(job)?)
    }

    fn jobs(&self) -> Result<Vec<Job>> {
        let mut jobs = Vec::new();
        for entry in fs::read_dir(&self.0)? {
            let path = entry?.path();
            if path.extension().is_some_and(|v| v == "json") {
                jobs.push(serde_json::from_slice::<Job>(&fs::read(path)?)?);
            } else if path.extension().is_some_and(|v| v == "result")
                && !path.with_extension("json").exists()
            {
                // Recover an interrupted cleanup after the job metadata was removed.
                fs::remove_file(path)?;
            }
        }
        Ok(jobs)
    }

    pub(crate) fn submit(
        &self,
        recipe: &str,
        parameters: Value,
        document: bool,
        timeout: u64,
    ) -> Result<String> {
        self.locked(|| {
            let revision = revision(recipe)?;
            let nonce = if document { String::new() } else { secret()? };
            let id = format!(
                "retrieval-{}",
                crate::validation::source_hash(&format!(
                    "{recipe}:{revision}:{parameters}:{nonce}"
                ))
            );
            if !self.path(&id)?.exists() {
                self.save(&Job {
                    id: id.clone(),
                    recipe: recipe.into(),
                    revision: revision.into(),
                    parameters,
                    document,
                    deadline: now() + if document { 86400 * 7 } else { timeout },
                    attempt: None,
                    expires: 0,
                    failures: 0,
                    error: None,
                    complete: false,
                })?;
            }
            Ok(id)
        })
    }

    pub(crate) fn result(&self, id: &str) -> Result<Option<String>> {
        self.locked(|| {
            let job = self.read(id)?;
            if let Some(error) = job.error {
                bail!("ML worker: {error}");
            }
            if job.complete {
                Ok(Some(fs::read_to_string(self.result_path(id)?)?))
            } else {
                Ok(None)
            }
        })
    }

    fn current(job: &Job, texts: &HashSet<&str>) -> bool {
        job.deadline > now()
            && revision(&job.recipe).is_ok_and(|revision| revision == job.revision)
            && (!job.document
                || job.parameters["texts"].as_array().is_some_and(|items| {
                    items.iter().all(|item| {
                        item["text"]
                            .as_str()
                            .is_some_and(|text| texts.contains(text))
                    })
                }))
    }

    pub(crate) fn clear_documents(&self) -> Result<()> {
        self.locked(|| {
            for job in self.jobs()? {
                if job.document {
                    self.remove(&job)?;
                }
            }
            Ok(())
        })
    }

    pub(crate) fn retry(&self, id: &str) -> Result<()> {
        self.locked(|| {
            let mut job = self.read(id)?;
            ensure!(
                job.document,
                "retry interactive inference by searching again"
            );
            ensure!(
                !job.complete,
                "completed document embeddings require a full reindex"
            );
            job.attempt = None;
            job.error = None;
            job.failures = 0;
            job.expires = 0;
            job.deadline = now() + 86400 * 7;
            self.save(&job)
        })
    }

    pub(crate) fn summaries(
        &self,
        texts: &HashSet<&str>,
    ) -> Result<(Vec<crate::store::artifacts::Job>, bool)> {
        self.locked(|| {
            let mut summaries = Vec::new();
            for job in self.jobs()? {
                if !Self::current(&job, texts) {
                    self.remove(&job)?;
                    continue;
                }
                // Completed retrieval requests are internal cache entries, not published artifacts.
                if job.complete {
                    continue;
                }
                summaries.push(crate::store::artifacts::Job {
                    source: String::new(),
                    recipe: job.recipe,
                    published: false,
                    state: crate::store::artifacts::JobState {
                        id: job.id.clone(),
                        fingerprint: job.id,
                        source_revision: String::new(),
                        status: if job.error.is_some() {
                            "failed"
                        } else if job.attempt.is_some() && job.expires > now() {
                            "running"
                        } else {
                            "queued"
                        }
                        .into(),
                        attempt: None,
                        expires: job.expires,
                        error: job.error,
                        progress: None,
                        failures: job.failures,
                        references: BTreeMap::new(),
                        rerun: false,
                    },
                });
            }
            summaries.sort_by(|a, b| a.state.id.cmp(&b.state.id));
            let fingerprint = crate::validation::source_hash(&serde_json::to_string(&summaries)?);
            let path = self.0.join("status");
            let previous = match fs::read_to_string(&path) {
                Ok(previous) => previous,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => String::new(),
                Err(error) => return Err(error.into()),
            };
            let changed = fingerprint != previous && !(previous.is_empty() && summaries.is_empty());
            if fingerprint != previous {
                crate::store::write_atomic(&path, fingerprint.as_bytes())?;
            }
            Ok((summaries, changed))
        })
    }

    pub(crate) fn claim(
        &self,
        recipes: &[String],
        texts: &HashSet<&str>,
        document: bool,
    ) -> Result<Option<Value>> {
        self.locked(|| {
            let mut jobs = self.jobs()?;
            jobs.sort_by_key(|job| (job.deadline, job.id.clone()));
            let mut assignment = None;
            for mut job in jobs {
                if !Self::current(&job, texts) {
                    self.remove(&job)?;
                    continue;
                }
                if assignment.is_some()
                    || job.document != document
                    || job.complete
                    || job.error.is_some()
                    || job.expires > now()
                    || !recipes.contains(&job.recipe)
                {
                    continue;
                }
                job.attempt = Some(secret()?);
                job.expires = now() + LEASE_SECONDS;
                assignment = Some(json!({
                    "retrieval": true,
                    "job": {"id": job.id, "recipe": job.recipe, "source": "retrieval/source.md",
                        "source_revision": "", "fingerprint": job.id, "attempt": job.attempt},
                    "recording": "", "parameters": {"ml": job.parameters},
                    "inputs": {}, "references": {}, "outputs": [OUTPUT]
                }));
                self.save(&job)?;
            }
            Ok(assignment)
        })
    }

    fn leased(&self, id: &str, attempt: &str, texts: &HashSet<&str>) -> Result<Job> {
        let job = self.read(id)?;
        ensure!(Self::current(&job, texts), "stale retrieval input");
        ensure!(
            job.attempt.as_deref() == Some(attempt) && job.expires > now(),
            "expired or superseded worker lease"
        );
        Ok(job)
    }

    pub(crate) fn heartbeat(
        &self,
        id: &str,
        attempt: &str,
        failure: Option<String>,
        texts: &HashSet<&str>,
    ) -> Result<()> {
        self.locked(|| {
            let mut job = self.leased(id, attempt, texts)?;
            job.expires = now() + LEASE_SECONDS;
            if let Some(failure) = failure {
                job.failures += 1;
                job.attempt = None;
                job.expires = now() + 30 * u64::from(job.failures);
                if job.failures >= 3 {
                    job.error = Some(failure);
                }
            }
            self.save(&job)
        })
    }

    pub(crate) fn complete(
        &self,
        id: &str,
        attempt: &str,
        outputs: &BTreeMap<String, String>,
        texts: &HashSet<&str>,
    ) -> Result<()> {
        self.locked(|| {
            let mut job = self.read(id)?;
            ensure!(Self::current(&job, texts), "stale retrieval input");
            ensure!(
                job.attempt.as_deref() == Some(attempt),
                "superseded worker lease"
            );
            ensure!(outputs.len() == 1, "retrieval requires exactly one result");
            let output = outputs.get(OUTPUT).context("missing retrieval result")?;
            if job.complete {
                ensure!(
                    fs::read_to_string(self.result_path(id)?)? == *output,
                    "conflicting completion replay"
                );
                return Ok(());
            }
            ensure!(job.expires > now(), "expired worker lease");
            let body: Value = serde_json::from_str(output)?;
            ensure!(
                body["provenance"]["fingerprint"] == job.id
                    && body["provenance"]["recipe"] == job.recipe,
                "wrong retrieval provenance"
            );
            validate_result(&job.recipe, &job.parameters, output)?;
            crate::store::write_atomic(&self.result_path(id)?, output.as_bytes())?;
            job.complete = true;
            self.save(&job)
        })
    }
}
