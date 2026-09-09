//! Compiled literate Starlark modules. No loaders, filesystem, clock, or network globals.
use anyhow::{Context, Result, bail};
use pulldown_cmark::{CodeBlockKind, Event, Options, Parser, Tag, TagEnd};
use serde_json::{Value as Json, json};
use starlark::{
    environment::{FrozenModule, GlobalsBuilder, Module},
    eval::Evaluator,
    syntax::{AstModule, Dialect},
    values::{Heap, Value, dict::AllocDict, list::AllocList, structs::AllocStruct},
};

use crate::markdown::{Finding, ParsedPage};

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
fn extract(path: &str, text: &str) -> Result<String> {
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
        .map_err(|error| anyhow::anyhow!("{error}"))?;
        let ast = AstModule::parse(path, source, &Dialect::Standard)
            .map_err(|error| anyhow::anyhow!("{error}"))?;
        Module::with_temp_heap(|module| {
            let definition;
            {
                let mut eval = Evaluator::new(&module);
                limits(&mut eval)?;
                eval.eval_module(prelude, &GlobalsBuilder::standard().with(builtins).build())
                    .map_err(|error| anyhow::anyhow!("{error}"))?;
                eval.eval_module(ast, &GlobalsBuilder::standard().with(builtins).build())
                    .map_err(|error| anyhow::anyhow!("{error}"))?;
                let schema = module
                    .get("mdstore_schema")
                    .context("missing template declaration state")?;
                definition = eval
                    .eval_function(schema, &[], &[])
                    .map_err(|error| anyhow::anyhow!("{error}"))?
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
        before: Option<&Json>,
        after: Option<&Json>,
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
                .map_err(|error| anyhow::anyhow!("{error}"))?;
            check_limits(&eval)
        })
    }
}

fn alloc_json<'v>(heap: Heap<'v>, value: &Json) -> Value<'v> {
    match value {
        Json::Null => Value::new_none(),
        Json::Bool(value) => heap.alloc(*value),
        Json::Number(value) => {
            if let Some(value) = value.as_i64() {
                heap.alloc(value)
            } else if let Some(value) = value.as_u64() {
                heap.alloc(value)
            } else {
                heap.alloc(value.as_f64().expect("JSON number"))
            }
        }
        Json::String(value) => heap.alloc(value.as_str()),
        Json::Array(values) => heap.alloc(AllocList(values.iter().map(|v| alloc_json(heap, v)))),
        Json::Object(values) => heap.alloc(AllocDict(
            values
                .iter()
                .map(|(k, v)| (k.as_str(), alloc_json(heap, v))),
        )),
    }
}

fn alloc_record<'v>(heap: Heap<'v>, value: &Json) -> Value<'v> {
    heap.alloc(AllocStruct(
        value
            .as_object()
            .expect("record")
            .iter()
            .map(|(k, v)| (k.as_str(), alloc_json(heap, v))),
    ))
}

fn alloc_document<'v>(heap: Heap<'v>, value: &Json) -> Value<'v> {
    let mut fields: Vec<_> = value
        .as_object()
        .expect("document")
        .iter()
        .filter(|(key, _)| key.as_str() != "sections")
        .map(|(key, value)| (key.as_str(), alloc_json(heap, value)))
        .collect();
    let sections = value["sections"].as_object().expect("sections");
    let sections = heap.alloc(AllocDict(sections.iter().map(|(name, section)| {
        let entries = heap.alloc(AllocList(
            section["entries"]
                .as_array()
                .expect("entries")
                .iter()
                .map(|entry| alloc_record(heap, entry)),
        ));
        let mut fields: Vec<_> = section
            .as_object()
            .expect("section")
            .iter()
            .filter(|(key, _)| key.as_str() != "entries")
            .map(|(key, value)| (key.as_str(), alloc_json(heap, value)))
            .collect();
        fields.push(("entries", entries));
        (name.as_str(), heap.alloc(AllocStruct(fields)))
    })));
    fields.push(("sections", sections));
    heap.alloc(AllocStruct(fields))
}

