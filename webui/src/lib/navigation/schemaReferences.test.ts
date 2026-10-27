import { expect, it } from 'vitest';
import { emptyCache } from '../workspace/cache';
import {
  rewriteSchemaReferences,
  assertReferencesPreserved
} from './schemaReferences';
import { moveTree } from './treeEdits';

it('stages dependency and wiki rewrites together with a move while preserving comments and fragments', () => {
  const cache = emptyCache('repo');
  const text =
    '---\ndepends_on: [tasks/a.md] # keep\n---\n# 😀 B\n\n[[a#part|Label]]\n';
  cache.paths = ['tasks/a.md', 'tasks/b.md'];
  cache.pages = {
    'tasks/a.md': {
      path: 'tasks/a.md',
      text: '# A\n',
      exists: true,
      template: null
    },
    'tasks/b.md': { path: 'tasks/b.md', text, exists: true, template: null }
  };
  const start = text.indexOf('a#part');
  const references = {
    'tasks/b.md': [
      {
        target: 'tasks/a.md',
        raw: 'tasks/a.md',
        pointer: '/depends_on/0',
        range: null
      },
      {
        target: 'tasks/a.md',
        raw: 'a#part',
        pointer: null,
        range: { start, end: start + 6 }
      }
    ]
  };
  const rewritten = rewriteSchemaReferences(
    cache,
    references,
    new Map([['tasks/a.md', 'tasks/renamed.md']]),
    false,
    false
  );
  const next = moveTree(
    rewritten,
    'tasks/a.md',
    'tasks/renamed.md',
    false,
    cache.pages
  );
  expect(next.drafts['tasks/b.md'].text).toContain(
    'depends_on: [ tasks/renamed.md ] # keep'
  );
  expect(next.drafts['tasks/b.md'].text).toContain('[[renamed.md#part|Label]]');
  expect(next.drafts['tasks/b.md'].base).toBe(text);
  expect(next.deletions?.['tasks/a.md']).toBe('# A\n');
  expect(cache.drafts).toEqual({});
});
it('does not rewrite references in a protected schema', () => {
  expect(() =>
    rewriteSchemaReferences(
      emptyCache('repo'),
      {
        'schema.md': [
          { target: 'a.md', raw: 'a.md', pointer: '/refs/0', range: null }
        ]
      },
      new Map([['a.md', 'b.md']]),
      false,
      false
    )
  ).toThrow('permission');
});

it('rejects a rename that makes a wiki target unrecognizable', () => {
  expect(() =>
    assertReferencesPreserved(
      {
        'a.md': [
          {
            target: 'b.md',
            raw: 'b',
            range: { start: 0, end: 1 },
            pointer: null
          }
        ]
      },
      { 'a.md': [] },
      new Map([['b.md', 'bracket].md']])
    )
  ).toThrow('schema references');
});

it('rebases outgoing relative wiki links when moving their source', () => {
  const cache = emptyCache('repo');
  const text = '[[./target#Notes|Label]] [[/a/target]]';
  cache.pages['a/source.md'] = {
    path: 'a/source.md',
    text,
    exists: true,
    template: null
  };
  const references = {
    'a/source.md': [
      {
        target: 'a/target.md',
        raw: './target',
        range: { start: 2, end: 10 },
        pointer: null
      },
      {
        target: 'a/target.md',
        raw: '/a/target',
        range: {
          start: text.indexOf('/a/target'),
          end: text.indexOf('/a/target') + 9
        },
        pointer: null
      }
    ]
  };
  const next = rewriteSchemaReferences(
    cache,
    references,
    new Map([['a/source.md', 'b/source.md']]),
    false,
    false
  );
  expect(next.drafts['a/source.md'].text).toBe(
    '[[../a/target.md#Notes|Label]] [[/a/target]]'
  );
});

it('preserves root wiki paths while rewriting moved targets', () => {
  const cache = emptyCache('repo');
  cache.pages['source.md'] = {
    path: 'source.md',
    text: '[[/target]]',
    exists: true,
    template: null
  };
  const next = rewriteSchemaReferences(
    cache,
    {
      'source.md': [
        {
          target: 'target.md',
          raw: '/target',
          range: { start: 2, end: 9 },
          pointer: null
        }
      ]
    },
    new Map([['target.md', 'work/target.md']]),
    false,
    false
  );
  expect(next.drafts['source.md'].text).toBe('[[/work/target.md]]');
});

it('keeps sibling references relative when an entire folder moves', () => {
  const cache = emptyCache('repo');
  cache.pages['a/source.md'] = {
    path: 'a/source.md',
    text: '[[target.md]]',
    exists: true,
    template: null
  };
  const next = rewriteSchemaReferences(
    cache,
    {
      'a/source.md': [
        {
          target: 'a/target.md',
          raw: 'target.md',
          range: { start: 2, end: 11 },
          pointer: null
        }
      ]
    },
    new Map([
      ['a/source.md', 'b/source.md'],
      ['a/target.md', 'b/target.md']
    ]),
    false,
    false
  );
  expect(next.drafts).toEqual({});
});
