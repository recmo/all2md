//! Browser build of the same validation modules used by the daemon.
#![allow(dead_code)]
#[path = "../../src/config.rs"]
mod config;
#[path = "../../src/markdown.rs"]
mod markdown;
#[path = "../../src/markdown_lint.rs"]
mod markdown_lint;
#[path = "../../src/structure.rs"]
mod structure;
#[path = "../../src/template.rs"]
mod template;
#[path = "../../src/template_script.rs"]
mod template_script;

pub use config::*;
pub use markdown::Finding;
use wasm_bindgen::prelude::*;

#[path = "../../src/client_validation.rs"]
mod client_validation;

#[wasm_bindgen]
pub fn validate(input: &str) -> Result<String, JsValue> {
    let input = serde_json::from_str(input).map_err(|error| JsValue::from_str(&format!("Invalid validation input: {error}")))?;
    serde_json::to_string(&client_validation::validate(input)).map_err(|error| JsValue::from_str(&error.to_string()))
}

#[path = "../../src/apps.rs"]
mod apps;
#[wasm_bindgen]
pub fn evaluate_app(input: &str) -> Result<String, JsValue> {
    let input = serde_json::from_str(input).map_err(|e| JsValue::from_str(&format!("Invalid app input: {e}")))?;
    let result = apps::evaluate(input).map_err(|e| JsValue::from_str(&e.to_string()))?;
    serde_json::to_string(&result).map_err(|e| JsValue::from_str(&e.to_string()))
}
