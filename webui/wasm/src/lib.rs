//! Browser-owned validation orchestration and Starlark app bindings.
mod app_documents;
mod apps;
mod client;
mod client_validation;
mod snapshot;
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub fn validate(input: &str) -> Result<String, JsValue> {
    client::validate(input).map_err(|error| JsValue::from_str(&error.to_string()))
}

#[wasm_bindgen]
pub fn evaluate_app(input: &str) -> Result<String, JsValue> {
    client::evaluate_app(input).map_err(|error| JsValue::from_str(&error.to_string()))
}

#[wasm_bindgen]
pub fn build_snapshot(input: &str) -> Result<String, JsValue> {
    client::build_snapshot(input).map_err(|error| JsValue::from_str(&error.to_string()))
}
