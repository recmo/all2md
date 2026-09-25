/// <reference lib="webworker" />
import init, { validate, build_snapshot } from './wasm/validator';
let ready: ReturnType<typeof init> | undefined;
self.onmessage = async ({ data }) => {
  try {
    ready ??= init({ module_or_path: data.wasmUrl });
    await ready;
    self.postMessage({
      id: data.id,
      result: JSON.parse((data.operation === 'build_snapshot' ? build_snapshot : validate)(JSON.stringify(data.input)))
    });
  } catch (error) {
    self.postMessage({ id: data.id, error: String(error) });
  }
};
