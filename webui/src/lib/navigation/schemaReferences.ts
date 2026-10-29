import { parseDocument } from 'yaml';
import type { Workspace } from '../workspace/state';
import { readonly } from '../workspace/api';

export type SchemaReference = {
  target: string;
  raw: string;
  range: { start: number; end: number } | null;
  pointer: string | null;
};
export type SchemaReferences = Record<string, SchemaReference[]>;

// Locations and targets come from the same Rust schema resolver used to validate
// documents. Preserve YAML comments and wiki delimiters, labels, and fragments.
export function rewriteSchemaReferences(
  workspace: Workspace,
  references: SchemaReferences,
  moves: Map<string, string>,
  allowTemplates: boolean,
  allowConfig: boolean
): Workspace {
  const drafts = { ...workspace.drafts };
  for (const [path, refs] of Object.entries(references)) {
    const destination = moves.get(path) || path;
    const changes = refs.filter(
      (ref) =>
        moves.has(ref.target) ||
        (ref.range && destination !== path && !ref.raw.startsWith('/'))
    );
    if (!changes.length) continue;
    if (readonly(path, allowTemplates, allowConfig))
      throw Error(`Updating references in ${path} requires edit permission.`);
    const page = drafts[path] || workspace.pages[path];
    if (!page) throw Error(`Load ${path} before updating its references.`);
    const replacement = (ref: SchemaReference) => {
      const target = moves.get(ref.target) || ref.target;
      const suffix = /[#?].*$/.exec(ref.raw)?.[0] || '';
      // Frontmatter relation fields keep their repository-path contract.
      if (ref.pointer !== null)
        return (ref.raw.startsWith('/') ? '/' : '') + target + suffix;
      if (ref.raw.startsWith('/')) return '/' + target + suffix;
      const parent = destination.split('/').slice(0, -1);
      const parts = target.split('/');
      while (parent.length && parts.length && parent[0] === parts[0]) {
        parent.shift();
        parts.shift();
      }
      const relative = '../'.repeat(parent.length) + parts.join('/');
      return (
        (/^[a-z][a-z\d+.-]*:/i.test(relative) ? './' : '') + relative + suffix
      );
    };
    let text = page.text;
    const ranges = new Map(
      changes
        .filter((ref) => ref.range)
        .map((ref) => [JSON.stringify(ref.range), ref])
    );
    let previousStart = text.length;
    for (const ref of [...ranges.values()].sort(
      (a, b) => b.range!.start - a.range!.start
    )) {
      const { start, end } = ref.range!;
      if (end > previousStart || text.slice(start, end) !== ref.raw)
        throw Error(`Overlapping or stale reference locations in ${path}.`);
      text = text.slice(0, start) + replacement(ref) + text.slice(end);
      previousStart = start;
    }
    const fields = changes.filter((ref) => ref.pointer !== null);
    if (fields.length) {
      const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(text);
      if (!match) throw Error(`Missing frontmatter in ${path}.`);
      const yaml = parseDocument(match[1]);
      if (yaml.errors.length) throw yaml.errors[0];
      for (const ref of fields) {
        const keys = ref
          .pointer!.slice(1)
          .split('/')
          .map((key) => key.replace(/~1/g, '/').replace(/~0/g, '~'));
        yaml.setIn(keys, replacement(ref));
      }
      const newline = text.startsWith('---\r\n') ? '\r\n' : '\n';
      text =
        '---' +
        newline +
        yaml.toString().replace(/\n/g, newline) +
        '---' +
        newline +
        text.slice(match[0].length);
    }
    if (text === page.text) continue;
    drafts[path] = {
      ...page,
      text,
      base: workspace.drafts[path]?.base ?? page.text
    };
  }
  return { ...workspace, drafts };
}

// A filename may contain characters the configured wiki grammar cannot express.
// Reparse before accepting a move rather than silently dropping that relation.
export function assertReferencesPreserved(
  before: SchemaReferences,
  after: SchemaReferences,
  moves: Map<string, string>
) {
  const keys = (refs: SchemaReference[], mapped: boolean) =>
    refs
      .map((ref) =>
        JSON.stringify([
          mapped ? moves.get(ref.target) || ref.target : ref.target,
          ref.pointer
        ])
      )
      .sort();
  for (const [path, refs] of Object.entries(before)) {
    if (!refs.length) continue;
    const destination = moves.get(path) || path;
    if (
      JSON.stringify(keys(refs, true)) !==
      JSON.stringify(keys(after[destination] || [], false))
    )
      throw Error(
        `Moving these documents would change schema references in ${path}. Choose a name supported by the schema.`
      );
  }
}
