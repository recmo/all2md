//! Non-document resources share MCP get/edit/search; worker leases stay explicit.
use super::*;
use crate::store::artifacts::{Completion, Derivation};

#[derive(Deserialize)]
#[serde(tag = "resource", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum Read {
    Derivations,
    Jobs,
}
#[derive(Deserialize)]
#[serde(tag = "resource", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum Edit {
    Derivations(DerivationEdit),
    Jobs { id: String, action: Retry },
}
#[derive(Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum DerivationEdit {
    Create { definition: Derivation },
    Update { id: String, inputs: Vec<String> },
    Delete { id: String },
}
#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum Retry {
    Retry,
}

pub(super) fn read(store: &Store, args: Read) -> Result<Value> {
    match args {
        Read::Derivations => store.derivations(),
        Read::Jobs => Ok(json!({"jobs": store.jobs()?})),
    }
}
pub(super) fn edit(store: &Store, args: Edit) -> Result<Value> {
    match args {
        Edit::Derivations(DerivationEdit::Create { definition }) => {
            Ok(json!({"id": store.create_derivation(definition)?}))
        }
        Edit::Derivations(DerivationEdit::Update { id, inputs }) => {
            store.update_derivation_inputs(&id, inputs)?;
            Ok(json!({}))
        }
        Edit::Derivations(DerivationEdit::Delete { id }) => {
            store.remove_derivation(&id)?;
            Ok(json!({}))
        }
        Edit::Jobs {
            id,
            action: Retry::Retry,
        } => {
            store.retry_job(&id)?;
            Ok(json!({}))
        }
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct VectorQuery {
    space: crate::vectors::EmbeddingSpace,
    vector: Vec<f32>,
    limit: usize,
    #[serde(default)]
    filter: std::collections::BTreeMap<String, Value>,
    job: Option<String>,
    attempt: Option<String>,
}
pub(super) fn search(store: &Store, args: VectorQuery, worker: bool) -> Result<Value> {
    let lease = if worker {
        Some((
            args.job.context("worker job is required")?,
            args.attempt.context("worker attempt is required")?,
        ))
    } else {
        anyhow::ensure!(
            args.job.is_none() && args.attempt.is_none(),
            "lease parameters require a worker credential"
        );
        None
    };
    Ok(
        json!({"results": store.search_artifact_vectors(args.space, args.vector, args.limit, args.filter, lease)?}),
    )
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum WorkerRequest {
    Claim {
        recipes: Vec<String>,
    },
    Heartbeat {
        id: String,
        attempt: String,
        #[serde(default)]
        progress: Value,
        failure: Option<String>,
    },
    Complete {
        id: String,
        attempt: String,
        outputs: std::collections::BTreeMap<String, String>,
        #[serde(default)]
        assets: std::collections::BTreeMap<String, crate::store::artifacts::Asset>,
    },
}
pub(super) async fn worker(
    State(state): State<AppState>,
    Json(request): Json<WorkerRequest>,
) -> Response {
    match run_blocking(move || match request {
        WorkerRequest::Claim { recipes } => {
            Ok(serde_json::to_value(state.store.claim_job(&recipes)?)?)
        }
        WorkerRequest::Heartbeat {
            id,
            attempt,
            progress,
            failure,
        } => {
            state.store.heartbeat(&id, &attempt, progress, failure)?;
            Ok(Value::Null)
        }
        WorkerRequest::Complete {
            id,
            attempt,
            outputs,
            assets,
        } => {
            state.store.complete_job(
                &id,
                Completion {
                    attempt,
                    outputs,
                    assets,
                },
            )?;
            Ok(Value::Null)
        }
    })
    .await
    {
        Ok(value) => Json(value).into_response(),
        Err(error) => problem::Problem::from_error(&error).into_response(),
    }
}

pub(super) fn extend_schema(tool: &mut Value) {
    fn object(properties: Value, required: &[&str]) -> Value {
        json!({"type":"object", "properties":properties, "required":required, "additionalProperties":false})
    }
    let base = tool["inputSchema"].clone();
    let variants = match tool["name"].as_str().unwrap() {
        "get" => vec![
            base,
            object(
                json!({"resource":{"enum":["derivations","jobs"]}}),
                &["resource"],
            ),
        ],
        "edit" => {
            let definition = object(
                json!({
                    "source":{"type":"string"}, "recipe":{"type":"string"},
                    "fields":{"type":"array","items":{"type":"string"}},
                    "inputs":{"type":"array","items":{"type":"string"}},
                    "outputs":{"type":"array","items":{"type":"string"}},
                    "reference_namespace":{"type":["string","null"]}
                }),
                &["source", "recipe", "fields", "inputs", "outputs"],
            );
            vec![
                base,
                object(
                    json!({"resource":{"const":"derivations"},"action":{"const":"create"},"definition":definition}),
                    &["resource", "action", "definition"],
                ),
                object(
                    json!({"resource":{"const":"derivations"},"action":{"const":"update"},"id":{"type":"string"},"inputs":{"type":"array","items":{"type":"string"}}}),
                    &["resource", "action", "id", "inputs"],
                ),
                object(
                    json!({"resource":{"const":"derivations"},"action":{"const":"delete"},"id":{"type":"string"}}),
                    &["resource", "action", "id"],
                ),
                object(
                    json!({"resource":{"const":"jobs"},"action":{"const":"retry"},"id":{"type":"string"}}),
                    &["resource", "action", "id"],
                ),
            ]
        }
        "search" => vec![
            base,
            object(
                json!({
                    "space": object(json!({"namespace":{"type":"string"},"recipe":{"type":"string"},"dimensions":{"type":"integer","minimum":1}}), &["namespace","recipe","dimensions"]),
                    "vector":{"type":"array","items":{"type":"number"}},"limit":{"type":"integer","minimum":0},
                    "filter":{"type":"object"},"job":{"type":"string"},"attempt":{"type":"string"}
                }),
                &["space", "vector", "limit"],
            ),
        ],
        _ => unreachable!(),
    };
    tool["inputSchema"] = json!({"type":"object", "oneOf":variants});
    let extra = match tool["name"].as_str().unwrap() {
        "get" => {
            " Use resource=derivations or resource=jobs for processing definitions and status."
        }
        "edit" => {
            " Resource edits manage derivations (create/update/delete) or retry jobs; these are separate from document batches."
        }
        _ => {
            " Supply space/vector/limit for artifact vector search instead of query/variants. Workers must supply job and attempt, restricting results to frozen references."
        }
    };
    tool["description"] = json!(format!("{}{extra}", tool["description"].as_str().unwrap()));
}
