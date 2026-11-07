//! Worker-only lease operations, independent of MCP transport.
use super::*;
use crate::store::artifacts::Completion;

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum WorkerRequest {
    Retry {
        id: String,
    },
    Search {
        id: String,
        attempt: String,
        query: VectorQuery,
    },
    Claim {
        recipes: std::collections::BTreeMap<String, usize>,
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
pub(super) async fn handle(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<WorkerRequest>,
) -> Response {
    let is_worker = state.worker_token.as_deref().is_some_and(|token| {
        headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            == Some(token)
    });
    if !is_worker && !matches!(request, WorkerRequest::Retry { .. }) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let result = run_blocking(move || match request {
        WorkerRequest::Retry { id } => {
            if id.starts_with("retrieval-") {
                state.store.with_retrieval(|queue, _| queue.retry(&id))?;
                state.store.notify_reindex();
            } else {
                state.store.retry_job(&id)?;
            }
            Ok(Value::Null)
        }
        WorkerRequest::Search { id, attempt, query } => {
            let results = state.store.search_reference_vectors(
                query.space,
                query.vector,
                query.limit,
                query.filter,
                (id, attempt),
            )?;
            Ok(json!({"results": results}))
        }
        WorkerRequest::Claim { recipes } => {
            anyhow::ensure!(
                !recipes.is_empty()
                    && recipes.len() <= 32
                    && recipes.values().all(|count| (1..=256).contains(count)),
                "claim requires recipe counts between 1 and 256"
            );
            let mut assignments = state
                .store
                .with_retrieval(|queue, texts| queue.claim(&recipes, texts, false))?;
            if assignments.is_empty() {
                let names: Vec<_> = recipes.keys().cloned().collect();
                if let Some(first) = state.store.claim_job(&names)? {
                    let recipe = first.job.recipe.clone();
                    assignments.push(serde_json::to_value(first)?);
                    for _ in 1..recipes[&recipe] {
                        let Some(next) = state.store.claim_job(std::slice::from_ref(&recipe))?
                        else {
                            break;
                        };
                        assignments.push(serde_json::to_value(next)?);
                    }
                } else {
                    assignments = state
                        .store
                        .with_retrieval(|queue, texts| queue.claim(&recipes, texts, true))?;
                }
            }
            Ok(serde_json::to_value(assignments)?)
        }
        WorkerRequest::Heartbeat {
            id,
            attempt,
            progress,
            failure,
        } => {
            if id.starts_with("retrieval-") {
                state.store.with_retrieval(|queue, texts| {
                    queue.heartbeat(&id, &attempt, failure, texts)
                })?;
            } else {
                state.store.heartbeat(&id, &attempt, progress, failure)?;
            }
            Ok(Value::Null)
        }
        WorkerRequest::Complete {
            id,
            attempt,
            outputs,
            assets,
        } => {
            if id.starts_with("retrieval-") {
                anyhow::ensure!(assets.is_empty(), "retrieval results must be inline JSON");
                state.store.with_retrieval(|queue, texts| {
                    queue.complete(&id, &attempt, &outputs, texts)
                })?;
                state.store.notify_reindex();
            } else {
                state.store.complete_job(
                    &id,
                    Completion {
                        attempt,
                        outputs,
                        assets,
                    },
                )?;
            }
            Ok(Value::Null)
        }
    })
    .await;
    match result {
        Ok(value) => Json(value).into_response(),
        Err(error) => problem::Problem::from_error(&error).into_response(),
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::provider::{EMBED_RECIPE, RERANK_RECIPE};
    use axum::body::Body;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    async fn request(app: &Router, value: Value) -> (StatusCode, Value) {
        let response = app
            .clone()
            .oneshot(
                HttpRequest::builder()
                    .method("POST")
                    .uri("/worker")
                    .header("authorization", "Bearer worker")
                    .header("content-type", "application/json")
                    .body(Body::from(value.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = response.status();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&bytes).unwrap())
    }

    async fn claim_one(app: &Router, recipes: &[&str]) -> Value {
        let recipes: serde_json::Map<_, _> = recipes
            .iter()
            .map(|recipe| ((*recipe).to_owned(), json!(1)))
            .collect();
        let (status, assignments) = request(app, json!({"op":"claim", "recipes": recipes})).await;
        assert_eq!(status, StatusCode::OK);
        assignments[0].clone()
    }

    fn result(assignment: &Value) -> Value {
        let parameters = &assignment["parameters"]["ml"];
        let texts = parameters["texts"].as_array().unwrap();
        let mut body = if assignment["job"]["recipe"] == EMBED_RECIPE {
            let mut vector = vec![0.0; 2560];
            vector[0] = 1.0;
            json!({"model":"zeroentropy/zembed-1-embedding", "revision":"cf13c81f3274394053d166740294f7eea4586f7a",
                "space":{"recipe": EMBED_RECIPE, "namespace":"search", "dimensions":2560},
                "records": texts.iter().map(|text| json!({"id":text["id"], "vector":vector,
                    "metadata":{"text":text["text"], "input_type":parameters["input_type"]}})).collect::<Vec<_>>()})
        } else {
            json!({"model":"zeroentropy/zerank-2-reranker", "revision":"5eae30d5ee3c6b2df2ef6d723bde45172d761c4c",
                "query":parameters["query"], "results": texts.iter().enumerate().take(parameters["top_n"].as_u64().unwrap() as usize)
                    .map(|(index,text)| json!({"index":index,"id":text["id"],"relevance_score":1.0})).collect::<Vec<_>>()})
        };
        body["provenance"] = json!({"recipe":assignment["job"]["recipe"], "fingerprint":assignment["job"]["fingerprint"]});
        json!({"op":"complete", "id":assignment["job"]["id"], "attempt":assignment["job"]["attempt"],
            "outputs": {crate::retrieval_jobs::OUTPUT:body.to_string()}})
    }

    #[tokio::test]
    async fn retrieval_workers_index_search_restart_and_fence_changed_chunks() {
        let (dir, fixture) = crate::store::artifacts::tests::fixture();
        drop(fixture);
        let fixture = Store::open(dir.path()).unwrap();
        fixture
            .apply_edits(&ApplyEditsRequest {
                summary: "Add searchable body".into(),
                edits: vec![crate::EditOperation::Append {
                    path: "recording.md".into(),
                    anchor: None,
                    content: "Recording content to search.\n\n## Details\n\nAdditional recording details.\n".into(),
                }],
                expected_commit: None,
                moves: vec![],
            })
            .unwrap();
        fixture
            .create_derivation(crate::DerivationConfig {
                source: "recording.md".into(),
                recipe: "fixture-v1".into(),
                fields: vec!["hotwords".into()],
                inputs: vec![],
                outputs: vec!["transcript.md".into()],
                reference_namespace: None,
            })
            .unwrap();
        drop(fixture);
        let store = Store::open(dir.path()).unwrap();
        let app = application(AppState {
            store: store.clone(),
            worker_token: Some("worker".into()),
        });
        let recipes = &[EMBED_RECIPE, RERANK_RECIPE];
        assert!(store.reindex_missing().await.is_err());
        assert!(store.status().unwrap().vectors_total > 0);
        assert_eq!(store.status().unwrap().vectors_ready, 0);
        assert!(
            store
                .jobs()
                .unwrap()
                .iter()
                .all(|job| job.state.status == "queued")
        );
        // With the MacBook absent, indexing stays queued and search immediately
        // returns lexical results without creating interactive inference jobs.
        let queued = store.jobs().unwrap().len();
        let response = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            store.search("Recording", &[]),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(!response.results.is_empty());
        assert_eq!(response.degraded.len(), 2);
        assert_eq!(store.jobs().unwrap().len(), queued);
        // Even an old queue entry from a long offline interval must be claimable.
        let retrieval = dir.path().join(".git/mdstore/retrieval");
        for entry in std::fs::read_dir(&retrieval).unwrap() {
            let path = entry.unwrap().path();
            if path
                .extension()
                .is_some_and(|extension| extension == "json")
            {
                let mut job: Value =
                    serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
                job["deadline"] = json!(0);
                std::fs::write(path, serde_json::to_vec(&job).unwrap()).unwrap();
            }
        }
        // Interactive queries precede declared jobs, which precede background indexing.
        claim_one(&app, &[RERANK_RECIPE]).await;
        let interactive = store.retrieval_queue().submit(RERANK_RECIPE,
            json!({"texts":[{"id":"0","text":"Recording"}],"query":"Which recording?","top_n":1}), false, 30).unwrap();
        let all_recipes = &[EMBED_RECIPE, RERANK_RECIPE, "fixture-v1"];
        let second = store.retrieval_queue().submit(RERANK_RECIPE,
            json!({"texts":[{"id":"0","text":"Recording"}],"query":"Another recording?","top_n":1}), false, 30).unwrap();
        let (status, batch) = request(
            &app,
            json!({"op":"claim","recipes":{
            EMBED_RECIPE: 2, RERANK_RECIPE: 2, "fixture-v1": 2}}),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let batch = batch.as_array().unwrap();
        assert_eq!(batch.len(), 2);
        let claimed: std::collections::HashSet<_> = batch
            .iter()
            .map(|job| job["job"]["id"].as_str().unwrap())
            .collect();
        assert_eq!(claimed, [interactive.as_str(), second.as_str()].into());
        assert_ne!(batch[0]["job"]["attempt"], batch[1]["job"]["attempt"]);
        for query in batch {
            assert_eq!(query["job"]["recipe"], RERANK_RECIPE);
            assert_eq!(request(&app, result(query)).await.0, StatusCode::OK);
        }
        let declared = claim_one(&app, all_recipes).await;
        assert_eq!(declared["job"]["recipe"], "fixture-v1");
        let (_, document_batch) =
            request(&app, json!({"op":"claim", "recipes":{EMBED_RECIPE: 2}})).await;
        let document_batch = document_batch.as_array().unwrap();
        assert_eq!(document_batch.len(), 2);
        for job in document_batch {
            assert_eq!(
                job["parameters"]["ml"]["texts"].as_array().unwrap().len(),
                1
            );
        }
        let assignment = document_batch[0].clone();
        let second_chunk = document_batch[1].clone();
        assert_eq!(assignment["parameters"]["ml"]["input_type"], "document");
        assert_eq!(
            assignment["parameters"]["ml"]["texts"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        let completion = result(&assignment);
        // A restarted daemon still accepts a valid outstanding lease.
        drop(app);
        drop(store);
        let store = Store::open(dir.path()).unwrap();
        let app = application(AppState {
            store: store.clone(),
            worker_token: Some("worker".into()),
        });
        let mut wrong = completion.clone();
        wrong["attempt"] = json!("superseded");
        assert_eq!(
            request(&app, wrong).await.0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
        let mut wrong = completion.clone();
        let mut body: Value = serde_json::from_str(
            wrong["outputs"][crate::retrieval_jobs::OUTPUT]
                .as_str()
                .unwrap(),
        )
        .unwrap();
        body["revision"] = json!("wrong-model-revision");
        wrong["outputs"][crate::retrieval_jobs::OUTPUT] = json!(body.to_string());
        assert_eq!(
            request(&app, wrong).await.0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
        assert_eq!(request(&app, completion.clone()).await.0, StatusCode::OK);
        assert_eq!(request(&app, completion).await.0, StatusCode::OK);
        assert!(store.reindex_missing().await.is_err());
        let partial = store.status().unwrap();
        assert!(partial.vectors_ready > 0 && partial.vectors_ready < partial.vectors_total);
        assert_eq!(request(&app, result(&second_chunk)).await.0, StatusCode::OK);
        store.reindex_missing().await.unwrap();
        assert_eq!(
            store.status().unwrap().vectors_ready,
            store.status().unwrap().vectors_total
        );
        assert!(dir.path().join("recording.mdstore").exists());
        // A forced rebuild must remain pending until fresh results replace the old sidecar.
        assert!(store.reindex().await.is_err());
        assert!(store.reindex_missing().await.is_err());
        for _ in 0..store.status().unwrap().vectors_total {
            let assignment = claim_one(&app, recipes).await;
            let mut completion = result(&assignment);
            let mut body: Value = serde_json::from_str(
                completion["outputs"][crate::retrieval_jobs::OUTPUT]
                    .as_str()
                    .unwrap(),
            )
            .unwrap();
            for record in body["records"].as_array_mut().unwrap() {
                record["vector"][0] = json!(0.0);
                record["vector"][1] = json!(1.0);
            }
            completion["outputs"][crate::retrieval_jobs::OUTPUT] = json!(body.to_string());
            assert_eq!(request(&app, completion).await.0, StatusCode::OK);
        }
        store.reindex_missing().await.unwrap();
        let rebuilt = crate::sidecar::read(&dir.path().join("recording.mdstore")).unwrap();
        assert!(
            rebuilt
                .chunks
                .iter()
                .all(|chunk| chunk.vector_le_f32[..4] == 0.0_f32.to_le_bytes()
                    && chunk.vector_le_f32[4..8] == 1.0_f32.to_le_bytes())
        );
        // Both query embeddings and reranking are claimed from the same worker endpoint.
        let searching = store.clone();
        let search = tokio::spawn(async move { searching.search("Recording", &[]).await.unwrap() });
        let mut modes = Vec::new();
        for _ in 0..10 {
            // Match the real worker's idle polling interval.
            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            let assignment = claim_one(&app, recipes).await;
            if !assignment.is_null() {
                modes.push((
                    assignment["job"]["recipe"].clone(),
                    assignment["parameters"]["ml"]["input_type"].clone(),
                ));
                assert_eq!(request(&app, result(&assignment)).await.0, StatusCode::OK);
            }
            if search.is_finished() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        assert!(search.is_finished());
        let response = search.await.unwrap();
        assert!(response.degraded.is_empty(), "{:?}", response.degraded);
        assert!(!response.results.is_empty());
        assert_eq!(
            modes,
            vec![
                (json!(EMBED_RECIPE), json!("query")),
                (json!(RERANK_RECIPE), Value::Null)
            ]
        );
        // Recent results remain usable when the worker goes away. An uncached
        // query falls back immediately and does not enqueue stale reranking.
        let queued = store.jobs().unwrap().len();
        std::fs::write(
            retrieval.join("workers"),
            serde_json::to_vec(&std::collections::BTreeMap::from([
                (EMBED_RECIPE, 0_u64),
                (RERANK_RECIPE, 0_u64),
            ]))
            .unwrap(),
        )
        .unwrap();
        let cached = store.search("Recording", &[]).await.unwrap();
        assert!(cached.degraded.is_empty(), "{:?}", cached.degraded);
        let uncached = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            store.search("Recording uncached", &[]),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(!uncached.results.is_empty());
        assert_eq!(uncached.degraded.len(), 2);
        assert_eq!(store.jobs().unwrap().len(), queued);
        // A changed chunk cannot accept a previously claimed result.
        assert!(store.reindex().await.is_err());
        let stale = claim_one(&app, recipes).await;
        let page = store.file_page("recording.md").unwrap();
        let changed = app
            .clone()
            .oneshot(
                HttpRequest::builder()
                    .method("PUT")
                    .uri("/recording.md")
                    .header("if-match", format!("\"{}\"", page.hash.unwrap()))
                    .body(Body::from("# Changed document\n\nFresh searchable text.\n"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert!(changed.status().is_success(), "{}", changed.status());
        assert_eq!(
            request(&app, result(&stale)).await.0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
        assert!(store.reindex_missing().await.is_err());
        let fresh = claim_one(&app, recipes).await;
        assert_ne!(fresh["job"]["id"], stale["job"]["id"]);
        assert_eq!(request(&app, result(&fresh)).await.0, StatusCode::OK);
        store.reindex_missing().await.unwrap();
        assert_eq!(
            store.status().unwrap().vectors_ready,
            store.status().unwrap().vectors_total
        );
        drop(app);
        drop(store);
        let reopened = Store::open(dir.path()).unwrap();
        assert_eq!(
            reopened.status().unwrap().vectors_ready,
            reopened.status().unwrap().vectors_total
        );
        // Concurrent searches share jobs and wait beyond the old three-second cap.
        let app = application(AppState {
            store: reopened.clone(),
            worker_token: Some("worker".into()),
        });
        claim_one(&app, recipes).await;
        let queued = reopened.jobs().unwrap().len();
        let searching = reopened.clone();
        let searches = tokio::spawn(async move {
            tokio::join!(
                searching.search("Fresh unavailable", &[]),
                searching.search("Fresh unavailable", &[])
            )
        });
        let mut completed = Vec::new();
        for _ in 0..2 {
            tokio::time::sleep(std::time::Duration::from_secs(4)).await;
            assert!(!searches.is_finished());
            assert_eq!(reopened.jobs().unwrap().len(), queued + 1);
            let assignment = claim_one(&app, recipes).await;
            assert!(!assignment.is_null());
            assert_eq!(request(&app, result(&assignment)).await.0, StatusCode::OK);
            completed.push(assignment);
        }
        let (first, second) = tokio::time::timeout(std::time::Duration::from_secs(2), searches)
            .await
            .unwrap()
            .unwrap();
        for response in [first.unwrap(), second.unwrap()] {
            assert!(!response.results.is_empty());
            assert!(response.degraded.is_empty(), "{:?}", response.degraded);
        }
        assert_eq!(reopened.jobs().unwrap().len(), queued);
        std::fs::write(retrieval.join("workers"), b"{}").unwrap();
        let cached = reopened.search("Fresh unavailable", &[]).await.unwrap();
        assert!(cached.degraded.is_empty(), "{:?}", cached.degraded);
        // Replacing an expired result keeps its key but rejects the old attempt.
        for assignment in completed {
            let id = assignment["job"]["id"].as_str().unwrap();
            let path = retrieval.join(format!("{id}.json"));
            let mut job: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
            job["deadline"] = json!(0);
            std::fs::write(&path, serde_json::to_vec(&job).unwrap()).unwrap();
            let recipe = assignment["job"]["recipe"].as_str().unwrap();
            claim_one(&app, &[recipe]).await;
            let replacement = reopened
                .retrieval_queue()
                .submit(recipe, assignment["parameters"]["ml"].clone(), false, 300)
                .unwrap();
            assert_eq!(replacement, id);
            assert_eq!(
                request(&app, result(&assignment)).await.0,
                StatusCode::UNPROCESSABLE_ENTITY
            );
            let replacement = claim_one(&app, &[recipe]).await;
            assert_ne!(replacement["job"]["attempt"], assignment["job"]["attempt"]);
            assert_eq!(request(&app, result(&replacement)).await.0, StatusCode::OK);
        }
        // A lower configured deadline still bounds a stalled inference stage.
        let provider = crate::provider::WorkerProvider::new(
            crate::config::ProviderConfig {
                request_timeout_seconds: 1,
            },
            &crate::git::git_dir(dir.path()).unwrap(),
        );
        let started = tokio::time::Instant::now();
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            crate::provider::RetrievalProvider::embed(
                &provider,
                crate::provider::InputType::Query,
                &["A stalled query".into()],
            ),
        )
        .await
        .unwrap();
        assert!(result.unwrap_err().to_string().contains("result pending"));
        assert!(started.elapsed() >= std::time::Duration::from_secs(1));
    }
}
