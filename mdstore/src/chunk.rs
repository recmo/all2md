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
        if block.text.len() > config.max_chars {
            if let Some(value) = current.take() {
                chunks.push(to_chunk(value, context));
            }
            for split in recursive_split(&block.text, config.max_chars) {
                chunks.push(to_chunk(
                    Block {
                        text: split,
                        start_line: block.start_line,
                        end_line: block.end_line,
                        heading: block.heading.clone(),
                    },
                    context,
                ));
            }
            continue;
        }
        match &mut current {
            Some(value)
                if value.text.len() + 2 + block.text.len() <= target_chars
                    && value.heading == block.heading =>
            {
                value.text.push_str("\n\n");
                value.text.push_str(&block.text);
                value.end_line = block.end_line;
            }
            Some(_) => {
                let previous = current.take().expect("present");
                let overlap = tail(&previous.text, overlap_chars);
                chunks.push(to_chunk(previous, context));
                let mut text = String::new();
                if !overlap.is_empty() {
                    text.push_str(&overlap);
                    text.push_str("\n\n");
                }
                text.push_str(&block.text);
                current = Some(Block { text, ..block });
            }
            None => current = Some(block),
        }
    }
    if let Some(value) = current {
        chunks.push(to_chunk(value, context));
    }
    chunks
}

fn structural_blocks(text: &str, parsed: &ParsedPage, config: &ChunkConfig) -> Vec<Block> {
    let lines: Vec<&str> = text.lines().collect();
    let mut heading_stack: Vec<String> = Vec::new();
    let mut excluded_level: Option<usize> = None;
    let mut blocks = Vec::new();
    let mut start = parsed.body_start_line.saturating_sub(1);
    let mut buffer = Vec::new();
    let mut in_fence = false;
    for (index, line) in lines.iter().enumerate().skip(start) {
        if let Some((level, title)) = parse_heading(line) {
            flush_block(&mut blocks, &mut buffer, start, index, &heading_stack);
            heading_stack.truncate(level.saturating_sub(1));
            heading_stack.push(title.clone());
            excluded_level = if config.exclude_sections.contains(&title) {
                Some(level)
            } else if excluded_level.is_some_and(|excluded| level <= excluded) {
                None
            } else {
                excluded_level
            };
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

fn parse_heading(line: &str) -> Option<(usize, String)> {
    let trimmed = line.trim_start();
    let count = trimmed.bytes().take_while(|byte| *byte == b'#').count();
    if !(1..=6).contains(&count) || trimmed.as_bytes().get(count) != Some(&b' ') {
        return None;
    }
    Some((count, trimmed[count + 1..].trim().into()))
}

fn recursive_split(text: &str, max_chars: usize) -> Vec<String> {
    if text.len() <= max_chars {
        return vec![text.into()];
    }
    for delimiter in ["\n\n", "\n", ". ", "; ", ", ", " "] {
        let pieces: Vec<&str> = text.split(delimiter).collect();
        if pieces.len() <= 1 {
            continue;
        }
        let mut output = Vec::new();
        let mut current = String::new();
        for piece in pieces {
            let needed = usize::from(!current.is_empty()) * delimiter.len() + piece.len();
            if !current.is_empty() && current.len() + needed > max_chars {
                output.push(current);
                current = String::new();
            }
            if !current.is_empty() {
                current.push_str(delimiter);
            }
            current.push_str(piece);
        }
        if !current.is_empty() {
            output.push(current);
        }
        return output
            .into_iter()
            .flat_map(|piece| recursive_split(&piece, max_chars))
            .collect();
    }
    split_at_char_boundaries(text, max_chars)
}

fn split_at_char_boundaries(text: &str, max_chars: usize) -> Vec<String> {
    let mut output = Vec::new();
    let mut start = 0;
    while start < text.len() {
        let mut end = (start + max_chars).min(text.len());
        while end > start && !text.is_char_boundary(end) {
            end -= 1;
        }
        output.push(text[start..end].into());
        start = end;
    }
    output
}

fn tail(text: &str, bytes: usize) -> String {
    if bytes == 0 || text.is_empty() {
        return String::new();
    }
    let mut start = text.len().saturating_sub(bytes);
    while start < text.len() && !text.is_char_boundary(start) {
        start += 1;
    }
    text[start..].trim_start().into()
}

fn to_chunk(block: Block, context: &[String]) -> Chunk {
    let mut prefix = context.to_vec();
    prefix.extend(block.heading.iter().cloned());
    let embedding_text = if prefix.is_empty() {
        block.text.clone()
    } else {
        format!("{}\n\n{}", prefix.join(" > "), block.text)
    };
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
        let text = "---\ntitle: Demo\n---\n# One\n\nParagraph.\n\n```rs\nfn x() {}\n```\n\n# Two\n\nOther.\n";
        let parsed = parse_page(text, &LinkConfig::default()).unwrap();
        let chunks = chunk_page(text, &parsed, &ChunkConfig::default(), &["Demo".into()]);
        assert!(chunks.iter().any(|chunk| chunk.heading == ["One"]));
        assert!(chunks.iter().any(|chunk| chunk.text.contains("fn x")));
        assert!(chunks.iter().any(|chunk| chunk.heading == ["Two"]));
    }
}
