//! Browser sessions exchange a bearer credential for an opaque HttpOnly cookie.
use super::*;
use std::collections::BTreeMap;

pub(super) const COOKIE: &str = "mdstore_session";
pub(super) const LIFETIME: u64 = 12 * 60 * 60;
pub(super) type Sessions = Arc<parking_lot::Mutex<BTreeMap<String, u64>>>;
pub(super) fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
pub(super) fn id(headers: &HeaderMap) -> Option<&str> {
    headers
        .get("cookie")?
        .to_str()
        .ok()?
        .split(';')
        .find_map(|part| {
            let (name, value) = part.trim().split_once('=')?;
            (name == COOKIE).then_some(value)
        })
}
pub(super) fn authorized(state: &AppState, headers: &HeaderMap) -> bool {
    let mut sessions = state.sessions.lock();
    sessions.retain(|_, expires| *expires > now());
    id(headers).is_some_and(|id| sessions.contains_key(id))
}
fn cookie(headers: &HeaderMap, value: &str, max_age: u64) -> String {
    // Plain HTTP is permitted only for direct loopback development.
    let local = headers
        .get("host")
        .and_then(|h| h.to_str().ok())
        .and_then(|h| h.parse::<axum::http::uri::Authority>().ok())
        .is_some_and(|host| {
            host.host() == "localhost"
                || host
                    .host()
                    .trim_matches(['[', ']'])
                    .parse::<std::net::IpAddr>()
                    .is_ok_and(|ip| ip.is_loopback())
        });
    format!(
        "{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{}",
        if local
            && !headers
                .get("x-forwarded-proto")
                .is_some_and(|v| v == "https")
            && !headers
                .get("origin")
                .is_some_and(|v| v.as_bytes().starts_with(b"https://"))
        {
            ""
        } else {
            "; Secure"
        }
    )
}
pub(super) async fn create(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let result = (|| -> Result<Response> {
        let token = crate::store::artifacts::secret()?;
        let mut sessions = state.sessions.lock();
        sessions.retain(|_, expires| *expires > now());
        if let Some(old) = id(&headers) {
            sessions.remove(old);
        }
        anyhow::ensure!(sessions.len() < 1000, "too many browser sessions");
        sessions.insert(token.clone(), now() + LIFETIME);
        Ok((
            [
                ("set-cookie", cookie(&headers, &token, LIFETIME)),
                ("cache-control", "no-store".into()),
            ],
            Json(json!({"expires_in": LIFETIME})),
        )
            .into_response())
    })();
    result.unwrap_or_else(problem::error)
}
pub(super) async fn delete(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Some(id) = id(&headers) {
        state.sessions.lock().remove(id);
    }
    (
        [
            ("set-cookie", cookie(&headers, "", 0)),
            ("cache-control", "no-store".into()),
        ],
        Json(json!({})),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn remote_sessions_are_secure_and_expired_sessions_are_rejected() {
        let mut headers = HeaderMap::new();
        headers.insert("host", "store.example".parse().unwrap());
        assert!(cookie(&headers, "opaque", LIFETIME).ends_with("; Secure"));
        headers.insert("host", "localhost".parse().unwrap());
        headers.insert("x-forwarded-proto", "https".parse().unwrap());
        assert!(cookie(&headers, "opaque", LIFETIME).ends_with("; Secure"));
        let (_dir, store) = crate::store::artifacts::tests::fixture();
        let state = AppState {
            store,
            bearer_token: Some("key".into()),
            worker_token: None,
            sessions: Arc::default(),
        };
        state.sessions.lock().insert("expired".into(), now() - 1);
        headers.insert("cookie", "mdstore_session=expired".parse().unwrap());
        assert!(!authorized(&state, &headers));
        assert!(state.sessions.lock().is_empty());
    }
}
