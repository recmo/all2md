import MarkdownIt from 'markdown-it';
import { parseDocument, Document } from 'yaml';
import type { Cache } from './cache';
import wasmUrl from './wasm/validator_bg.wasm?url';
const markdown = new MarkdownIt();
export type AppDocument = { path: string; title: string; template: string | null; frontmatter: Record<string, unknown>; text: string | null; sourceHash?: string };
export type AppView = { kind: 'table' | 'kanban' | 'gantt'; name: string; collection: string; bindings: { columns?: string[]; group?: string; on_move?: string; start?: string; end?: string; dependencies?: string } };
export type AppResult = { collections: Record<string, string[]>; views: AppView[] };
export type AppEdit = { path: string; fields: Record<string, unknown>; timeline?: string | null; pointers?: Record<string, unknown>; expected?: Record<string, unknown> };
export function isApp(text: string): boolean {
  const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(text);
  if (!match) return false;
  try {
    const yaml = parseDocument(match[1]);
    return !yaml.errors.length && yaml.toJS({ maxAliasCount: 100 })?.mdstore === 'app';
  } catch { return false; }
}
export function appDocuments(cache: Cache) {
  const documents: AppDocument[] = [], missing: string[] = [], errors: string[] = [];
  const paths = [...new Set([...cache.paths, ...Object.keys(cache.drafts)])].sort();
  for (const path of paths) {
    if (!path.endsWith('.md') || /(^|\/)(template)\.md$/.test(path) || path in (cache.deletions || {})) continue;
    const draft = cache.drafts[path];
    const baseline = cache.validationSnapshot?.documents[path]?.parsed as { frontmatter: Record<string, unknown>; headings: { level: number; text: string }[] } | undefined;
    const page = draft || cache.pages[path];
    let frontmatter = baseline?.frontmatter, title = baseline?.headings.find(h => h.level === 1)?.text;
    try {
      if (draft || (!baseline && page)) {
        const text = page!.text;
        const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(text);
        if (text.startsWith('---\n') && !match) throw Error('Unclosed frontmatter');
        const yaml = parseDocument(match?.[1] || '');
        if (yaml.errors.length) throw yaml.errors[0];
        frontmatter = yaml.toJS({ maxAliasCount: 100 }) ?? {};
        if (typeof frontmatter !== 'object' || Array.isArray(frontmatter)) throw Error('Frontmatter must be a mapping');
        const tokens = markdown.parse(match ? text.slice(match[0].length) : text, {});
        const i = tokens.findIndex(t => t.type === 'heading_open' && t.tag === 'h1');
        title = i < 0 ? undefined : tokens[i + 1].children?.filter(t => ['text', 'code_inline'].includes(t.type)).map(t => t.content).join('');
      }
      if (frontmatter?.mdstore === 'app') continue;
      if (!frontmatter) { missing.push(path); continue; }
      const templates = paths.filter(p => /(^|\/)template\.md$/.test(p) && !(p in (cache.deletions || {})) && path.startsWith(p.slice(0, -'template.md'.length))).sort((a,b) => b.length-a.length);
      documents.push({ path, title: title || path.split('/').at(-1)!, template: templates[0] ? '/' + templates[0] : null, frontmatter, text: page?.text ?? null, sourceHash: draft ? undefined : cache.validationSnapshot?.documents[path]?.hash });
    } catch (error) { errors.push(`${path}: ${String(error)}`); }
  }
  return { documents, missing, errors, cached: !!cache.validationSnapshot };
}
export function field(doc: AppDocument, binding: string | undefined): unknown {
  if (binding === 'title') return doc.title;
  if (binding === 'path') return doc.path;
  if (!binding?.startsWith('/')) return undefined;
  let value: unknown = doc.frontmatter;
  for (const part of binding.slice(1).split('/')) {
    if (!value || typeof value !== 'object') return undefined;
    value = (value as Record<string, unknown>)[part.replace(/~1/g, '/').replace(/~0/g, '~')];
  }
  return value;
}
export function display(value: unknown): string {
  return value === undefined || value === null ? '—' : Array.isArray(value) ? value.map(display).join(', ') : typeof value === 'object' ? JSON.stringify(value) : String(value);
}
export async function evaluateApp(path: string, source: string, documents: AppDocument[], action?: string, event?: unknown): Promise<AppResult & { edits?: AppEdit[] }> {
  const records = await Promise.all(documents.map(async ({ sourceHash, ...doc }) => {
    if (sourceHash && doc.text !== null) {
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(doc.text));
      const hash = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2,'0')).join('');
      if (hash !== sourceHash) doc.text = null;
    }
    return doc;
  }));
  const worker = new Worker(new URL('./apps.worker.ts', import.meta.url), { type: 'module' });
  return new Promise((resolve, reject) => {
    const finish = () => { clearTimeout(timer); worker.terminate(); };
    const timer = setTimeout(() => { finish(); reject(Error('App evaluation timed out')); }, 15000);
    worker.onmessage = ({ data }) => {
      finish();
      if (data.error) { reject(Error(data.error)); return; }
      if (!action) {
        const result = data.result as AppResult;
        const known = new Set(documents.map(d => d.path));
        if (!result || !Array.isArray(result.views) || !result.collections ||
            Object.values(result.collections).some(paths => !Array.isArray(paths) || paths.some(path => !known.has(path))) ||
            result.views.some(view => !view || !['table','kanban','gantt'].includes(view.kind) || typeof view.name !== 'string' || !view.bindings || !(view.collection in result.collections))) {
          reject(Error('Invalid app view definitions or collection references')); return;
        }
      }
      resolve(data.result);
    };
    worker.onerror = () => { finish(); reject(Error('App worker failed')); };
    worker.postMessage({ wasmUrl, input: { path, source, documents: records, action, event } });
  });
}
export function applyAppEdit(text: string, edit: AppEdit): string {
  if (!edit.fields || typeof edit.fields !== 'object' || Array.isArray(edit.fields)) throw Error('Action fields must be a mapping');
  const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(text);
  if (/^---\r?\n/.test(text) && !match) throw Error('Cannot edit malformed frontmatter');
  const yaml = match ? parseDocument(match[1]) : new Document({});
  if (yaml.errors.length) throw yaml.errors[0];
  const pointerPath = (pointer: string) => {
    if (!pointer.startsWith('/') || /~(?![01])/.test(pointer)) throw Error('Invalid frontmatter pointer');
    return pointer.slice(1).split('/').map(key => key.replace(/~1/g, '/').replace(/~0/g, '~'));
  };
  for (const [pointer, value] of Object.entries(edit.expected || {})) {
    if (JSON.stringify(yaml.getIn(pointerPath(pointer))) !== JSON.stringify(value)) throw Error('Task dates changed. Refresh the view and try again.');
  }
  for (const [key, value] of Object.entries(edit.fields)) yaml.set(key, value);
  for (const [pointer, value] of Object.entries(edit.pointers || {})) yaml.setIn(pointerPath(pointer), value);
  let body = match ? text.slice(match[0].length) : text;
  if (edit.timeline !== undefined && edit.timeline !== null) {
    if (typeof edit.timeline !== 'string' || /[\r\n]/.test(edit.timeline)) throw Error('Timeline entries must be single-line strings');
    const tokens = markdown.parse(body, {});
    const timeline = tokens.find(t => t.type === 'inline' && t.content === 'Timeline' && tokens[tokens.indexOf(t)-1]?.type === 'heading_open' && tokens[tokens.indexOf(t)-1]?.tag === 'h2');
    if (!timeline?.map) throw Error('The document has no H2 Timeline section');
    const next = tokens.find(t => t.type === 'heading_open' && ['h1','h2'].includes(t.tag) && t.map && t.map[0] > timeline.map![0]);
    const lines = body.split('\n');
    const at = next?.map?.[0] ?? lines.length;
    const before = lines.slice(0, at).join('\n').trimEnd();
    const after = lines.slice(at).join('\n');
    body = before + '\n- ' + edit.timeline + '\n' + (after ? '\n' + after : '');
  }
  return '---\n' + yaml.toString({ flowCollectionPadding: false }) + '---\n' + body;
}
