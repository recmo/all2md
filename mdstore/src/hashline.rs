use std::collections::{BTreeMap, HashMap};

use serde::{Deserialize, Serialize};
use thiserror::Error;
use xxhash_rust::xxh32::xxh32;

#[derive(Debug, Error, Clone, Serialize)]
pub enum HashlineError {
    #[error("invalid anchor {0:?}")]
    InvalidAnchor(String),
    #[error("stale anchor {anchor}; current neighborhood:\n{context}")]
    StaleAnchor { anchor: String, context: String },
    #[error("ambiguous anchor {anchor}; matches lines {lines:?}")]
    AmbiguousAnchor { anchor: String, lines: Vec<usize> },
    #[error("invalid range {0:?}")]
    InvalidRange(String),
    #[error("overlapping edits in {0}")]
    Overlap(String),
    #[error("operation {0} is incompatible with other operations on the same path")]
    Incompatible(String),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum EditOperation {
    Replace {
        path: String,
        anchor: String,
        content: String,
    },
    InsertBefore {
        path: String,
        anchor: String,
        content: String,
    },
    InsertAfter {
        path: String,
        anchor: String,
        content: String,
    },
    Delete {
        path: String,
        anchor: String,
    },
    CreatePage {
        path: String,
        content: String,
    },
    RemovePage {
        path: String,
        anchor: String,
    },
}

impl EditOperation {
    pub fn path(&self) -> &str {
        match self {
            Self::Replace { path, .. }
            | Self::InsertBefore { path, .. }
            | Self::InsertAfter { path, .. }
            | Self::Delete { path, .. }
            | Self::CreatePage { path, .. }
            | Self::RemovePage { path, .. } => path,
        }
    }
}

#[must_use]
pub fn short_hash(line: &str) -> String {
    let value = xxh32(line.trim_end().as_bytes(), 0) as u8;
    format!("{value:02x}")
}

#[must_use]
pub fn render(text: &str, window: Option<(usize, usize)>) -> String {
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
        .map(|(offset, line)| format!("{}:{}|{}", start + offset, short_hash(line), line))
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

#[derive(Debug, Clone, Copy)]
pub struct ChangedRange {
    pub start_line: usize,
    pub end_line: usize,
}

pub struct AppliedOperations {
    pub changes: HashMap<String, Option<String>>,
    pub changed_ranges: HashMap<String, Vec<ChangedRange>>,
}

pub fn apply_operations(
    original: &HashMap<String, String>,
    operations: &[EditOperation],
) -> Result<HashMap<String, Option<String>>, HashlineError> {
    Ok(apply_operations_with_ranges(original, operations)?.changes)
}

pub fn apply_operations_with_ranges(
    original: &HashMap<String, String>,
    operations: &[EditOperation],
) -> Result<AppliedOperations, HashlineError> {
    let mut grouped: BTreeMap<&str, Vec<(usize, &EditOperation)>> = BTreeMap::new();
    for (index, operation) in operations.iter().enumerate() {
        grouped
            .entry(operation.path())
            .or_default()
            .push((index, operation));
    }
    let mut output = HashMap::new();
    let mut changed_ranges = HashMap::new();
    for (path, ops) in grouped {
        if let [(.., EditOperation::CreatePage { content, .. })] = ops.as_slice() {
            if original.contains_key(path) {
                return Err(HashlineError::Incompatible(path.into()));
            }
            output.insert(path.into(), Some(content.clone()));
            let line_count = content.lines().count();
            if line_count > 0 {
                changed_ranges.insert(
                    path.into(),
                    vec![ChangedRange {
                        start_line: 1,
                        end_line: line_count,
                    }],
                );
            }
            continue;
        }
        let Some(text) = original.get(path) else {
            return Err(HashlineError::Incompatible(path.into()));
        };
        if ops
            .iter()
            .any(|(_, op)| matches!(op, EditOperation::CreatePage { .. }))
        {
            return Err(HashlineError::Incompatible(path.into()));
        }
        if let [(.., EditOperation::RemovePage { anchor, .. })] = ops.as_slice() {
            let lines: Vec<String> = text.lines().map(str::to_owned).collect();
            if lines.is_empty() && anchor == "0:" {
                output.insert(path.into(), None);
                continue;
            }
            let span = resolve_span(anchor, &lines)?;
            if span.start != 0 || span.end != lines.len().saturating_sub(1) {
                return Err(HashlineError::InvalidRange(anchor.clone()));
            }
            output.insert(path.into(), None);
            continue;
        }
        if ops
            .iter()
            .any(|(_, op)| matches!(op, EditOperation::RemovePage { .. }))
        {
            return Err(HashlineError::Incompatible(path.into()));
        }

        let mut lines: Vec<String> = text.lines().map(str::to_owned).collect();
        let separator = if text.contains("\r\n") { "\r\n" } else { "\n" };
        let trailing_newline = text.ends_with('\n');
        let mut resolved = Vec::new();
        for (request_index, operation) in ops {
            let item = match operation {
                EditOperation::Replace {
                    anchor, content, ..
                } => Resolved {
                    index: request_index,
                    span: resolve_span(anchor, &lines)?,
                    kind: ResolvedKind::Replace(content.clone()),
                },
                EditOperation::Delete { anchor, .. } => Resolved {
                    index: request_index,
                    span: resolve_span(anchor, &lines)?,
                    kind: ResolvedKind::Delete,
                },
                EditOperation::InsertBefore {
                    anchor, content, ..
                } => {
                    let line = resolve_insert_position(anchor, &lines, false)?;
                    Resolved {
                        index: request_index,
                        span: Span {
                            start: line,
                            end: line,
                        },
                        kind: ResolvedKind::Insert(content.clone()),
                    }
                }
                EditOperation::InsertAfter {
                    anchor, content, ..
                } => {
                    let line = resolve_insert_position(anchor, &lines, true)?;
                    Resolved {
                        index: request_index,
                        span: Span {
                            start: line,
                            end: line,
                        },
                        kind: ResolvedKind::Insert(content.clone()),
                    }
                }
                EditOperation::CreatePage { .. } | EditOperation::RemovePage { .. } => {
                    unreachable!()
                }
            };
            resolved.push(item);
        }
        reject_overlaps(path, &resolved)?;
        let mut ordered_ranges: Vec<&Resolved> = resolved.iter().collect();
        ordered_ranges.sort_by(|a, b| {
            a.span
                .start
                .cmp(&b.span.start)
                .then_with(|| a.index.cmp(&b.index))
        });
        let mut offset = 0_isize;
        let mut ranges = Vec::new();
        for edit in ordered_ranges {
            let removed = match &edit.kind {
                ResolvedKind::Insert(_) => 0,
                ResolvedKind::Replace(_) | ResolvedKind::Delete => {
                    edit.span.end - edit.span.start + 1
                }
            };
            let inserted = match &edit.kind {
                ResolvedKind::Insert(content) | ResolvedKind::Replace(content) => {
                    split_content(content).len()
                }
                ResolvedKind::Delete => 0,
            };
            let start = (edit.span.start as isize + offset).max(0) as usize;
            ranges.push((start, inserted));
            offset += inserted as isize - removed as isize;
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
        let mut updated = lines.join(separator);
        if trailing_newline || lines.last().is_some_and(String::is_empty) {
            updated.push_str(separator);
        }
        let final_lines = lines.len();
        let ranges: Vec<ChangedRange> = ranges
            .into_iter()
            .filter_map(|(start, inserted)| {
                if final_lines == 0 {
                    return None;
                }
                let start = start.min(final_lines - 1);
                let end = if inserted == 0 {
                    start
                } else {
                    (start + inserted - 1).min(final_lines - 1)
                };
                Some(ChangedRange {
                    start_line: start + 1,
                    end_line: end + 1,
                })
            })
            .collect();
        if !ranges.is_empty() {
            changed_ranges.insert(path.into(), ranges);
        }
        output.insert(path.into(), Some(updated));
    }
    Ok(AppliedOperations {
        changes: output,
        changed_ranges,
    })
}

fn resolve_insert_position(
    anchor: &str,
    lines: &[String],
    insert_after: bool,
) -> Result<usize, HashlineError> {
    if anchor == "0:" {
        return Ok(0);
    }
    Ok(resolve_line(anchor, lines)? + usize::from(insert_after))
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
    if expected.len() != 2 || !expected.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(HashlineError::InvalidAnchor(anchor.into()));
    }
    if lines
        .get(number - 1)
        .is_some_and(|line| short_hash(line) == expected)
    {
        return Ok(number - 1);
    }
    let matches: Vec<usize> = lines
        .iter()
        .enumerate()
        .filter_map(|(index, line)| (short_hash(line) == expected).then_some(index))
        .collect();
    if matches.len() == 1 {
        return Ok(matches[0]);
    }
    let nearby: Vec<usize> = matches
        .iter()
        .copied()
        .filter(|index| index.abs_diff(number - 1) <= 3)
        .collect();
    if nearby.len() == 1 {
        return Ok(nearby[0]);
    }
    if !matches.is_empty() {
        return Err(HashlineError::AmbiguousAnchor {
            anchor: anchor.into(),
            lines: matches.into_iter().map(|index| index + 1).collect(),
        });
    }
    let start = number.saturating_sub(3).max(1);
    let end = (number + 2).min(lines.len());
    Err(HashlineError::StaleAnchor {
        anchor: anchor.into(),
        context: render(&lines.join("\n"), Some((start, end))),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_and_edit_with_shifted_anchor() {
        let text = "alpha\nbeta\ngamma\n";
        let anchor = format!("2:{}", short_hash("beta"));
        let mut original = HashMap::new();
        original.insert("note.md".into(), format!("prefix\n{text}"));
        let changed = apply_operations(
            &original,
            &[EditOperation::Replace {
                path: "note.md".into(),
                anchor,
                content: "BETA".into(),
            }],
        )
        .unwrap();
        assert_eq!(
            changed["note.md"].as_deref(),
            Some("prefix\nalpha\nBETA\ngamma\n")
        );
    }

    #[test]
    fn batch_uses_one_snapshot_and_rejects_overlap() {
        let mut original = HashMap::new();
        original.insert("note.md".into(), "one\ntwo\nthree\n".into());
        let one = format!("1:{}", short_hash("one"));
        let two = format!("2:{}", short_hash("two"));
        let error = apply_operations(
            &original,
            &[
                EditOperation::Replace {
                    path: "note.md".into(),
                    anchor: format!("{one}..{two}"),
                    content: "both".into(),
                },
                EditOperation::Delete {
                    path: "note.md".into(),
                    anchor: two,
                },
            ],
        )
        .unwrap_err();
        assert!(matches!(error, HashlineError::Overlap(_)));
    }

    #[test]
    fn zero_anchor_inserts_into_and_removes_empty_page() {
        let mut original = HashMap::new();
        original.insert("empty.md".into(), String::new());
        let inserted = apply_operations(
            &original,
            &[EditOperation::InsertAfter {
                path: "empty.md".into(),
                anchor: "0:".into(),
                content: "first".into(),
            }],
        )
        .unwrap();
        assert_eq!(inserted["empty.md"].as_deref(), Some("first"));
        let removed = apply_operations(
            &original,
            &[EditOperation::RemovePage {
                path: "empty.md".into(),
                anchor: "0:".into(),
            }],
        )
        .unwrap();
        assert_eq!(removed["empty.md"], None);
    }

    #[test]
    fn edits_preserve_crlf_and_empty_content_is_a_line() {
        let mut original = HashMap::new();
        original.insert("note.md".into(), "one\r\ntwo\r\nthree\r\n".into());
        let changed = apply_operations_with_ranges(
            &original,
            &[
                EditOperation::Replace {
                    path: "note.md".into(),
                    anchor: format!("2:{}", short_hash("two")),
                    content: String::new(),
                },
                EditOperation::InsertAfter {
                    path: "note.md".into(),
                    anchor: format!("3:{}", short_hash("three")),
                    content: "\n".into(),
                },
            ],
        )
        .unwrap();
        assert_eq!(
            changed.changes["note.md"].as_deref(),
            Some("one\r\n\r\nthree\r\n\r\n")
        );
        assert_eq!(changed.changed_ranges["note.md"].len(), 2);

        let mut original = HashMap::new();
        original.insert("one.md".into(), "one".into());
        let blank = apply_operations(
            &original,
            &[EditOperation::Replace {
                path: "one.md".into(),
                anchor: format!("1:{}", short_hash("one")),
                content: String::new(),
            }],
        )
        .unwrap();
        assert_eq!(blank["one.md"].as_deref(), Some("\n"));
    }
}
