//! One stream for repository and job invalidation notifications.
use super::*;
use axum::response::sse::{Event, KeepAlive, Sse};
use std::{convert::Infallible, time::Duration};

pub(super) async fn subscribe(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let receiver = state.store.subscribe();
    let stream = futures_util::stream::unfold(
        (receiver, true, state, headers),
        |(mut receiver, mut first, state, headers)| async move {
            loop {
                let changed = if first {
                    true
                } else {
                    tokio::select! {
                        result = receiver.changed() => { if result.is_err() { return None; } true },
                        _ = tokio::time::sleep(Duration::from_secs(15)) => false,
                    }
                };
                if !changed {
                    continue;
                }
                first = false;
                let change = receiver.borrow_and_update().clone();
                if change.shutdown {
                    return None;
                }
                let event = Event::default()
                    .event("change")
                    .json_data(change)
                    .expect("change serializes");
                return Some((
                    Ok::<_, Infallible>(event),
                    (receiver, first, state, headers),
                ));
            }
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
