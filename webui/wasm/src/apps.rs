//! Plain Starlark collection functions over an in-memory document list.
use anyhow::{Context, Result, bail};
use serde::Deserialize;
use serde_json::Value as Json;
use starlark::{environment::{GlobalsBuilder, Module}, eval::Evaluator, syntax::{AstModule, Dialect}, values::{list::AllocList, structs::AllocStruct}};

#[derive(Deserialize)]
pub(crate) struct Input {
    pub path: String,
    pub source: String,
    pub documents: Vec<Json>,
    pub action: Option<String>,
    pub event: Option<Json>,
}

pub(crate) fn evaluate(input: Input) -> Result<Json> {
    run(input)
}

pub(crate) fn is_app(text: &str) -> bool {
    mdstore::validation::parse_frontmatter(text).is_ok_and(|(value, _, _)| value["mdstore"] == "app")
}

pub(crate) fn validate_definition(path: &str, source: &str, documents: Vec<Json>) -> Result<()> {
    evaluate(Input { path: path.into(), source: source.into(), documents, action: None, event: None }).map(|_| ())
}

fn run(input: Input) -> Result<Json> {
    let source = mdstore::validation::extract_starlark(&input.path, &input.source)?;
    let inputs = Module::with_temp_heap(|module| {
        let heap = module.heap();
        let documents = heap.alloc(AllocList(input.documents.iter().map(|doc| {
            heap.alloc(AllocStruct(doc.as_object().into_iter().flatten().map(|(k, v)| (k.as_str(), heap.alloc(v)))))
        })));
        module.set("documents", documents);
        module.freeze().map_err(|e| anyhow::anyhow!("{e:?}"))
    })?;
    Module::with_temp_heap(|module| {
        let heap = module.heap();
        let documents = heap.access_owned_frozen_value(&inputs.get("documents")?);
        let globals = GlobalsBuilder::standard().build();
        let mut eval = Evaluator::new(&module);
        eval.set_max_tick_count(1_000_000)?;
        eval.set_max_heap_size(64 * 1024 * 1024)?;
        eval.set_max_callstack_size(64)?;
        for (path, text) in [("<mdstore-app>", include_str!("app_prelude.star").to_owned()), (input.path.as_str(), source)] {
            let ast = AstModule::parse(path, text, &Dialect::Standard).map_err(|e| anyhow::anyhow!("{e}"))?;
            eval.eval_module(ast, &globals).map_err(|e| anyhow::anyhow!("{e}"))?;
        }
        let runner = module.get("mdstore_app_run").context("missing app runner")?;
        let args = [documents, input.action.as_ref().map_or(starlark::values::Value::new_none(), |v| heap.alloc(v)), input.event.as_ref().map_or(starlark::values::Value::new_none(), |v| heap.alloc(v))];
        let result = eval.eval_function(runner, &args, &[])
            .map_err(|e| anyhow::anyhow!("{e}"))?.to_json()?;
        if result.len() > 8 * 1024 * 1024 { bail!("app result exceeds 8 MiB"); }
        Ok(serde_json::from_str(&result)?)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn run(source: &str) -> Result<Json> {
        evaluate(Input { path: "app.md".into(), source: format!("```starlark\n{source}\n```\n"), documents: vec![serde_json::json!({"path":"a.md", "title":"A", "template":"template.md", "frontmatter":{"state":"ready"}})], action: None, event: None })
    }
    #[test]
    fn collections_use_plain_functions_and_views_are_declarative() {
        let result = run("def ready(docs):\n    return [d for d in docs if d.frontmatter.get('state') == 'ready']\ncollection('ready', ready)\nkanban('Board', 'ready', group='/state')").unwrap();
        assert_eq!(result["collections"]["ready"], serde_json::json!(["a.md"]));
        assert_eq!(result["views"][0]["kind"], "kanban");
    }
    #[test]
    fn gantt_grouping_is_an_optional_field_binding() {
        let result = run("collection('all', lambda docs: docs)\ngantt('Schedule', 'all', start='/start', end='/end')\ngantt('Workload', 'all', start='/start', end='/end', group='/assignee', dependencies='/depends_on')").unwrap();
        assert_eq!(result["views"][0]["kind"], "gantt");
        assert!(result["views"][0]["bindings"].get("group").is_none());
        assert_eq!(result["views"][1]["kind"], "gantt");
        assert_eq!(result["views"][1]["bindings"]["group"], "/assignee");
        assert_eq!(result["views"][1]["bindings"]["dependencies"], "/depends_on");
        assert!(run("collection('all', lambda docs: docs)\ngantt('Bad', 'all', start='/start', end='/end', dependencies=42)").is_err());
        assert!(run("collection('all', lambda docs: docs)\ngantt('Bad', 'all', start='/start', end='/end', group=42)").is_err());
    }
    #[test]
    fn collection_inputs_are_immutable_and_bindings_checked() {
        assert!(run("def bad(docs):\n    docs[0].frontmatter['state'] = 'changed'\n    return docs\ncollection('bad', bad)").is_err());
        assert!(run("collection('all', lambda docs: docs)\ntable('Bad', 'all', columns=[42])").is_err());
    }
    #[test]
    fn invalid_references_and_external_access_fail() {
        assert!(run("table('Bad', 'missing', columns=[])").is_err());
        assert!(run("load('external.star', 'secret')").is_err());
        assert!(run("open('/tmp/secret')").is_err());
    }
    #[test]
    fn task_example_runs_and_action_returns_proposed_edits() {
        let mut input = Input { path: "tasks/v1/app.md".into(), source: include_str!("../../examples/tasks/v1/app.md").into(), documents: vec![serde_json::json!({"path":"tasks/v1/a.md", "title":"A", "template":"/tasks/v1/template.md", "frontmatter":{"state":"inbox"}, "text":null})], action: None, event: None };
        assert_eq!(evaluate(Input { path: input.path.clone(), source: input.source.clone(), documents: input.documents.clone(), action: None, event: None }).unwrap()["views"].as_array().unwrap().len(), 4);
        input.action = Some("change-state".into());
        input.event = Some(serde_json::json!({"path":"tasks/v1/a.md","value":"ready","timestamp":"2026-09-23T10:00:00Z"}));
        assert_eq!(evaluate(input).unwrap()["edits"][0]["fields"]["state"], "ready");
    }
}