/// Parsed data exposed to callbacks; no mutable store objects cross the boundary.
pub(crate) fn document(path: &str, text: &str, page: &ParsedPage) -> Json {
    let mut offsets = vec![0];
    offsets.extend(text.match_indices('\n').map(|(offset, _)| offset + 1));
    let body_start = offsets
        .get(page.body_start_line - 1)
        .copied()
        .unwrap_or(text.len());
    let mut headings = Vec::new();
    let mut depth = 0;
    for (event, range) in Parser::new_ext(&text[body_start..], Options::all()).into_offset_iter() {
        match event {
            Event::Start(tag) => {
                if let Tag::Heading { level, .. } = tag {
                    let line =
                        offsets.partition_point(|offset| *offset <= range.start + body_start);
                    if depth == 0
                        && let Some(heading) =
                            page.headings.iter().find(|heading| heading.line == line)
                    {
                        headings.push((
                            level as u8,
                            heading.text.clone(),
                            line,
                            range.start + body_start,
                            range.end + body_start,
                        ));
                    }
                }
                depth += 1;
            }
            Event::End(_) => depth -= 1,
            _ => {}
        }
    }
    let mut sections = serde_json::Map::new();
    for (index, (level, name, line, _, start)) in headings.iter().enumerate() {
        let end = headings[index + 1..]
            .iter()
            .find(|h| h.0 <= *level)
            .map_or(text.len(), |h| h.3);
        let content = &text[*start..end];
        let mut entries = Vec::new();
        let mut depth = 0;
        for (event, range) in Parser::new_ext(content, Options::all()).into_offset_iter() {
            match event {
                Event::Start(tag) => {
                    if matches!(tag, Tag::Item) && depth == 1 {
                        let raw = &content[range.clone()];
                        let stripped = raw
                            .trim_start()
                            .trim_start_matches(['-', '+', '*'])
                            .trim_start();
                        let token_end =
                            stripped.find(char::is_whitespace).unwrap_or(stripped.len());
                        let stamp = &stripped[..token_end];
                        let timestamp = chrono::DateTime::parse_from_rfc3339(stamp).ok();
                        let entry_line =
                            offsets.partition_point(|offset| *offset <= *start + range.start);
                        let entry_end =
                            offsets.partition_point(|offset| *offset < *start + range.end);
                        entries.push(json!({
                            "timestamp": timestamp.map(|date| date.to_rfc3339()),
                            "text": if timestamp.is_some() { stripped[token_end..].trim_start().trim_start_matches('—').trim_start() } else { stripped },
                            "line": entry_line,
                            "links": page.links.iter().filter(|link| link.line >= entry_line && link.line <= entry_end).map(|link| &link.target).collect::<Vec<_>>(),
                        }));
                    }
                    depth += 1;
                }
                Event::End(_) => depth -= 1,
                _ => {}
            }
        }
        sections.insert(
            name.clone(),
            json!({"text":content,"line":line,"level":level,"entries":entries}),
        );
    }
    json!({"path":path,"text":text,"frontmatter":page.frontmatter,"sections":sections,"links":page.links,"line":1})
}

