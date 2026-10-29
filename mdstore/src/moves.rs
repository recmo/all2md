use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    ops::Range,
    path::Path,
};

use anyhow::{Context, Result, anyhow, bail};
use pulldown_cmark::{Event, LinkType, Options, Parser, Tag};
use serde::{Deserialize, Serialize};

use crate::{
    config::validate_repo_path,
    markdown::{self, LinkSyntax, TargetResolver},
    template::Templates,
};

/// A simultaneous document relocation, addressed in the original snapshot.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Move {
    /// Original repository-relative path.
    pub from: String,
    /// Final repository-relative path.
    pub to: String,
}

struct Destination {
    range: Range<usize>,
    target: String,
    syntax: LinkSyntax,
}

/// Plans all moves and rewrites without touching the checkout.
pub(crate) fn apply(
    before: &HashMap<String, String>,
    edited: &HashMap<String, String>,
    moves: &[Move],
    old_templates: &Templates,
    new_templates: &Templates,
) -> Result<HashMap<String, String>> {
    let mut mapping = BTreeMap::new();
    let mut destinations = BTreeSet::new();
    for movement in moves {
        for path in [&movement.from, &movement.to] {
            validate_repo_path(path)?;
            if markdown::normalize_path(Path::new(path)).map_err(anyhow::Error::msg)? != *path {
                bail!("move paths must use canonical repository spelling: {path}");
            }
            if !path.ends_with(".md") || crate::template::is_template(path) {
                bail!("moves support document paths only: {path}");
            }
        }
        if movement.from == movement.to {
            bail!(
                "move source and destination are identical: {}",
                movement.from
            );
        }
        if !before.contains_key(&movement.from) {
            bail!("move source does not exist: {}", movement.from);
        }
        if !edited.contains_key(&movement.from) {
            bail!(
                "cannot move and delete the same document: {}",
                movement.from
            );
        }
        if mapping
            .insert(movement.from.clone(), movement.to.clone())
            .is_some()
        {
            bail!("duplicate move source: {}", movement.from);
        }
        if !destinations.insert(movement.to.clone()) {
            bail!("duplicate move destination: {}", movement.to);
        }
    }
    for destination in &destinations {
        if edited.contains_key(destination) && !mapping.contains_key(destination) {
            bail!(
                "move destination is occupied: {destination}; delete or move its document in this batch"
            );
        }
    }

    // Include deleted identities so replacing their paths cannot silently retarget links.
    let resolver = TargetResolver::new(before.keys().chain(edited.keys()));
    let final_inventory = edited
        .keys()
        .map(|path| mapping.get(path).unwrap_or(path).clone())
        .collect::<BTreeSet<_>>();
    let final_resolver = TargetResolver::new(final_inventory.iter());
    let mut output = HashMap::new();
    for (source, text) in edited {
        let destination = mapping.get(source).unwrap_or(source);
        let policy = old_templates.policy(source);
        let links = collect(text, &policy.links)
            .with_context(|| format!("{source}: locate move references"))?;
        let mut expected = Vec::new();
        let mut patches = BTreeMap::new();
        for link in links {
            let Some((target, document)) = resolve(&resolver, source, &link)? else {
                continue;
            };
            if document && !edited.contains_key(&target) {
                bail!(
                    "{source}: link to deleted document {target}; remove it or explicitly retarget it before moving"
                );
            }
            let final_target = mapping.get(&target).unwrap_or(&target);
            expected.push((link.range.start, link.range.end, final_target.clone()));
            if target == *final_target && source == destination {
                continue;
            }
            let raw = &text[link.range.clone()];
            let suffix = link
                .target
                .find(['#', '?'])
                .map_or("", |index| &link.target[index..]);
            // Preserve authored suffix bytes; reject encodings we cannot patch exactly.
            let raw_suffix = if suffix.is_empty() {
                ""
            } else {
                let index =
                    suffix_start(raw).context("cannot locate encoded link suffix safely")?;
                if raw.as_bytes()[index] != suffix.as_bytes()[0] {
                    bail!("{source}: cannot preserve encoded link suffix safely");
                }
                &raw[index..]
            };
            let path = render_path(
                destination,
                final_target,
                link.target.starts_with('/'),
                document
                    && !link
                        .target
                        .split(['#', '?'])
                        .next()
                        .unwrap_or_default()
                        .ends_with(".md"),
                link.syntax,
            );
            let replacement = format!("{path}{raw_suffix}");
            if let Some(previous) =
                patches.insert((link.range.start, link.range.end), replacement.clone())
                && previous != replacement
            {
                bail!("{source}: conflicting link rewrites");
            }
        }
        for (start, end, _) in &mut expected {
            let shift: isize = patches
                .iter()
                .filter(|((_, patch_end), _)| *patch_end <= *start)
                .map(|((patch_start, patch_end), replacement)| {
                    replacement.len() as isize - (patch_end - patch_start) as isize
                })
                .sum();
            let length = patches
                .get(&(*start, *end))
                .map_or(*end - *start, String::len);
            *start = start
                .checked_add_signed(shift)
                .context("invalid rewritten link offset")?;
            *end = *start + length;
        }
        let mut rewritten = text.clone();
        let mut previous = text.len();
        for ((start, end), replacement) in patches.into_iter().rev() {
            if end > previous {
                bail!("{source}: overlapping link rewrites");
            }
            rewritten.replace_range(start..end, &replacement);
            previous = start;
        }
        // Check the new schema's grammar as well as the destination paths. A wiki
        // pattern may not be able to express a new filename or may change by directory.
        let mut actual = Vec::new();
        for link in collect(&rewritten, &new_templates.policy(destination).links)? {
            if let Some((target, _)) = resolve(&final_resolver, destination, &link)? {
                actual.push((link.range.start, link.range.end, target));
            }
        }
        expected.sort();
        actual.sort();
        if expected != actual {
            bail!("{source}: move would change link identities under the destination schema");
        }
        output.insert(destination.clone(), rewritten);
    }
    Ok(output)
}

