//! Plain Starlark collection functions over an in-memory document list.
use anyhow::{Context, Result, bail};
use serde::Deserialize;
use serde_json::Value as Json;
use starlark::{
    environment::{GlobalsBuilder, Module},
    eval::Evaluator,
    syntax::{AstModule, Dialect},
    values::{list::AllocList, structs::AllocStruct},
};

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
    mdstore::validation::parse_frontmatter(text)
        .is_ok_and(|(value, _, _)| value["mdstore"] == "app")
}

pub(crate) fn validate_definition(path: &str, source: &str, documents: Vec<Json>) -> Result<()> {
    evaluate(Input {
        path: path.into(),
        source: source.into(),
        documents,
        action: None,
        event: None,
    })
    .map(|_| ())
}

fn run(input: Input) -> Result<Json> {
    let source = mdstore::validation::extract_starlark(&input.path, &input.source)?;
    let inputs = Module::with_temp_heap(|module| {
        let heap = module.heap();
        let documents = heap.alloc(AllocList(input.documents.iter().map(|doc| {
            heap.alloc(AllocStruct(
                doc.as_object()
                    .into_iter()
                    .flatten()
                    .map(|(k, v)| (k.as_str(), heap.alloc(v))),
            ))
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
        for (path, text) in [
            ("<mdstore-app>", include_str!("app_prelude.star").to_owned()),
            (input.path.as_str(), source),
        ] {
            let ast = AstModule::parse(path, text, &Dialect::Standard)
                .map_err(|e| anyhow::anyhow!("{e}"))?;
            eval.eval_module(ast, &globals)
                .map_err(|e| anyhow::anyhow!("{e}"))?;
        }
        let runner = module
            .get("mdstore_app_run")
            .context("missing app runner")?;
        let args = [
            documents,
            input
                .action
                .as_ref()
                .map_or(starlark::values::Value::new_none(), |v| heap.alloc(v)),
            input
                .event
                .as_ref()
                .map_or(starlark::values::Value::new_none(), |v| heap.alloc(v)),
        ];
        let result = eval
            .eval_function(runner, &args, &[])
            .map_err(|e| anyhow::anyhow!("{e}"))?
            .to_json()?;
        if result.len() > 8 * 1024 * 1024 {
            bail!("app result exceeds 8 MiB");
        }
        Ok(serde_json::from_str(&result)?)
    })
}
