//! Browser bindings for mdstore's portable library API.
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub fn validate(input: &str) -> Result<String, JsValue> {
    mdstore::client::validate(input).map_err(|error| JsValue::from_str(&error.to_string()))
}

#[wasm_bindgen]
pub fn evaluate_app(input: &str) -> Result<String, JsValue> {
    mdstore::client::evaluate_app(input).map_err(|error| JsValue::from_str(&error.to_string()))
}