fn resolve(
    resolver: &TargetResolver,
    source: &str,
    link: &Destination,
) -> Result<Option<(String, bool)>> {
    let path = link.target.split(['#', '?']).next().unwrap_or_default();
    if path.is_empty() || path.starts_with("//") || markdown::has_uri_scheme(path) {
        return Ok(None);
    }
    if let Some(target) = resolver
        .resolve(source, &link.target, link.syntax)
        .map_err(|error| anyhow!("{source}: {error}"))?
    {
        return Ok(Some((target, true)));
    }
    // The corpus resolver deliberately ignores assets. Their relative paths still
    // need rebasing when their source document moves.
    let decoded = markdown::decode_markdown_path(path).map_err(anyhow::Error::msg)?;
    let target = if let Some(rooted) = decoded.strip_prefix('/') {
        markdown::normalize_path(Path::new(rooted))
    } else {
        markdown::normalize_path(
            &Path::new(source)
                .parent()
                .unwrap_or(Path::new(""))
                .join(decoded),
        )
    }
    .map_err(anyhow::Error::msg)?;
    Ok(Some((target, false)))
}

fn render_path(
    source: &str,
    target: &str,
    rooted: bool,
    omit_extension: bool,
    syntax: LinkSyntax,
) -> String {
    let target = if omit_extension {
        target
            .strip_suffix(".md")
            .filter(|path| !path.is_empty() && !path.ends_with('/'))
            .unwrap_or(target)
    } else {
        target
    };
    let path = if rooted {
        format!("/{target}")
    } else {
        let mut parent: Vec<_> = source.split('/').collect();
        parent.pop();
        let parts: Vec<_> = target.split('/').collect();
        let shared = parent
            .iter()
            .zip(&parts)
            .take_while(|(a, b)| a == b)
            .count();
        format!(
            "{}{}",
            "../".repeat(parent.len() - shared),
            parts[shared..].join("/")
        )
    };
    let mut path = match syntax {
        LinkSyntax::Wiki => path,
        LinkSyntax::Markdown => path
            .bytes()
            .map(|byte| {
                if byte.is_ascii_alphanumeric() || b"/-._~".contains(&byte) {
                    char::from(byte).to_string()
                } else {
                    format!("%{byte:02X}")
                }
            })
            .collect(),
    };
    if markdown::has_uri_scheme(&path) {
        path.insert_str(0, "./");
    }
    path
}

