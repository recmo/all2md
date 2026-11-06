//! One stream for repository and job invalidation notifications.
use super::*;
use axum::response::sse::{Event, KeepAlive, Sse};
use std::{convert::Infallible, time::Duration};

pub(super) async fn subscribe(State(state): State<AppState>) -> Response {
    let stream = futures_util::stream::unfold(
        (state.store.subscribe(), true),
        |(mut receiver, first)| async move {
            if !first && receiver.changed().await.is_err() {
                return None;
            }
            let change = receiver.borrow_and_update().clone();
            if change.shutdown {
                return None;
            }
            let event = Event::default()
                .event("change")
                .json_data(change)
                .expect("change serializes");
            Some((Ok::<_, Infallible>(event), (receiver, false)))
        },
    );
    let mut response = Sse::new(stream)
        .keep_alive(KeepAlive::default().interval(Duration::from_secs(10)))
        .into_response();
    response
        .headers_mut()
        .insert("x-accel-buffering", "no".parse().unwrap());
    response
        .headers_mut()
        .insert("cache-control", "private, no-store".parse().unwrap());
    response
}
