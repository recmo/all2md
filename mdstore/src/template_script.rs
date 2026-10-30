//! Compiled literate Starlark modules. No loaders, filesystem, clock, or network globals.
use anyhow::{Context, Result, bail};
use pulldown_cmark::{CodeBlockKind, Event, Options, Parser, Tag, TagEnd};
use serde_json::Value as Json;
use starlark::{
    environment::{FrozenModule, GlobalsBuilder, Module},
    eval::Evaluator,
    syntax::{AstModule, Dialect},
    values::{Heap, Value, dict::AllocDict, list::AllocList, structs::AllocStruct},
};

use crate::markdown::{Finding, ParsedPage};

#[derive(Debug)]
struct ScriptError {
    message: String,
    source: Option<crate::markdown::FindingSource>,
}
impl std::fmt::Display for ScriptError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str(&self.message) }
}
impl std::error::Error for ScriptError {}

fn script_error(error: starlark::Error) -> anyhow::Error {
    // require() fails inside the built-in prelude: use its closest user caller.
    let location = error.span().into_iter()
        .chain(error.call_stack().frames.iter().rev().filter_map(|frame| frame.location.as_ref()))
        .find(|span| !span.filename().starts_with('<'));
    let source = location.map(|span| crate::markdown::FindingSource {
        path: span.filename().into(), line: span.file.resolve_span(span.span).begin.line + 1,
    });
    let message = match error.kind() {
        starlark::ErrorKind::Fail(message) => message.to_string(),
        _ => error.without_diagnostic().to_string(),
    };
    ScriptError {message, source}.into()
}

pub(crate) struct Script {
    module: FrozenModule,
    locations: Json,
    pub(crate) has_change_checks: bool,
    guidance: Vec<(usize, String)>,
}

#[starlark::starlark_module]
fn builtins(builder: &mut GlobalsBuilder) {
    fn _location(eval: &mut Evaluator<'_, '_, '_>) -> anyhow::Result<i32> {
        for depth in 0..eval.call_stack_count() {
            if let Some(span) = eval.call_stack_nth_location(depth)
                && span.filename() != "<mdstore>"
            {
                return Ok((span.resolve_span().begin.line + 1) as i32);
            }
        }
        Ok(1)
    }
}

fn limits(eval: &mut Evaluator<'_, '_, '_>) -> Result<()> {
    eval.set_max_tick_count(100_000)?;
    eval.set_max_heap_size(16 * 1024 * 1024)?;
    eval.set_max_callstack_size(64)?;
    Ok(())
}

fn check_limits(eval: &Evaluator<'_, '_, '_>) -> Result<()> {
    if eval.get_total_tick_count() > 100_000 {
        bail!("template tick limit exceeded");
    }
    if eval.heap().peak_allocated_bytes() + eval.frozen_heap().allocated_bytes() > 16 * 1024 * 1024
    {
        bail!("template heap limit exceeded");
    }
    Ok(())
}

/// Extract only top-level fences with the exact info string `starlark`.
/// Blank lines retain the Markdown source positions; examples remain inert.
pub fn extract(path: &str, text: &str) -> Result<String> {
    if text.len() > 1024 * 1024 {
        bail!("template exceeds 1 MiB");
    }
    let mut output: Vec<String> = text.split_inclusive('\n').map(|_| "\n".into()).collect();
    let mut depth = 0;
    let mut code = false;
    for (event, range) in Parser::new_ext(text, Options::all()).into_offset_iter() {
        match event {
            Event::Start(Tag::CodeBlock(CodeBlockKind::Fenced(info))) => {
                code = depth == 0 && info.as_ref() == "starlark";
                if depth == 0 && info.starts_with("starlark") && !code {
                    bail!("{path}: use the exact fence info string starlark");
                }
                if code {
                    // CommonMark accepts unclosed fences; executable templates do not.
                    let raw = &text[range.clone()];
                    let first = raw.lines().next().unwrap_or_default().trim_start();
                    let marker = first.chars().next().unwrap_or('`');
                    let count = first.chars().take_while(|c| *c == marker).count();
                    let last = raw.lines().last().unwrap_or_default().trim();
                    if raw.lines().count() < 2
                        || last.len() < count
                        || !last.chars().all(|c| c == marker)
                    {
                        bail!("{path}: unclosed executable fence");
                    }
                }
                depth += 1;
            }
            Event::Start(_) => depth += 1,
            Event::End(TagEnd::CodeBlock) => {
                depth -= 1;
                code = false;
            }
            Event::End(_) => depth -= 1,
            Event::Text(value) if code => {
                let line = text[..range.start].bytes().filter(|c| *c == b'\n').count();
                for (index, source) in value.split_inclusive('\n').enumerate() {
                    output[line + index] = source.to_owned();
                }
            }
            _ => {}
        }
    }
    Ok(output.concat())
}

