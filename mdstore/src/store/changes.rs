//! Coalesced notifications: clients fetch state again after connecting/reconnecting.
use super::*;

#[derive(Clone, Debug, Serialize)]
pub(crate) struct Change {
    pub revision: String,
    pub jobs: u64,
    #[serde(skip)]
    pub shutdown: bool,
}
impl Store {
    pub(crate) fn subscribe(&self) -> tokio::sync::watch::Receiver<Change> {
        self.changes.subscribe()
    }
    pub(crate) fn stop_changes(&self) {
        self.changes.send_modify(|change| change.shutdown = true);
    }
    pub(super) fn publish_revision(&self) {
        let revision = self.state.read().head.clone();
        self.changes.send_if_modified(|change| {
            if change.revision == revision {
                return false;
            }
            change.revision = revision;
            true
        });
    }
    pub(crate) async fn maintain_jobs(self: Arc<Self>) {
        let mut interval = tokio::time::interval(Duration::from_secs(5));
        loop {
            interval.tick().await;
            let store = self.clone();
            match tokio::task::spawn_blocking(move || store.jobs()).await {
                Ok(Ok(_)) => {}
                result => tracing::warn!(?result, "job reconciliation failed"),
            }
        }
    }
}