fn collect(text: &str, policy: &crate::LinkConfig) -> Result<Vec<Destination>> {
    let page = markdown::parse_page(text, policy)?;
    let mut result: Vec<_> = page
        .links
        .iter()
        .filter_map(|link| {
            link.target_range.clone().map(|range| Destination {
                range,
                target: link.target.clone(),
                syntax: LinkSyntax::Wiki,
            })
        })
        .collect();
    if !policy.markdown {
        return Ok(result);
    }
    let (_, _, body) = markdown::parse_frontmatter(text)?;
    let offset = text.len() - body.len();
    let parser = Parser::new_ext(body, Options::all());
    let mut spans = BTreeMap::new();
    for (_, definition) in parser.reference_definitions().iter() {
        if !is_local(&definition.dest) {
            continue;
        }
        let span = find_span(body, definition.span.clone(), "]:", &definition.dest)?;
        spans.insert(
            (offset + span.start, offset + span.end),
            definition.dest.to_string(),
        );
    }
    for (event, range) in parser.into_offset_iter() {
        let Event::Start(
            Tag::Link {
                link_type,
                dest_url,
                ..
            }
            | Tag::Image {
                link_type,
                dest_url,
                ..
            },
        ) = event
        else {
            continue;
        };
        if !is_local(&dest_url) {
            continue;
        }
        if result.iter().any(|link| {
            link.range.start >= offset + range.start && link.range.end <= offset + range.end
        }) {
            continue;
        }
        let span = match link_type {
            LinkType::Inline => find_span(body, range, "](", &dest_url)?,
            LinkType::WikiLink { .. } if policy.wiki.is_empty() => {
                // With no custom wiki grammar, validation also recognizes the
                // Markdown parser's native [[target|label]] syntax.
                let prefix = if body[range.clone()].starts_with("![[") {
                    3
                } else {
                    2
                };
                let start = range.start + prefix;
                let end = start + dest_url.len();
                if end > range.end || body.get(start..end) != Some(dest_url.as_ref()) {
                    bail!("cannot locate native wiki destination safely");
                }
                start..end
            }
            LinkType::WikiLink { .. } => {
                bail!("wiki link is not recognized by the configured grammar")
            }
            _ => continue,
        };
        spans.insert(
            (offset + span.start, offset + span.end),
            dest_url.into_string(),
        );
    }
    result.extend(spans.into_iter().map(|((start, end), target)| Destination {
        range: start..end,
        target,
        syntax: LinkSyntax::Markdown,
    }));
    Ok(result)
}

fn is_local(target: &str) -> bool {
    let path = target.split(['#', '?']).next().unwrap_or_default();
    !path.is_empty() && !path.starts_with("//") && !markdown::has_uri_scheme(path)
}

fn suffix_start(raw: &str) -> Option<usize> {
    let mut cursor = 0;
    let bytes = raw.as_bytes();
    while cursor < bytes.len() {
        // Numeric character references contain a '#' that belongs to the path.
        if bytes[cursor] == b'&'
            && let Some(end) = raw[cursor..].find(';')
            && raw[cursor + 1..cursor + end]
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'#')
        {
            cursor += end + 1;
            continue;
        }
        if matches!(bytes[cursor], b'#' | b'?') {
            return Some(cursor);
        }
        cursor += 1;
    }
    None
}

fn find_span(body: &str, range: Range<usize>, marker: &str, target: &str) -> Result<Range<usize>> {
    let candidates: Vec<_> = body[range.clone()]
        .match_indices(marker)
        .filter_map(|(index, _)| {
            let start = range.start + index + marker.len();
            destination_span(body, start, range.end).and_then(|span| {
                let raw = &body[span.clone()];
                let probe = format!("[](<{raw}>)");
                let parsed = Parser::new(&probe).find_map(|event| match event {
                    Event::Start(Tag::Link { dest_url, .. }) => Some(dest_url.into_string()),
                    _ => None,
                });
                (parsed.as_deref() == Some(target)).then_some(span)
            })
        })
        .collect();
    let [span] = candidates.as_slice() else {
        bail!(
            "ambiguous or unsupported Markdown destination: {}",
            &body[range]
        );
    };
    Ok(span.clone())
}

// The Markdown parser identifies the link/definition; this scanner only locates
// its destination bytes, including escaped and balanced parentheses.
fn destination_span(text: &str, mut start: usize, end: usize) -> Option<Range<usize>> {
    let bytes = text.as_bytes();
    while start < end && bytes[start].is_ascii_whitespace() {
        start += 1;
    }
    let angle = start < end && bytes[start] == b'<';
    if angle {
        start += 1;
    }
    let mut cursor = start;
    let mut depth = 0;
    while cursor < end {
        let byte = bytes[cursor];
        if byte == b'\\' && cursor + 1 < end {
            cursor += 2;
            continue;
        }
        if angle {
            if byte == b'>' {
                return Some(start..cursor);
            }
        } else {
            if byte == b'(' {
                depth += 1;
            }
            if byte == b')' {
                if depth == 0 {
                    return Some(start..cursor);
                }
                depth -= 1;
            }
            if byte.is_ascii_whitespace() {
                return Some(start..cursor);
            }
        }
        cursor += 1;
    }
    (!angle && depth == 0).then_some(start..cursor)
}
