use serde::{Deserialize, Serialize};

use crate::{config::ChunkConfig, markdown::ParsedPage};

#[derive(Debug, Clone, Serialize, Deserialize)]
/// A searchable excerpt and the context used to embed it.
pub(crate) struct Chunk {
    /// First source line, one-based and inclusive.
    pub start_line: usize,
    /// Last source line, one-based and inclusive.
    pub end_line: usize,
    /// Heading breadcrumb that contains the excerpt.
    pub heading: Vec<String>,
    /// Source excerpt returned to callers.
    pub text: String,
    /// Excerpt plus configured context sent to the embedding provider.
    pub embedding_text: String,
}

#[derive(Debug)]
struct Block {
    start_line: usize,
    end_line: usize,
    heading: Vec<String>,
    text: String,
}

/// Splits a parsed page into bounded, structure-aware excerpts.
#[must_use]
pub(crate) fn chunk_page(
    text: &str,
    parsed: &ParsedPage,
    config: &ChunkConfig,
    context: &[String],
) -> Vec<Chunk> {
    let blocks = structural_blocks(text, parsed, config);
    let mut chunks = Vec::new();
    let target_chars = config.target_tokens.saturating_mul(4).min(config.max_chars);
    let overlap_chars = target_chars.saturating_mul(config.overlap_percent) / 100;
    let mut current: Option<Block> = None;
    for block in blocks {
        let body_limit = embedding_plan(
            context,
            &block.heading,
            target_chars,
            overlap_chars,
            config.max_chars,
        )
        .body_limit;
        if block.text.chars().count() > body_limit {
            if let Some(value) = current.take() {
                push_chunk(
                    &mut chunks,
                    value,
                    context,
                    target_chars,
                    overlap_chars,
                    config.max_chars,
                );
            }
            for split in split_block(&block, body_limit) {
                push_chunk(
                    &mut chunks,
                    split,
                    context,
                    target_chars,
                    overlap_chars,
                    config.max_chars,
                );
            }
            continue;
        }
        match &mut current {
            Some(value)
                if value.text.chars().count() + 2 + block.text.chars().count() <= body_limit
                    && value.heading == block.heading =>
            {
                value.text.push_str("\n\n");
                value.text.push_str(&block.text);
                value.end_line = block.end_line;
            }
            Some(_) => {
                let previous = current.take().expect("present");
                push_chunk(
                    &mut chunks,
                    previous,
                    context,
                    target_chars,
                    overlap_chars,
                    config.max_chars,
                );
                current = Some(block);
            }
            None => current = Some(block),
        }
    }
    if let Some(value) = current {
        push_chunk(
            &mut chunks,
            value,
            context,
            target_chars,
            overlap_chars,
            config.max_chars,
        );
    }
    chunks
}

fn structural_blocks(text: &str, parsed: &ParsedPage, config: &ChunkConfig) -> Vec<Block> {
    let lines: Vec<&str> = text.lines().collect();
    let headings: std::collections::HashMap<usize, _> = parsed
        .headings
        .iter()
        .map(|heading| (heading.line, heading))
        .collect();
    let setext_markers: std::collections::HashSet<usize> = parsed
        .headings
        .iter()
        .filter_map(|heading| {
            lines
                .get(heading.line)
                .is_some_and(|line| is_setext_underline(line))
                .then_some(heading.line + 1)
        })
        .collect();
    let in_code_block = |line| {
        parsed
            .code_blocks
            .iter()
            .any(|range| (range.start_line..=range.end_line).contains(&line))
    };
    let structural_boundaries: std::collections::HashSet<usize> =
        parsed.structural_boundaries.iter().copied().collect();
    let mut heading_stack: Vec<String> = Vec::new();
    let mut excluded_level: Option<usize> = None;
    let mut blocks = Vec::new();
    let mut start = parsed.body_start_line.saturating_sub(1);
    let mut buffer = Vec::new();
    let mut in_code = false;
    for (index, line) in lines.iter().enumerate().skip(start) {
        let line_number = index + 1;
        if structural_boundaries.contains(&line_number) {
            flush_block(&mut blocks, &mut buffer, start, index, &heading_stack);
            start = index;
        }
        let code = in_code_block(line_number);
        if code != in_code {
            flush_block(&mut blocks, &mut buffer, start, index, &heading_stack);
            start = index;
            in_code = code;
        }
        if let Some(heading) = headings.get(&line_number) {
            flush_block(&mut blocks, &mut buffer, start, index, &heading_stack);
            let level = usize::from(heading.level);
            heading_stack.truncate(level.saturating_sub(1));
            heading_stack.push(heading.text.clone());
            excluded_level = if config.exclude_sections.contains(&heading.text) {
                Some(excluded_level.map_or(level, |excluded| excluded.min(level)))
            } else if excluded_level.is_some_and(|excluded| level <= excluded) {
                None
            } else {
                excluded_level
            };
            start = index + 1;
            continue;
        }
        if setext_markers.contains(&line_number) {
            start = index + 1;
            continue;
        }
        if excluded_level.is_some() {
            start = index + 1;
            continue;
        }
        if line.trim().is_empty() && !in_code {
            flush_block(&mut blocks, &mut buffer, start, index, &heading_stack);
            start = index + 1;
        } else {
            buffer.push((*line).to_owned());
        }
    }
    flush_block(&mut blocks, &mut buffer, start, lines.len(), &heading_stack);
    blocks
}

