use serde::{Deserialize, Serialize};

use crate::{config::ChunkConfig, markdown::ParsedPage};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Chunk {
    pub start_line: usize,
    pub end_line: usize,
    pub heading: Vec<String>,
    pub text: String,
    pub embedding_text: String,
}

#[derive(Debug)]
struct Block {
    start_line: usize,
    end_line: usize,
    heading: Vec<String>,
    text: String,
}

pub fn chunk_page(
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
        if block.text.chars().count() > config.max_chars {
            if let Some(value) = current.take() {
                push_chunk(&mut chunks, value, context, overlap_chars, config.max_chars);
            }
            for split in split_block(block, config.max_chars) {
                push_chunk(&mut chunks, split, context, overlap_chars, config.max_chars);
            }
            continue;
        }
        match &mut current {
            Some(value)
                if value.text.chars().count() + 2 + block.text.chars().count() <= target_chars
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
                    overlap_chars,
                    config.max_chars,
                );
                current = Some(block);
            }
            None => current = Some(block),
        }
    }
    if let Some(value) = current {
        push_chunk(&mut chunks, value, context, overlap_chars, config.max_chars);
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
    let mut heading_stack: Vec<String> = Vec::new();
    let mut excluded_level: Option<usize> = None;
    let mut blocks = Vec::new();
    let mut start = parsed.body_start_line.saturating_sub(1);
    let mut buffer = Vec::new();
    let mut in_fence = false;
    for (index, line) in lines.iter().enumerate().skip(start) {
        let line_number = index + 1;
        if let Some(heading) = headings.get(&line_number) {
            flush_block(&mut blocks, &mut buffer, start, index, &heading_stack);
            let level = usize::from(heading.level);
            heading_stack.truncate(level.saturating_sub(1));
            heading_stack.push(heading.text.clone());
            excluded_level = if config.exclude_sections.contains(&heading.text) {
                Some(level)
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
        if line.trim_start().starts_with("```") || line.trim_start().starts_with("~~~") {
            in_fence = !in_fence;
        }
        if excluded_level.is_some() {
            start = index + 1;
            continue;
        }
        if line.trim().is_empty() && !in_fence {
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

fn split_block(block: Block, max_chars: usize) -> Vec<Block> {
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
    overlap_chars: usize,
    max_chars: usize,
) {
    let overlap = chunks
        .last()
        .map(|previous| tail(&previous.text, overlap_chars))
        .unwrap_or_default();
    chunks.push(to_chunk(block, context, &overlap, max_chars));
}

fn to_chunk(block: Block, context: &[String], overlap: &str, max_chars: usize) -> Chunk {
    let mut prefix = context.to_vec();
    prefix.extend(block.heading.iter().cloned());
    let prefix = prefix.join(" > ");
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{config::LinkConfig, markdown::parse_page};

    #[test]
    fn respects_headings_and_code_fences() {
        let text = "---\ntitle: Demo\n---\n# One\n\nParagraph.\n\n```md\n# Not a heading\nfn x() {}\n```\n\nVisible\n=======\n\nShown.\n\nHidden\n------\n\nSecret.\n\n# Two\n\nOther.\n";
        let parsed = parse_page(text, &LinkConfig::default()).unwrap();
        let config = ChunkConfig {
            exclude_sections: vec!["Hidden".into()],
            ..ChunkConfig::default()
        };
        let chunks = chunk_page(text, &parsed, &config, &["Demo".into()]);
        assert!(chunks.iter().any(|chunk| chunk.heading == ["One"]));
        assert!(chunks.iter().any(|chunk| chunk.text.contains("fn x")));
        assert!(
            chunks
                .iter()
                .any(|chunk| chunk.text.contains("# Not a heading"))
        );
        assert!(!chunks.iter().any(|chunk| {
            chunk
                .heading
                .iter()
                .any(|heading| heading == "Not a heading")
        }));
        assert!(chunks.iter().any(|chunk| chunk.heading == ["Visible"]));
        assert!(!chunks.iter().any(|chunk| chunk.text.contains("Secret")));
        assert!(chunks.iter().any(|chunk| chunk.heading == ["Two"]));
    }

    #[test]
    fn split_excerpts_stay_within_their_source_ranges() {
        let text =
            "# Notes\n\nfirst sentence. second sentence. third sentence.\n\nnext paragraph.\n";
        let parsed = parse_page(text, &LinkConfig::default()).unwrap();
        let config = ChunkConfig {
            target_tokens: 5,
            overlap_percent: 50,
            max_chars: 24,
            ..ChunkConfig::default()
        };
        let chunks = chunk_page(
            text,
            &parsed,
            &config,
            &["A deliberately long title".into()],
        );
        let lines: Vec<&str> = text.lines().collect();
        for chunk in &chunks {
            let source = lines[chunk.start_line - 1..chunk.end_line].join("\n");
            assert!(source.contains(chunk.text.trim()));
        }
        assert!(chunks.iter().skip(1).any(|chunk| {
            chunk.embedding_text.len() > chunk.text.len()
                && !chunk.text.starts_with("first sentence")
        }));
        assert!(
            chunks
                .iter()
                .all(|chunk| chunk.embedding_text.chars().count() <= config.max_chars)
        );

        let text = "# 中文\n\n这是一个没有空格的长段落。\n";
        let parsed = parse_page(text, &LinkConfig::default()).unwrap();
        let cjk_config = ChunkConfig {
            max_chars: 5,
            ..config
        };
        let chunks = chunk_page(text, &parsed, &cjk_config, &["很长的标题".into()]);
        assert!(chunks.iter().all(|chunk| chunk.text.chars().count() <= 5));
        assert!(
            chunks
                .iter()
                .all(|chunk| chunk.embedding_text.chars().count() <= 5)
        );
    }
}
