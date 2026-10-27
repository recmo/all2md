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
    #[error("stale anchor {anchor}; current neighborhood:\n{context}")]
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
    #[error("overlapping edits in {0}")]
    Overlap(String),
    /// An operation cannot be applied to the current source.
    #[error("{0}")]
    InvalidOperation(String),
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
/// Computes a four-character SHA-256 prefix over a line and its immediate neighbors.
/// Missing neighbors are empty strings. Length prefixes preserve field boundaries.
pub fn short_hash(line: &str, prev: &str, next: &str) -> String {
    let mut hash = Sha256::new();
    for part in [prev, line, next] {
        hash.update((part.len() as u64).to_be_bytes());
        hash.update(part.as_bytes());
    }
    format!("{:x}", hash.finalize())[..4].to_owned()
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
) -> Result<HashMap<String, Option<String>>, HashlineError> {
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
                return Err(HashlineError::InvalidOperation(format!(
                    "{path}: cannot create a file with empty content"
                )));
            }
            if original.contains_key(path) {
                return Err(HashlineError::InvalidOperation(format!(
                    "{path}: file exists; replacement requires an anchor"
                )));
            }
            output.insert(path.into(), Some(content.clone()));
            continue;
        }
        let text = original.get(path).ok_or_else(|| {
            HashlineError::InvalidOperation(format!("{path}: file does not exist"))
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
                return Err(HashlineError::InvalidOperation(format!(
                    "{path}: cannot replace an empty file with empty content"
                )));
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
                    let span = resolve_span(anchor, &lines)?;
                    let kind = if content.is_empty() {
                        ResolvedKind::Delete
                    } else {
                        ResolvedKind::Replace(content.clone())
                    };
                    (span, kind)
                }
                EditOperation::Replace { anchor: None, .. } => {
                    return Err(HashlineError::InvalidOperation(format!(
                        "{path}: creation must be the only operation for its path"
                    )));
                }
                EditOperation::Append {
                    anchor, content, ..
                } => {
                    if content.is_empty() {
                        return Err(HashlineError::InvalidOperation(format!(
                            "{path}: append content must be non-empty"
                        )));
                    }
                    let line = match anchor.as_deref() {
                        None => lines.len(),
                        Some("0:") => 0,
                        Some(anchor) => resolve_line(anchor, &lines)? + 1,
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
        reject_overlaps(path, &resolved)?;
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
            return Err(HashlineError::InvalidOperation(format!(
                "{path}: deleting a file requires one complete-file anchor"
            )));
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

fn reject_overlaps(path: &str, edits: &[Resolved]) -> Result<(), HashlineError> {
    for (index, left) in edits.iter().enumerate() {
        for right in &edits[index + 1..] {
            let both_inserts = matches!(left.kind, ResolvedKind::Insert(_))
                && matches!(right.kind, ResolvedKind::Insert(_));
            if both_inserts && left.span.start == right.span.start {
                continue;
            }
            if left.span.start <= right.span.end && right.span.start <= left.span.end {
                return Err(HashlineError::Overlap(path.into()));
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
    if expected.len() != 4 || !expected.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(HashlineError::InvalidAnchor(anchor.into()));
    }
    if lines
        .get(number - 1)
        .is_some_and(|_| line_hash(lines, number - 1) == expected)
    {
        return Ok(number - 1);
    }
    let start = number.saturating_sub(3).max(1);
    let end = number.saturating_add(2).min(lines.len());
    Err(HashlineError::StaleAnchor {
        anchor: anchor.into(),
        context: render(&lines.join("\n"), Some((start, end))),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn complete_anchor(text: &str) -> String {
        let lines: Vec<_> = text.lines().collect();
        if lines.is_empty() {
            return "0:".into();
        }
        let anchor = |i: usize| {
            format!(
                "{}:{}",
                i + 1,
                short_hash(
                    lines[i],
                    i.checked_sub(1).map_or("", |j| lines[j]),
                    lines.get(i + 1).copied().unwrap_or("")
                )
            )
        };
        if lines.len() == 1 {
            anchor(0)
        } else {
            format!("{}..{}", anchor(0), anchor(lines.len() - 1))
        }
    }

    #[test]
    fn complete_anchor_deletion_stages_atomic_move_and_rejects_stale_source() {
        let original = HashMap::from([("old.md".into(), "original\n".into())]);
        let operations = vec![
            EditOperation::Replace {
                path: "old.md".into(),
                anchor: Some(complete_anchor(&("original\n"))),
                content: String::new(),
            },
            EditOperation::Replace {
                path: "new.md".into(),
                content: "modified\n".into(),
                anchor: None,
            },
        ];
        let changes = apply_operations(&original, &operations).unwrap();
        assert_eq!(changes["old.md"], None);
        assert_eq!(changes["new.md"].as_deref(), Some("modified\n"));
        let stale = HashMap::from([("old.md".into(), "server edit\n".into())]);
        assert!(apply_operations(&stale, &operations).is_err());
    }

    #[test]
    fn shifted_anchor_is_rejected_even_with_a_unique_match() {
        let text = "alpha\nbeta\ngamma\n";
        let anchor = render(text, Some((2, 2)))
            .split('|')
            .next()
            .unwrap()
            .to_owned();
        let original = HashMap::from([("note.md".into(), format!("prefix\n{text}"))]);
        let error = apply_operations(
            &original,
            &[EditOperation::Replace {
                path: "note.md".into(),
                anchor: Some(anchor),
                content: "BETA".into(),
            }],
        )
        .unwrap_err();
        assert!(matches!(error, HashlineError::StaleAnchor { .. }));
    }

    #[test]
    fn batch_uses_one_snapshot_and_rejects_overlap() {
        let mut original = HashMap::new();
        original.insert("note.md".into(), "one\ntwo\nthree\n".into());
        let one = format!("1:{}", short_hash("one", "", "two"));
        let two = format!("2:{}", short_hash("two", "one", "three"));
        let error = apply_operations(
            &original,
            &[
                EditOperation::Replace {
                    path: "note.md".into(),
                    anchor: Some(format!("{one}..{two}")),
                    content: "both".into(),
                },
                EditOperation::Replace {
                    path: "note.md".into(),
                    anchor: Some(two),
                    content: String::new(),
                },
            ],
        )
        .unwrap_err();
        assert!(matches!(error, HashlineError::Overlap(_)));
    }

    #[test]
    fn zero_anchor_inserts_into_empty_page_but_empty_deletion_fails() {
        let mut original = HashMap::new();
        original.insert("empty.md".into(), String::new());
        let inserted = apply_operations(
            &original,
            &[EditOperation::Append {
                path: "empty.md".into(),
                anchor: Some("0:".into()),
                content: "first".into(),
            }],
        )
        .unwrap();
        assert_eq!(inserted["empty.md"].as_deref(), Some("first"));
        let removed = apply_operations(
            &original,
            &[EditOperation::Replace {
                path: "empty.md".into(),
                anchor: Some("0:".into()),
                content: String::new(),
            }],
        )
        .unwrap_err();
        assert!(
            removed
                .to_string()
                .contains("cannot replace an empty file with empty content")
        );
    }

    #[test]
    fn edits_preserve_crlf_and_empty_content_deletes_lines() {
        let mut original = HashMap::new();
        original.insert("note.md".into(), "one\r\ntwo\r\nthree\r\n".into());
        let changed = apply_operations(
            &original,
            &[
                EditOperation::Replace {
                    path: "note.md".into(),
                    anchor: Some(format!("2:{}", short_hash("two", "one", "three"))),
                    content: String::new(),
                },
                EditOperation::Append {
                    path: "note.md".into(),
                    anchor: Some(format!("3:{}", short_hash("three", "two", ""))),
                    content: "\n".into(),
                },
            ],
        )
        .unwrap();
        assert_eq!(changed["note.md"].as_deref(), Some("one\r\nthree\r\n\r\n"));

        let mut original = HashMap::new();
        original.insert("one.md".into(), "one".into());
        let blank = apply_operations(
            &original,
            &[EditOperation::Replace {
                path: "one.md".into(),
                anchor: Some(format!("1:{}", short_hash("one", "", ""))),
                content: String::new(),
            }],
        )
        .unwrap();
        assert_eq!(blank["one.md"], None);
    }

    #[test]
    fn hashes_include_neighbors_and_preserve_field_boundaries() {
        assert_ne!(short_hash("same", "a", "b"), short_hash("same", "c", "b"));
        assert_ne!(short_hash("same", "a", "b"), short_hash("same", "a", "c"));
        assert_ne!(short_hash("bc", "a", ""), short_hash("c", "ab", ""));
        let text = "first\nsame\nlast\n";
        let full = render(text, None);
        let window = render(text, Some((2, 2)));
        assert_eq!(window, full.lines().nth(1).unwrap());
        assert_eq!(
            window
                .split('|')
                .next()
                .unwrap()
                .split(':')
                .nth(1)
                .unwrap()
                .len(),
            4
        );
        assert!(full.starts_with(&format!("1:{}|first", short_hash("first", "", "same"))));
        assert!(full.ends_with(&format!("3:{}|last", short_hash("last", "same", ""))));
    }

    #[test]
    fn changed_neighbors_invalidate_anchor() {
        let lines = vec!["new neighbor".to_owned(), "same".into(), "last".into()];
        let anchor = format!("2:{}", short_hash("same", "old neighbor", "last"));
        assert!(matches!(
            resolve_line(&anchor, &lines),
            Err(HashlineError::StaleAnchor { .. })
        ));
    }
}
