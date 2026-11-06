//! Streaming file transfers; object storage is private to mdstore.
use super::*;
use anyhow::ensure;
use axum::body::Body;
use futures_util::StreamExt;
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt};

pub(super) async fn receive_object(
    store: Arc<Store>,
    body: Body,
    limit: u64,
) -> Result<crate::store::artifacts::Asset> {
    let temporary = store.object_temporary()?;
    let mut writer = tokio::fs::File::from_std(temporary.reopen()?);
    let mut stream = body.into_data_stream();
    let mut size = 0_u64;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        size += chunk.len() as u64;
        ensure!(size <= limit, "asset exceeds upload limit");
        writer.write_all(&chunk).await?;
    }
    writer.sync_all().await?;
    drop(writer);
    run_blocking(move || store.store_object(temporary)).await
}

pub(super) async fn serve_asset(
    store: &Store,
    asset: crate::store::artifacts::Asset,
    headers: &HeaderMap,
) -> Result<Response> {
    let mut file = tokio::fs::File::open(store.object_path(&asset.oid)?).await?;
    ensure!(
        file.metadata().await?.len() == asset.size,
        "asset object size mismatch"
    );
    let mut start = 0;
    let mut end = asset.size;
    let mut status = StatusCode::OK;
    if let Some(range) = headers.get("range") {
        let parsed = range
            .to_str()
            .ok()
            .and_then(|r| r.strip_prefix("bytes="))
            .and_then(|r| r.split_once('-'))
            .and_then(|(a, b)| {
                if a.is_empty() {
                    let suffix: u64 = b.parse().ok()?;
                    Some((asset.size.saturating_sub(suffix), asset.size))
                } else {
                    Some((
                        a.parse().ok()?,
                        if b.is_empty() {
                            asset.size
                        } else {
                            b.parse::<u64>().ok()?.checked_add(1)?.min(asset.size)
                        },
                    ))
                }
            });
        let Some((a, b)) = parsed.filter(|(a, b)| a < b && *a < asset.size) else {
            return Ok((
                StatusCode::RANGE_NOT_SATISFIABLE,
                [("content-range", format!("bytes */{}", asset.size))],
            )
                .into_response());
        };
        start = a;
        end = b;
        status = StatusCode::PARTIAL_CONTENT;
    }
    file.seek(std::io::SeekFrom::Start(start)).await?;
    let body = Body::from_stream(tokio_util::io::ReaderStream::new(file.take(end - start)));
    let mut builder = Response::builder()
        .status(status)
        .header("accept-ranges", "bytes")
        .header("content-length", (end - start).to_string())
        .header("content-type", "application/octet-stream")
        .header("cache-control", "private, no-store")
        .header("etag", format!("\"{}\"", asset.oid));
    if status == StatusCode::PARTIAL_CONTENT {
        builder = builder.header(
            "content-range",
            format!("bytes {start}-{}/{}", end - 1, asset.size),
        );
    }
    Ok(builder.body(body)?)
}