impl Script {
    pub(crate) fn compile(path: &str, text: &str) -> Result<(Self, Json)> {
        let source = extract(path, text)?;
        let prelude = AstModule::parse(
            "<mdstore>",
            include_str!("template_prelude.star").into(),
            &Dialect::Standard,
        )
        .map_err(script_error)?;
        let ast = AstModule::parse(path, source, &Dialect::Standard)
            .map_err(script_error)?;
        Module::with_temp_heap(|module| {
            let definition;
            {
                let mut eval = Evaluator::new(&module);
                limits(&mut eval)?;
                eval.eval_module(prelude, &GlobalsBuilder::standard().with(builtins).build())
                    .map_err(script_error)?;
                eval.eval_module(ast, &GlobalsBuilder::standard().with(builtins).build())
                    .map_err(script_error)?;
                let schema = module
                    .get("mdstore_schema")
                    .context("missing template declaration state")?;
                definition = eval
                    .eval_function(schema, &[], &[])
                    .map_err(script_error)?
                    .to_json()?;
                check_limits(&eval)?;
            }
            let module = module
                .freeze()
                .map_err(|error| anyhow::anyhow!("{error:?}"))?;
            let bundle: Json = serde_json::from_str(&definition)?;
            let mut guidance = Vec::new();
            let mut heading = None;
            for (event, range) in Parser::new(text).into_offset_iter() {
                match event {
                    Event::Start(Tag::Heading { .. }) => {
                        heading = Some((
                            text[..range.start].bytes().filter(|c| *c == b'\n').count() + 1,
                            String::new(),
                        ));
                    }
                    Event::Text(value) if heading.is_some() => {
                        heading.as_mut().expect("heading").1.push_str(&value);
                    }
                    Event::End(TagEnd::Heading(_)) => {
                        guidance.push(heading.take().expect("heading"));
                    }
                    _ => {}
                }
            }
            Ok((
                Self {
                    module,
                    locations: bundle["locations"].clone(),
                    guidance,
                    has_change_checks: bundle["changes"].as_u64().unwrap_or(0) > 0,
                },
                bundle["definition"].clone(),
            ))
        })
    }

    pub(crate) fn rule_context(&self, key: &str) -> String {
        let Some(line) = self.locations.get(key).and_then(Json::as_u64) else {
            return String::new();
        };
        let heading = self
            .guidance
            .iter()
            .rev()
            .find(|(start, _)| *start <= line as usize);
        format!(
            ":{line}{}",
            heading.map_or(String::new(), |(_, name)| format!(" ({name})"))
        )
    }

    pub(crate) fn check(
        &self,
        before: Option<&Document<'_>>,
        after: Option<&Document<'_>>,
        change: bool,
    ) -> Result<()> {
        let inputs = Module::with_temp_heap(|module| {
            let heap = module.heap();
            module.set(
                "before",
                before.map_or(Value::new_none(), |doc| alloc_document(heap, doc)),
            );
            module.set(
                "after",
                after.map_or(Value::new_none(), |doc| alloc_document(heap, doc)),
            );
            module
                .freeze()
                .map_err(|error| anyhow::anyhow!("{error:?}"))
        })?;
        Module::with_temp_heap(|module| {
            let heap = module.heap();
            let callback = self.module.get(if change {
                "mdstore_validate_change"
            } else {
                "mdstore_validate"
            })?;
            let callback = heap.access_owned_frozen_value(&callback);
            let after = heap.access_owned_frozen_value(&inputs.get("after")?);
            let args = if change {
                vec![
                    heap.access_owned_frozen_value(&inputs.get("before")?),
                    after,
                ]
            } else {
                vec![after]
            };
            let mut eval = Evaluator::new(&module);
            limits(&mut eval)?;
            eval.eval_function(callback, &args, &[])
                .map_err(script_error)?;
            check_limits(&eval)
        })
    }
}

pub(crate) struct Document<'a> {
    path: &'a str,
    text: &'a str,
    page: &'a ParsedPage,
}

pub(crate) fn document<'a>(path: &'a str, text: &'a str, page: &'a ParsedPage) -> Document<'a> {
    Document { path, text, page }
}

