//! Server-owned media classification; storage is not a client choice.
pub(crate) fn media_type(path: &str) -> &'static str {
    match path
        .rsplit('.')
        .next()
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "md" => "text/markdown; charset=utf-8",
        "yaml" | "yml" => "application/yaml",
        "toml" => "application/toml",
        "json" => "application/json",
        "txt" | "log" | "csv" | "tsv" | "srt" | "vtt" => "text/plain; charset=utf-8",
        "pdf" => "application/pdf",
        "mp4" | "m4v" => "video/mp4",
        "webm" => "video/webm",
        "mov" => "video/quicktime",
        "m4a" => "audio/mp4",
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "flac" => "audio/flac",
        "ogg" | "opus" => "audio/ogg",
        "aac" => "audio/aac",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "avif" => "image/avif",
        _ => "application/octet-stream",
    }
}

pub(crate) fn git_text(path: &str) -> bool {
    let media = media_type(path);
    media.starts_with("text/")
        || matches!(
            media,
            "application/json" | "application/yaml" | "application/toml"
        )
}
