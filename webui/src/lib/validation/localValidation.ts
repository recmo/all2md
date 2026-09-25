import wasmUrl from '../wasm/validator_bg.wasm?url';
import type { EditRequest, ValidationFinding } from '../workspace/api';
export type ValidationSnapshot = {
  version: number;
  revision: string;
  files: Record<string, string>;
  documents: Record<string, { hash: string; parsed: unknown }>;
  edges: unknown[];
};
export type LocalValidationResult = {
  valid: boolean;
  findings: ValidationFinding[];
  needs: string[];
  server_required: string | null;
  restart_required: boolean;
};
let worker: Worker | undefined;
let nextId = 0;
const pending = new Map<
  number,
  {
    resolve: (result: unknown) => void;
    reject: (error: Error) => void;
    timer: ReturnType<typeof setTimeout>;
  }
>();
export function disposeValidation() {
  worker?.terminate();
  worker = undefined;
  for (const request of pending.values()) {
    clearTimeout(request.timer);
    request.reject(
      new Error('Local validation worker stopped. Revalidate to retry.')
    );
  }
  pending.clear();
}
export function validateLocally(
  snapshot: ValidationSnapshot,
  request: EditRequest,
  sources: Record<string, string>
): Promise<LocalValidationResult> {
  return runWorker<LocalValidationResult>('validate', {
    snapshot,
    edits: request.edits,
    sources
  });
}
export function buildSnapshot(
  revision: string,
  sources: Record<string, string>
) {
  return runWorker<ValidationSnapshot>('build_snapshot', { revision, sources });
}
function runWorker<T>(operation: string, input: unknown): Promise<T> {
  if (!worker) {
    worker = new Worker(new URL('./validation.worker.ts', import.meta.url), {
      type: 'module'
    });
    worker.onmessage = ({ data }) => {
      const request = pending.get(data.id);
      if (!request) return;
      clearTimeout(request.timer);
      pending.delete(data.id);
      if (data.error) request.reject(new Error(data.error));
      else request.resolve(data.result);
    };
    worker.onerror = () => disposeValidation();
  }
  const id = ++nextId;
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => disposeValidation(), 15000);
    pending.set(id, {
      resolve: (result) => resolve(result as T),
      reject,
      timer
    });
    try {
      worker!.postMessage({
        id,
        wasmUrl,
        operation,
        input
      });
    } catch (error) {
      clearTimeout(timer);
      pending.delete(id);
      reject(error);
    }
  });
}
