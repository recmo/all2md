//! Conditional single-document writes are adapters over the batch transaction.
use super::*;

impl Store {
    pub(crate) fn edit_file(
        &self,
        path: &str,
        expected: Option<&str>,
        content: Option<String>,
    ) -> Result<Option<PageResponse>> {
        if path.contains(['{', '}']) {
            bail!("file URLs must be literal; use apply_edits for allocated paths");
        }
        let _lock = self.lock_repository()?;
        git::recover_worktree(&self.root)?;
        self.refresh_external_commit()?;
        let page = self.get_page(path, None)?;
        if page.hash.as_deref() != expected {
            return Err(artifacts::PreconditionFailed.into());
        }
        let edit = match content {
            Some(content) if page.exists => EditOperation::ReplacePage {
                path: path.into(),
                base: page.text.unwrap_or_default(),
                content,
            },
            Some(content) => EditOperation::CreatePage {
                path: path.into(),
                content,
            },
            None => EditOperation::DeletePage {
                path: path.into(),
                base: page.text.unwrap_or_default(),
            },
        };
        let deleted = matches!(edit, EditOperation::DeletePage { .. });
        self.apply_edits_locked(&ApplyEditsRequest {
            edit_summary: format!("{} {path}", if deleted { "Delete" } else { "Write" }),
            edits: vec![edit],
        })?;
        if deleted {
            Ok(None)
        } else {
            self.get_page(path, None).map(Some)
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
        let hash = store.get_page("recording.md", None).unwrap().hash.unwrap();
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