pub(crate) fn finding(path: &str, text: &str, template: &str, error: &anyhow::Error) -> Finding {
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
        path: path.to_owned(),
        line,
        message: format!("{template}: {message}"),
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{markdown::validate_corpus, template::Templates};
    use std::collections::HashMap;

    const TEMPLATE: &str = r#"# Tasks

## State
Explain external dependencies.

```starlark
frontmatter(title=string(required=True, min_length=1), state=enum(["inbox", "ready", "waiting", "completed"], required=True), waiting_on=string(nullable=True))

def waiting(doc):
    if doc.frontmatter["state"] == "waiting":
        require(bool((doc.frontmatter.get("waiting_on") or "").strip()), "Explain the dependency", field="waiting_on")

validate(waiting)
```

## Timeline
Record messages and decisions in order.

```starlark
section("Timeline", required=True, content=dated_list())

def transition(before, after):
    if before != None and after != None:
        old = before.frontmatter["state"]
        new = after.frontmatter["state"]
        require(old != "inbox" or new != "completed", "Clarify before completing", field="state")
        if old != new:
            require(len(after.sections["Timeline"].entries) > len(before.sections["Timeline"].entries), "Record the transition", at=after.sections["Timeline"])

validate_change(transition)
```
"#;

    fn templates(source: &str) -> Templates {
        Templates::compile(&HashMap::from([(
            "tasks/template.md".into(),
            source.into(),
        )]))
        .unwrap()
    }

    fn page(state: &str) -> String {
        format!(
            "---\ntitle: Review\nstate: {state}\n---\n# Task\n\n## Timeline\n- 2026-09-09T12:00:00+02:00 — Captured.\n"
        )
    }

    fn check(source: &str, content: &str) -> Vec<Finding> {
        validate_corpus(
            &HashMap::from([("tasks/a.md".into(), content.into())]),
            &templates(source),
        )
        .err()
        .unwrap_or_default()
    }

    #[test]
    fn shipped_example_and_directory_versions() {
        let templates = Templates::compile(&HashMap::from([
            (
                "tasks/v1/template.md".into(),
                include_str!("../examples/tasks/v1/template.md").into(),
            ),
            (
                "tasks/v2/template.md".into(),
                "```starlark\nfrontmatter(owner=string(required=True))\n```\n".into(),
            ),
        ]))
        .unwrap();
        let path = "tasks/v1/2026/09/09-001-review.md";
        let before = HashMap::from([(path.into(), page("inbox"))]);
        validate_corpus(&before, &templates).unwrap();
        let after = HashMap::from([(
            path.into(),
            format!("{}- 2026-09-09T11:00:00Z — Clarified.\n", page("ready")),
        )]);
        validate_corpus(&after, &templates).unwrap();
        templates.validate_changes(&before, &after).unwrap();
        assert!(
            validate_corpus(
                &HashMap::from([("tasks/v2/a.md".into(), page("inbox"))]),
                &templates
            )
            .is_err()
        );
    }

    #[test]
    fn literate_schema_callbacks_and_timeline() {
        assert!(check(TEMPLATE, &page("inbox")).is_empty());
        for content in [
            page("unknown"),
            page("waiting"),
            page("waiting").replace("state: waiting", "state: waiting\nwaiting_on: null"),
            page("inbox").replace("title: Review", "title: 123"),
            page("inbox").replace("title: Review", "title: Review\nextra: true"),
            page("inbox").replace("## Timeline", "### Timeline"),
            page("inbox").replace("2026-09-09T12:00:00+02:00", "2026-02-30T12:00:00Z"),
            format!("{}- 2026-09-09T09:59:59Z — Earlier.\n", page("inbox")),
            page("inbox").replace("— Captured.", "—"),
        ] {
            assert!(!check(TEMPLATE, &content).is_empty(), "{content}");
        }
        assert!(
            check(
                TEMPLATE,
                &format!("{}- 2026-09-09T10:00:01Z — Later.\n", page("inbox"))
            )
            .is_empty()
        );
        let findings = check(TEMPLATE, &page("waiting"));
        assert!(findings[0].message.contains("tasks/template.md"));
        assert!(findings[0].message.contains("Explain the dependency"));
        assert!(
            findings[0].message.contains("tasks/template.md:"),
            "{}",
            findings[0].message
        );
        let schema_error = check(TEMPLATE, &page("unknown"));
        assert_eq!(schema_error[0].line, Some(3));
    }

    #[test]
    fn change_callbacks_see_both_snapshots() {
        let templates = templates(TEMPLATE);
        let before = HashMap::from([("tasks/a.md".into(), page("inbox"))]);
        let after = |state, extra: &str| {
            HashMap::from([("tasks/a.md".into(), format!("{}{extra}", page(state)))])
        };
        assert!(
            templates
                .validate_changes(
                    &before,
                    &after("completed", "- 2026-09-09T11:00:00Z — Done.\n")
                )
                .is_err()
        );
        assert!(
            templates
                .validate_changes(&before, &after("ready", ""))
                .is_err()
        );
        assert!(
            templates
                .validate_changes(
                    &before,
                    &after("ready", "- 2026-09-09T11:00:00Z — Clarified.\n")
                )
                .is_ok()
        );
        assert!(templates.validate_changes(&HashMap::new(), &before).is_ok());
        assert!(templates.validate_changes(&before, &HashMap::new()).is_ok());
    }

    #[test]
    fn only_exact_top_level_starlark_fences_execute_and_blocks_share_scope() {
        let source = "# Timeline\n\n```python\ninvalid syntax !\n```\n\n> ```starlark\n> fail('example')\n> ```\n\n```starlark\nname = 'title'\n```\nProse\n```starlark\nfrontmatter(fields={name: string(required=True)})\n```\n";
        assert!(check(source, "---\ntitle: Example\n---\nAnything.\n").is_empty());
        assert!(!check(source, "Anything.\n").is_empty());
        assert!(
            templates("# Timeline\nRequired in prose only.\n")
                .discovery("tasks/a.md")
                .is_some()
        );
        assert!(check("# Timeline\nRequired in prose only.\n", "Anything.\n").is_empty());
        for source in [
            "```starlark\nx = 1\n",
            "```starlark schema\nx = 1\n```\n",
            "```starlark\nload('file.star', 'x')\n```\n",
        ] {
            assert!(Script::compile("tasks/template.md", source).is_err());
        }
    }

    #[test]
    fn execution_is_bounded_and_frozen_between_documents() {
        let source = "```starlark\ndef endless(doc):\n    for x in range(200000):\n        pass\nvalidate(endless)\n```\n";
        assert!(check(source, "Hello\n")[0].message.contains("tick"));
        let source = "```starlark\nstate = []\ndef mutate(doc):\n    state.append(1)\nvalidate(mutate)\n```\n";
        assert!(!check(source, "Hello\n").is_empty());
        let source = "```starlark\ndef mutate(doc):\n    doc.frontmatter['state'] = 'completed'\nvalidate(mutate)\n```\n";
        assert!(!check(source, &page("inbox")).is_empty());
        for source in [
            "```starlark\nx = open('file')\n```\n",
            "```starlark\nfrontmatter(x=string(min_length=-1))\n```\n",
            "```starlark\nvalidate(123)\n```\n",
            "```starlark\nvalidate_change('not a function')\n```\n",
        ] {
            assert!(
                Templates::compile(&HashMap::from([("template.md".into(), source.into())]))
                    .is_err()
            );
        }
    }

    #[test]
    fn filename_allocation_uses_scope_and_all_reserved_paths() {
        let source = r#"```starlark
filename(r"tasks/(?P<year>[0-9]{4})/(?P<month>[0-9]{2})/(?P<day>[0-9]{2})-(?P<serial>[0-9]+)-[a-z-]+\.md", serial_scope=["year", "month", "day"])
```"#;
        let templates = templates(source);
        let paths = ["tasks/2026/09/09-002-old.md", "tasks/2026/09/08-999-old.md"]
            .map(String::from)
            .into_iter()
            .collect();
        assert_eq!(
            templates
                .allocate_path("tasks/2026/09/09-{serial:03}-new.md", &paths)
                .unwrap(),
            "tasks/2026/09/09-003-new.md"
        );
        assert!(templates.allocate_path("tasks/{oops}.md", &paths).is_err());
        assert!(
            templates
                .allocate_path("tasks/{serial}-{serial}.md", &paths)
                .is_err()
        );
        assert!(
            templates
                .allocate_path("tasks/2026/09/09-{serial:99}-new.md", &paths)
                .is_err()
        );
        assert!(
            templates
                .allocate_path("tasks/2026/09/{serial:02}-001-new.md", &paths)
                .is_err()
        );
        let paths = ["tasks/2026/09/09-999-old.md".into()].into_iter().collect();
        assert_eq!(
            templates
                .allocate_path("tasks/2026/09/09-{serial:03}-new.md", &paths)
                .unwrap(),
            "tasks/2026/09/09-1000-new.md"
        );
    }

    #[test]
    fn malformed_frontmatter_is_rejected_before_callbacks() {
        for content in ["---\na: 1\na: 2\n---\n", "---\n[1,2]\n---\n"] {
            assert!(!check("# Empty policy\n", content).is_empty());
        }
    }
}
