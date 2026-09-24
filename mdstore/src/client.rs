//! Portable entry points shared by the daemon library and browser bindings.

use anyhow::{Result, anyhow};

/// Validates a JSON edit request against a source-light validation snapshot.
/// Findings and missing source requirements are returned as JSON.
pub fn validate(input: &str) -> Result<String> {
    let input = serde_json::from_str(input)
        .map_err(|error| anyhow!("Invalid validation input: {error}"))?;
    Ok(serde_json::to_string(&crate::client_validation::validate(
        input,
    ))?)
}

/// Evaluates a Starlark app definition or action over a JSON document list.
pub fn evaluate_app(input: &str) -> Result<String> {
    let input =
        serde_json::from_str(input).map_err(|error| anyhow!("Invalid app input: {error}"))?;
    Ok(serde_json::to_string(&crate::apps::evaluate(input)?)?)
}
