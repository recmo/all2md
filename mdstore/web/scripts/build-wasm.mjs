import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const wasm = fileURLToPath(new URL('../../wasm/', import.meta.url));
const out = fileURLToPath(new URL('../src/lib/wasm/', import.meta.url));
execFileSync(
  'cargo',
  [
    'build',
    '--locked',
    '--manifest-path',
    wasm + 'Cargo.toml',
    '--target',
    'wasm32-unknown-unknown',
    '--release'
  ],
  { stdio: 'inherit', env: { ...process.env, RUSTC_WRAPPER: '' } }
);
execFileSync(
  process.env.WASM_BINDGEN || 'wasm-bindgen',
  [
    wasm + 'target/wasm32-unknown-unknown/release/mdstore_validation_wasm.wasm',
    '--target',
    'web',
    '--omit-default-module-path',
    '--remove-name-section',
    '--out-dir',
    out,
    '--out-name',
    'validator'
  ],
  { stdio: 'inherit' }
);
