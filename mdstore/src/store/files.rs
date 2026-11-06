//! Conditional single-document writes are adapters over the batch transaction.
use super::*;

#[derive(Debug, Serialize)]
pub(crate) struct FilePageResponse {
    pub readonly: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub asset: Option<artifacts::Asset>,
    pub revision: String,
    pub hash: Option<String>,
    pub exists: bool,
    pub template: Option<serde_json::Value>,
    pub path: String,
    pub text: Option<String>,
}

impl StoreState {
    pub(crate) fn file_directory(&self, path: &str) -> Result<serde_json::Value> {
        let children = self
            .get_directory(path)?
            .into_iter()
            .map(|path| {
                let kind = if path.ends_with('/') {
                    "directory"
                } else {
                    "file"
                };
                serde_json::json!({"path": path, "kind": kind})
            })
            .collect::<Vec<_>>();
        Ok(serde_json::json!({"path": path, "revision": self.head, "children": children}))
    }

    pub(crate) fn file_page(&self, path: &str) -> Result<FilePageResponse> {
        validate_repo_path(path)?;
        let asset = self.artifacts.assets.get(path).cloned();
        let (text, template) = if asset.is_some() {
            (Some(String::new()), None)
        } else {
            let source = self.get_source(path)?;
            (source.text, source.template)
        };
        Ok(FilePageResponse {
            readonly: self.artifacts.owned(path)
                || (crate::template::is_template(path) && !self.config.server.allow_template_edits)
                || (is_config_resource_path(path) && !self.config.server.allow_config_edits),
            hash: asset.as_ref().map(|asset| asset.oid.clone()).or_else(|| {
                text.as_ref()
                    .map(|text| crate::validation::source_hash(text))
            }),
            exists: text.is_some(),
            asset,
            revision: self.head.clone(),
            template: template
                .map(|path| serde_json::json!({"path": path.trim_start_matches('/')})),
            path: path.into(),
            text: Some(text.unwrap_or_default()),
        })
    }
}

impl Store {
    pub(crate) fn file_page(&self, path: &str) -> Result<FilePageResponse> {
        self.read_state().file_page(path)
    }

    pub(crate) fn edit_file(
        &self,
        path: &str,
        expected: Option<&str>,
        content: Option<String>,
    ) -> Result<Option<FilePageResponse>> {
        if path.contains(['{', '}']) {
            bail!("file URLs must be literal; use apply_edits for allocated paths");
        }
        let _lock = self.lock_repository()?;
        git::recover_worktree(&self.root)?;
        self.refresh_external_commit()?;
        let page = self.file_page(path)?;
        if page.hash.as_deref() != expected {
            return Err(artifacts::PreconditionFailed.into());
        }
        let deleted = content.is_none();
        let edit = EditOperation::replace_file(
            path.into(),
            page.exists
                .then_some(page.text.as_deref().unwrap_or_default()),
            content.unwrap_or_default(),
        );
        self.apply_edits_locked(&ApplyEditsRequest {
            summary: format!("{} {path}", if deleted { "Delete" } else { "Write" }),
            edits: vec![edit],
            expected_commit: None,
            moves: vec![],
        })?;
        if deleted {
            Ok(None)
        } else {
            self.file_page(path).map(Some)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn competing_file_writes_check_the_precondition_under_the_lock() {
        let (_dir, store) = artifacts::tests::fixture();
        assert!(
            store
                .edit_file("{serial:03}.md", None, Some("# Allocated\n".into()))
                .is_err()
        );
        let hash = store.file_page("recording.md").unwrap().hash.unwrap();
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let handles: Vec<_> = (0..2)
            .map(|i| {
                let store = store.clone();
                let hash = hash.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    store.edit_file(
                        "recording.md",
                        Some(&hash),
                        Some(format!("# Version {i}\n")),
                    )
                })
            })
            .collect();
        let results: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
        assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
        assert!(
            results
                .iter()
                .find_map(|r| r.as_ref().err())
                .unwrap()
                .is::<artifacts::PreconditionFailed>()
        );
    }
}