fn alloc_document<'v>(heap: Heap<'v>, doc: &Document<'_>) -> Value<'v> {
    let page = doc.page;
    let mut counts = std::collections::HashMap::new();
    for (_, name, _, _) in &page.section_headings {
        *counts.entry(name).or_insert(0) += 1;
    }
    // A name identifies a section only when it is unique across the document.
    // Ambiguous names are unavailable, so lookup cannot silently select a different scope.
    let sections = heap.alloc(AllocDict(
        page.section_headings
            .iter()
            .enumerate()
            .filter(|(_, (_, name, _, _))| counts[name] == 1)
            .map(|(index, (level, name, start, content_start))| {
                let end = page.section_headings[index + 1..]
                    .iter()
                    .find(|heading| heading.0 <= *level)
                    .map_or(doc.text.len(), |heading| heading.2);
                let entries = heap.alloc(AllocList(
                    page.list_entries
                        .iter()
                        .filter(|entry| {
                            entry.range.start >= *content_start && entry.range.end <= end
                        })
                        .map(|entry| {
                            let timestamp = entry
                                .timestamp
                                .map_or(Value::new_none(), |stamp| heap.alloc(stamp.to_rfc3339()));
                            let end_line = entry.line
                                + doc.text[entry.range.clone()]
                                    .trim_end_matches('\n')
                                    .bytes()
                                    .filter(|c| *c == b'\n')
                                    .count();
                            let links = heap.alloc(AllocList(
                                page.links
                                    .iter()
                                    .filter(|link| !link.image)
                                    .filter(|link| (entry.line..=end_line).contains(&link.line))
                                    .map(|link| link.target.as_str()),
                            ));
                            heap.alloc(AllocStruct([
                                ("timestamp", timestamp),
                                (
                                    "text",
                                    heap.alloc(&doc.text[entry.text_start..entry.range.end]),
                                ),
                                ("line", heap.alloc(entry.line)),
                                ("links", links),
                            ]))
                        }),
                ));
                (
                    name.as_str(),
                    heap.alloc(AllocStruct([
                        ("text", heap.alloc(&doc.text[*content_start..end])),
                        ("level", heap.alloc(*level as u32)),
                        (
                            "line",
                            heap.alloc(
                                doc.text[..*start].bytes().filter(|c| *c == b'\n').count() + 1,
                            ),
                        ),
                        ("entries", entries),
                    ])),
                )
            }),
    ));
    let links = heap.alloc(AllocList(page.links.iter().filter(|link| !link.image).map(
        |link| {
            heap.alloc(AllocDict([
                ("target", heap.alloc(link.target.as_str())),
                ("line", heap.alloc(link.line)),
                (
                    "syntax",
                    heap.alloc(match link.syntax {
                        crate::markdown::LinkSyntax::Markdown => "markdown",
                        crate::markdown::LinkSyntax::Wiki => "wiki",
                    }),
                ),
                (
                    "sections",
                    heap.alloc(AllocList(link.sections.iter().map(String::as_str))),
                ),
            ]))
        },
    )));
    heap.alloc(AllocStruct([
        ("path", heap.alloc(doc.path)),
        ("text", heap.alloc(doc.text)),
        ("title", heap.alloc(page.headings.iter().find(|heading| heading.level == 1).map(|heading| heading.text.as_str()).unwrap_or(""))),
        ("frontmatter", heap.alloc(&page.frontmatter)),
        ("sections", sections),
        ("links", links),
        ("line", heap.alloc(1_i32)),
    ]))
}

pub(crate) fn finding(path: &str, text: &str, _template: &str, error: &anyhow::Error) -> Finding {
    let message = error.to_string();
    let line = message
        .split("[mdstore-line:")
        .nth(1)
        .and_then(|value| value.split(']').next())
        .and_then(|line| line.parse().ok())
        .or_else(|| {
            message
                .split("[mdstore-field:")
                .nth(1)
                .and_then(|value| value.split(']').next())
                .map(|field| field_line(text, field))
        });
    Finding {
        source: error.downcast_ref::<ScriptError>().and_then(|error| error.source.clone()),
        path: path.to_owned(),
        line,
        message: {
            let mut clean = message.trim();
            while clean.starts_with("[mdstore-line:") || clean.starts_with("[mdstore-field:") {
                let Some((_, rest)) = clean.split_once(']') else { break; };
                clean = rest.trim_start();
            }
            clean.to_owned()
        },
    }
}

pub(crate) fn field_line(text: &str, field: &str) -> usize {
    text.lines()
        .enumerate()
        .skip(1)
        .take_while(|(_, line)| line.trim() != "---")
        .find(|(_, line)| {
            line.split_once(':')
                .is_some_and(|(key, _)| key.trim().trim_matches(['\'', '"']) == field)
        })
        .map_or(1, |(index, _)| index + 1)
}
