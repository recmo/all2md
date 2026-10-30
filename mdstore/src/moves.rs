use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    path::Path,
};

use anyhow::{Context, Result, bail};
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
    let final_resolver =
        TargetResolver::new(edited.keys().map(|path| mapping.get(path).unwrap_or(path)));
    let mut output = HashMap::new();
    for (source, text) in edited {
        let destination = mapping.get(source).unwrap_or(source);
        let policy = old_templates.policy(source);
        let page = markdown::parse_page(text, &policy.links)
            .with_context(|| format!("{source}: locate move references"))?;
        let mut replacements = BTreeMap::new();
        for link in &page.links {
            let Some((target, document)) = resolver.resolve_link(source, link)? else {
                continue;
            };
            if document && !edited.contains_key(&target) {
                bail!(
                    "{source}: link to deleted document {target}; remove it or explicitly retarget it before moving"
                );
            }
            let final_target = mapping.get(&target).unwrap_or(&target);
            let range = link
                .target_range
                .as_ref()
                .context("cannot locate link destination safely")?;
            let raw = &text[range.clone()];
            let replacement = if target == *final_target && source == destination {
                raw.to_owned()
            } else {
                let authored = link.target.split(['#', '?']).next().unwrap_or_default();
                let path = render_path(
                    destination,
                    final_target,
                    authored.starts_with('/'),
                    document && !link.image && !authored.ends_with(".md"),
                    link.syntax,
                );
                format!("{path}{}", link.source_suffix(text)?)
            };
            let (previous, targets) = replacements
                .entry((range.start, range.end))
                .or_insert_with(|| (replacement.clone(), Vec::new()));
            if *previous != replacement {
                bail!("{source}: conflicting link rewrites");
            }
            targets.push(final_target.clone());
        }
        let mut rewritten = String::with_capacity(text.len());
        let mut expected = Vec::new();
        let mut cursor = 0;
        for ((start, end), (replacement, targets)) in replacements {
            if start < cursor {
                bail!("{source}: overlapping link rewrites");
            }
            rewritten.push_str(&text[cursor..start]);
            let new_start = rewritten.len();
            rewritten.push_str(&replacement);
            expected.extend(
                targets
                    .into_iter()
                    .map(|target| (new_start, rewritten.len(), target)),
            );
            cursor = end;
        }
        rewritten.push_str(&text[cursor..]);
        // Check the new schema's grammar as well as the destination paths. A wiki
        // pattern may not be able to express a new filename or may change by directory.
        let mut actual = Vec::new();
        let reparsed = markdown::parse_page(&rewritten, &new_templates.policy(destination).links)?;
        for link in &reparsed.links {
            if let Some((target, _)) = final_resolver.resolve_link(destination, link)? {
                let range = link
                    .target_range
                    .as_ref()
                    .context("cannot locate rewritten destination safely")?;
                actual.push((range.start, range.end, target));
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