fn flush_block(
    blocks: &mut Vec<Block>,
    buffer: &mut Vec<String>,
    start: usize,
    end: usize,
    heading: &[String],
) {
    if buffer.is_empty() {
        return;
    }
    blocks.push(Block {
        start_line: start + 1,
        end_line: end.max(start + 1),
        heading: heading.to_vec(),
        text: std::mem::take(buffer).join("\n"),
    });
}

fn is_setext_underline(line: &str) -> bool {
    let trimmed = line.trim();
    !trimmed.is_empty()
        && (trimmed.bytes().all(|byte| byte == b'=') || trimmed.bytes().all(|byte| byte == b'-'))
}

fn split_block(block: &Block, max_chars: usize) -> Vec<Block> {
    let mut output = Vec::new();
    let mut start = 0;
    while start < block.text.len() {
        let hard_end = block.text[start..]
            .char_indices()
            .nth(max_chars)
            .map_or(block.text.len(), |(offset, _)| start + offset);
        let mut end = hard_end;
        if hard_end < block.text.len() {
            let window = &block.text[start..hard_end];
            for delimiter in ["\n\n", "\n", ". ", "; ", ", ", " "] {
                if let Some(position) = window.rfind(delimiter) {
                    let candidate = start + position + delimiter.len();
                    if candidate > start {
                        end = candidate;
                        break;
                    }
                }
            }
        }
        let start_line = block.start_line
            + block.text.as_bytes()[..start]
                .iter()
                .filter(|b| **b == b'\n')
                .count();
        let last_byte = end.saturating_sub(1);
        let end_line = block.start_line
            + block.text.as_bytes()[..last_byte]
                .iter()
                .filter(|b| **b == b'\n')
                .count();
        output.push(Block {
            start_line,
            end_line,
            heading: block.heading.clone(),
            text: block.text[start..end].into(),
        });
        start = end;
    }
    output
}

fn tail(text: &str, chars: usize) -> String {
    if chars == 0 || text.is_empty() {
        return String::new();
    }
    let start = text
        .char_indices()
        .rev()
        .nth(chars.saturating_sub(1))
        .map_or(0, |(offset, _)| offset);
    text[start..].trim_start().into()
}

fn push_chunk(
    chunks: &mut Vec<Chunk>,
    block: Block,
    context: &[String],
    target_chars: usize,
    overlap_chars: usize,
    max_chars: usize,
) {
    let plan = embedding_plan(
        context,
        &block.heading,
        target_chars,
        overlap_chars,
        max_chars,
    );
    let overlap = chunks
        .last()
        .map(|previous| tail(&previous.text, plan.overlap_chars))
        .unwrap_or_default();
    chunks.push(to_chunk(block, &plan.prefix, &overlap, max_chars));
}

struct EmbeddingPlan {
    prefix: String,
    overlap_chars: usize,
    body_limit: usize,
}

fn embedding_plan(
    context: &[String],
    heading: &[String],
    target_chars: usize,
    overlap_chars: usize,
    max_chars: usize,
) -> EmbeddingPlan {
    let mut prefix = context.to_vec();
    prefix.extend(heading.iter().cloned());
    let prefix = prefix.join(" > ");
    let minimum_body = target_chars.min(max_chars.saturating_mul(3) / 4).max(1);
    let mut auxiliary = max_chars.saturating_sub(minimum_body);
    let mut remaining = max_chars;
    let prefix_limit = auxiliary.saturating_sub(2);
    let prefix: String = prefix.chars().take(prefix_limit).collect();
    if !prefix.is_empty() {
        let cost = prefix.chars().count() + 2;
        auxiliary -= cost;
        remaining -= cost;
    }
    let overlap_chars = overlap_chars.min(auxiliary.saturating_sub(2));
    if overlap_chars > 0 {
        remaining -= overlap_chars + 2;
    }
    EmbeddingPlan {
        prefix,
        overlap_chars,
        body_limit: target_chars.min(remaining).max(1),
    }
}

fn to_chunk(block: Block, prefix: &str, overlap: &str, max_chars: usize) -> Chunk {
    let mut remaining = max_chars.saturating_sub(block.text.chars().count());
    let prefix = if prefix.is_empty() || remaining <= 2 {
        String::new()
    } else {
        let prefix: String = prefix.chars().take(remaining - 2).collect();
        remaining -= prefix.chars().count() + 2;
        prefix
    };
    let overlap = if overlap.is_empty() || remaining <= 2 {
        String::new()
    } else {
        tail(overlap, remaining - 2)
    };
    let mut embedding_parts = Vec::new();
    if !prefix.is_empty() {
        embedding_parts.push(prefix);
    }
    if !overlap.is_empty() {
        embedding_parts.push(overlap);
    }
    embedding_parts.push(block.text.clone());
    let embedding_text = embedding_parts.join("\n\n");
    Chunk {
        start_line: block.start_line,
        end_line: block.end_line,
        heading: block.heading,
        text: block.text,
        embedding_text,
    }
}
