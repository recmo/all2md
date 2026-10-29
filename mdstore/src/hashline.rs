use std::collections::{BTreeMap, HashMap};

use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;

#[derive(Debug, Error, Clone, Serialize)]
/// Failure to resolve or combine hashline edit anchors.
pub(crate) enum HashlineError {
    /// The anchor syntax is invalid.
    #[error("invalid anchor {0:?}")]
    InvalidAnchor(String),
    /// No current line matches the anchor.
    #[error("stale anchor {anchor}. Current:\n{context}")]
    StaleAnchor {
        /// Anchor supplied by the caller.
        anchor: String,
        /// Current nearby hashlines for recovery.
        context: String,
    },
    /// The inclusive range syntax or ordering is invalid.
    #[error("invalid range {0:?}")]
    InvalidRange(String),
    /// Two operations modify overlapping source ranges.
    #[error("overlapping edits")]
    Overlap { line: usize },
    /// An operation cannot be applied to the current source.
    #[error("{0}")]
    InvalidOperation(String),
}

#[derive(Debug, Error)]
#[error("{path}: {source}")]
pub(crate) struct HashlineEditError {
    pub path: String,
    pub line: Option<usize>,
    pub source: HashlineError,
}

impl HashlineError {
    fn at(self, path: &str) -> HashlineEditError {
        let line = match &self {
            Self::InvalidAnchor(anchor)
            | Self::InvalidRange(anchor)
            | Self::StaleAnchor { anchor, .. } => anchor
                .split_once(':')
                .and_then(|(line, _)| line.parse::<usize>().ok())
                .filter(|line| *line > 0),
            Self::Overlap { line } => Some(*line),
            Self::InvalidOperation(_) => None,
        };
        HashlineEditError {
            path: path.into(),
            line,
            source: self,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "op", rename_all = "snake_case")]
/// A hashline-anchored page edit.
pub enum EditOperation {
    /// Replaces anchored lines, or creates a missing file without an anchor.
    Replace {
        /// Repository-relative target path.
        path: String,
        /// One line or inclusive range; absent only for creation.
        anchor: Option<String>,
        /// Replacement text; empty deletes the selected lines.
        content: String,
    },
    /// Inserts into an existing file after an anchor, or at EOF without one.
    Append {
        /// Repository-relative target path.
        path: String,
        /// One line, `0:` for BOF, or absent for EOF.
        anchor: Option<String>,
        /// Non-empty text to insert.
        content: String,
    },
}

impl EditOperation {
    /// Returns the repository-relative target path.
    #[must_use]
    pub fn path(&self) -> &str {
        match self {
            Self::Replace { path, .. } | Self::Append { path, .. } => path,
        }
    }
}

#[must_use]
/// Computes a two-character SHA-256 prefix over a line and its immediate neighbors.
/// Missing neighbors are empty strings. Length prefixes preserve field boundaries.
pub fn short_hash(line: &str, prev: &str, next: &str) -> String {
    let mut hash = Sha256::new();
    for part in [prev, line, next] {
        hash.update((part.len() as u64).to_be_bytes());
        hash.update(part.as_bytes());
    }
    format!("{:x}", hash.finalize())[..2].to_owned()
}

fn line_hash<S: AsRef<str>>(lines: &[S], index: usize) -> String {
    short_hash(
        lines[index].as_ref(),
        index.checked_sub(1).map_or("", |i| lines[i].as_ref()),
        lines.get(index + 1).map_or("", AsRef::as_ref),
    )
}

#[must_use]
/// Renders a page or line window as `LINE:HASH|content` records.
pub(crate) fn render(text: &str, window: Option<(usize, usize)>) -> String {
    let lines: Vec<&str> = text.lines().collect();
    let (start, end) = window.unwrap_or((1, lines.len()));
    if lines.is_empty() {
        return String::new();
    }
    let start = start.max(1).min(lines.len());
    let end = end.max(start).min(lines.len());
    lines[start - 1..end]
        .iter()
        .enumerate()
        .map(|(offset, line)| {
            let index = start - 1 + offset;
            format!("{}:{}|{line}", index + 1, line_hash(&lines, index))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[derive(Debug, Clone, Copy)]
struct Span {
    start: usize,
    end: usize,
}

#[derive(Debug)]
struct Resolved {
    index: usize,
    span: Span,
    kind: ResolvedKind,
}

#[derive(Debug)]
enum ResolvedKind {
    Replace(String),
    Insert(String),
    Delete,
}

/// Applies all operations against one immutable pre-edit snapshot.
pub(crate) fn apply_operations(
    original: &HashMap<String, String>,
    operations: &[EditOperation],
) -> Result<HashMap<String, Option<String>>, HashlineEditError> {
    let mut grouped: BTreeMap<&str, Vec<(usize, &EditOperation)>> = BTreeMap::new();
    for (index, operation) in operations.iter().enumerate() {
        grouped
            .entry(operation.path())
            .or_default()
            .push((index, operation));
    }
    let mut output = HashMap::new();
    for (path, ops) in grouped {
        if let [
            (
                ..,
                EditOperation::Replace {
                    anchor: None,
                    content,
                    ..
                },
            ),
        ] = ops.as_slice()
        {
            if content.is_empty() {
                return Err(HashlineError::InvalidOperation(
                    "cannot create a file with empty content".into(),
                )
                .at(path));
            }
            if original.contains_key(path) {
                return Err(HashlineError::InvalidOperation(
                    "file exists; replacement requires an anchor".into(),
                )
                .at(path));
            }
            output.insert(path.into(), Some(content.clone()));
            continue;
        }
        let text = original.get(path).ok_or_else(|| {
            HashlineError::InvalidOperation("file does not exist".into()).at(path)
        })?;
        if let [
            (
                ..,
                EditOperation::Replace {
                    anchor: Some(anchor),
                    content,
                    ..
                },
            ),
        ] = ops.as_slice()
            && text.is_empty()
            && anchor == "0:"
        {
            if content.is_empty() {
                return Err(HashlineError::InvalidOperation(
                    "cannot replace an empty file with empty content".into(),
                )
                .at(path));
            }
            output.insert(path.into(), Some(content.clone()));
            continue;
        }
        let mut lines: Vec<String> = text.lines().map(str::to_owned).collect();
        let separator = if text.contains("\r\n") { "\r\n" } else { "\n" };
        let trailing_newline = text.ends_with('\n');
        let mut resolved = Vec::new();
        for (index, operation) in ops {
            let (span, kind) = match operation {
                EditOperation::Replace {
                    anchor: Some(anchor),
                    content,
                    ..
                } => {
                    let span = resolve_span(anchor, &lines).map_err(|error| error.at(path))?;
                    let kind = if content.is_empty() {
                        ResolvedKind::Delete
                    } else {
                        ResolvedKind::Replace(content.clone())
                    };
                    (span, kind)
                }
                EditOperation::Replace { anchor: None, .. } => {
                    return Err(HashlineError::InvalidOperation(
                        "creation must be the only operation for its path".into(),
                    )
                    .at(path));
                }
                EditOperation::Append {
                    anchor, content, ..
                } => {
                    if content.is_empty() {
                        return Err(HashlineError::InvalidOperation(
                            "append content must be non-empty".into(),
                        )
                        .at(path));
                    }
                    let line = match anchor.as_deref() {
                        None => lines.len(),
                        Some("0:") => 0,
                        Some(anchor) => {
                            resolve_line(anchor, &lines).map_err(|error| error.at(path))? + 1
                        }
                    };
                    (
                        Span {
                            start: line,
                            end: line,
                        },
                        ResolvedKind::Insert(content.clone()),
                    )
                }
            };
            resolved.push(Resolved { index, span, kind });
        }
        reject_overlaps(&resolved).map_err(|error| error.at(path))?;
        if let [edit] = resolved.as_slice()
            && edit.span.start == 0
            && edit.span.end + 1 == lines.len()
        {
            match &edit.kind {
                ResolvedKind::Replace(content) => {
                    output.insert(path.into(), Some(content.clone()));
                    continue;
                }
                ResolvedKind::Delete => {
                    output.insert(path.into(), None);
                    continue;
                }
                ResolvedKind::Insert(_) => {}
            }
        }
        resolved.sort_by(|a, b| {
            b.span
                .start
                .cmp(&a.span.start)
                .then_with(|| b.index.cmp(&a.index))
        });
        for edit in resolved {
            match edit.kind {
                ResolvedKind::Insert(content) => {
                    lines.splice(edit.span.start..edit.span.start, split_content(&content));
                }
                ResolvedKind::Replace(content) => {
                    lines.splice(edit.span.start..=edit.span.end, split_content(&content));
                }
                ResolvedKind::Delete => {
                    lines.splice(edit.span.start..=edit.span.end, Vec::<String>::new());
                }
            }
        }
        if lines.is_empty() {
            return Err(HashlineError::InvalidOperation(
                "deleting a file requires one complete-file anchor".into(),
            )
            .at(path));
        }
        let mut updated = lines.join(separator);
        if trailing_newline || lines.last().is_some_and(String::is_empty) {
            updated.push_str(separator);
        }
        output.insert(path.into(), Some(updated));
    }
    Ok(output)
}

fn split_content(content: &str) -> Vec<String> {
    let mut lines: Vec<String> = content
        .split('\n')
        .map(|line| line.strip_suffix('\r').unwrap_or(line).to_owned())
        .collect();
    if content.ends_with('\n') {
        lines.pop();
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

fn reject_overlaps(edits: &[Resolved]) -> Result<(), HashlineError> {
    for (index, left) in edits.iter().enumerate() {
        for right in &edits[index + 1..] {
            let both_inserts = matches!(left.kind, ResolvedKind::Insert(_))
                && matches!(right.kind, ResolvedKind::Insert(_));
            if both_inserts && left.span.start == right.span.start {
                continue;
            }
            if left.span.start <= right.span.end && right.span.start <= left.span.end {
                return Err(HashlineError::Overlap {
                    line: right.span.start + 1,
                });
            }
        }
    }
    Ok(())
}

fn resolve_span(anchor: &str, lines: &[String]) -> Result<Span, HashlineError> {
    if let Some((start, end)) = anchor.split_once("..") {
        if end.contains("..") {
            return Err(HashlineError::InvalidRange(anchor.into()));
        }
        let start = resolve_line(start, lines)?;
        let end = resolve_line(end, lines)?;
        if start > end {
            return Err(HashlineError::InvalidRange(anchor.into()));
        }
        Ok(Span { start, end })
    } else {
        let line = resolve_line(anchor, lines)?;
        Ok(Span {
            start: line,
            end: line,
        })
    }
}

fn resolve_line(anchor: &str, lines: &[String]) -> Result<usize, HashlineError> {
    let (number, expected) = anchor
        .split_once(':')
        .ok_or_else(|| HashlineError::InvalidAnchor(anchor.into()))?;
    let number = number
        .parse::<usize>()
        .ok()
        .filter(|value| *value > 0)
        .ok_or_else(|| HashlineError::InvalidAnchor(anchor.into()))?;
    if expected.len() != 2 || !expected.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(HashlineError::InvalidAnchor(anchor.into()));
    }
    if lines
        .get(number - 1)
        .is_some_and(|_| line_hash(lines, number - 1) == expected)
    {
        return Ok(number - 1);
    }
    let line = number.min(lines.len()).max(1);
    let start = line.saturating_sub(1).max(1);
    let end = line.saturating_add(1).min(lines.len());
    Err(HashlineError::StaleAnchor {
        anchor: anchor.into(),
        context: if lines.is_empty() {
            "File is empty.".into()
        } else {
            render(&lines.join("\n"), Some((start, end)))
        },
    })
}
