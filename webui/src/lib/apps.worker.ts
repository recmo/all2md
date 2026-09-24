/// <reference lib="webworker" />
import init, { evaluate_app } from './wasm/validator';
self.onmessage = async ({ data }) => {
  try {
    await init({ module_or_path: data.wasmUrl });
    self.postMessage({ result: JSON.parse(evaluate_app(JSON.stringify(data.input))) });
  } catch (error) { self.postMessage({ error: String(error) }); }
};
