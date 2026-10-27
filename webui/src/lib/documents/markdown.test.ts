import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './markdown';
describe('local Markdown', () => {
  it('renders repository wiki links, aliases, and literal filename characters', () => {
    const el = document.createElement('div');
    el.innerHTML = renderMarkdown(
      '[[companies/moja-firma]] [[companies/moja-firma.md#People|Our company]] [[notes/café %20|A & B]]'
    );
    const links = [...el.querySelectorAll('a')];
    expect(links.map((a) => a.getAttribute('href'))).toEqual([
      '/companies/moja-firma.md',
      '/companies/moja-firma.md#People',
      '/notes/caf%C3%A9%20%2520.md'
    ]);
    expect(links.map((a) => a.textContent)).toEqual([
      'companies/moja-firma',
      'Our company',
      'A & B'
    ]);
    expect(links.every((a) => a.classList.contains('internal-link'))).toBe(
      true
    );
  });

  it('marks exact missing paths as broken instead of searching the repository', () => {
    const el = document.createElement('div');
    el.innerHTML = renderMarkdown(
      '[[unique]] [[/notes/unique]] [[shared]]',
      undefined,
      {
        sourcePath: 'work/source.md',
        paths: ['notes/unique.md', 'a/shared.md', 'b/shared.md']
      }
    );
    expect(
      [...el.querySelectorAll('a')].map((a) => a.getAttribute('href'))
    ).toEqual(['/work/unique.md', '/notes/unique.md', '/work/shared.md']);
    expect(el.querySelectorAll('a.broken-link')).toHaveLength(2);
    expect(
      el.querySelector('a.broken-link')?.getAttribute('aria-invalid')
    ).toBe('true');
  });

  it('resolves wiki links relative to the current document', () => {
    const el = document.createElement('div');
    const paths = [
      'meeting.md',
      'work/meeting.md',
      'personal/meeting.md',
      'work/notes/detail.md'
    ];
    el.innerHTML = renderMarkdown(
      '[[meeting]] [[meeting.md#Notes|Notes]] [[notes/detail]]',
      undefined,
      {
        sourcePath: 'work/page.md',
        paths
      }
    );
    expect(
      [...el.querySelectorAll('a')].map((a) => a.getAttribute('href'))
    ).toEqual([
      '/work/meeting.md',
      '/work/meeting.md#Notes',
      '/work/notes/detail.md'
    ]);
    expect(el.querySelector('.broken-link')).toBeNull();
  });

  it('supports explicit root and parent paths without implicit fallback', () => {
    const el = document.createElement('div');
    el.innerHTML = renderMarkdown(
      '[[../meeting]] [[/companies/example]] [[unique]] [[./missing]] [[../../../escape]]',
      undefined,
      {
        sourcePath: 'work/notes/page.md',
        paths: [
          'work/meeting.md',
          'companies/example.md',
          'archive/unique.md',
          'missing.md'
        ]
      }
    );
    expect(
      [...el.querySelectorAll('a')].map((a) => a.getAttribute('href'))
    ).toEqual([
      '/work/meeting.md',
      '/companies/example.md',
      '/work/notes/unique.md',
      '/work/notes/missing.md'
    ]);
    expect(el.textContent).toContain('[[../../../escape]]');
  });

  it('uses the same exact relative and root paths for Markdown links', () => {
    const el = document.createElement('div');
    el.innerHTML = renderMarkdown(
      '[Local](meeting) [Root](/meeting.md) [Missing](unique)',
      undefined,
      {
        sourcePath: 'work/source.md',
        paths: ['work/meeting.md', 'meeting.md', 'notes/unique.md']
      }
    );
    expect(
      [...el.querySelectorAll('a')].map((a) => a.getAttribute('href'))
    ).toEqual(['/work/meeting.md', '/meeting.md', '/work/unique.md']);
    expect(el.querySelector('a.broken-link')?.textContent).toBe('Missing');
  });

  it('preserves wiki examples in code, escaped text, and existing links', () => {
    const el = document.createElement('div');
    el.innerHTML = renderMarkdown(
      '`[[inline]]`\n\n```md\n[[fenced]]\n```\n\n\\[[escaped]]\n\n[See [[example]]](other.md)\n\n[[unclosed'
    );
    expect(el.querySelectorAll('a')).toHaveLength(1);
    expect(el.querySelector('a')?.textContent).toBe('See [[example]]');
    expect(el.textContent).toContain('[[escaped]]');
    expect(el.textContent).toContain('[[unclosed');
  });

  it('keeps wiki labels inert and rejects unsafe destinations', () => {
    const el = document.createElement('div');
    el.innerHTML = renderMarkdown(
      '[[safe|<script>alert(1)</script>]] [[javascript:alert(1)]] [[../escape]] [[//evil.test]]'
    );
    expect(el.querySelector('script')).toBeNull();
    expect(el.querySelector('a')?.getAttribute('href')).toBe('/safe.md');
    expect(el.querySelector('a')?.textContent).toBe(
      '<script>alert(1)</script>'
    );
    expect(el.innerHTML).not.toContain('href="javascript:');
    expect(el.innerHTML).not.toContain('href="/escape');
    // URL auto-linking may still recognize external text inside invalid wiki syntax.
    expect(el.querySelectorAll('a.internal-link')).toHaveLength(1);
  });

  it('distinguishes internal links from external URLs, email, and metadata links', () => {
    const el = document.createElement('div');
    el.innerHTML = renderMarkdown(
      '---\nwebsite: https://company.test\n---\n# Links\n\n[Note](other.md) [Section](#links) [Web](https://example.test) [CDN](//cdn.test/file) [Email](mailto:hello@example.test) https://autolink.test'
    );
    expect(el.querySelectorAll('a.internal-link')).toHaveLength(2);
    expect(el.querySelectorAll('a.external-link')).toHaveLength(5);
    expect(
      [...el.querySelectorAll('a.external-link')].every((a) =>
        a.getAttribute('rel')?.includes('noreferrer')
      )
    ).toBe(true);
  });

  it('renders rich Markdown without executable HTML or remote images', () => {
    const html = renderMarkdown(
      '# Heading\n\n<script>alert(1)</script>\n\n[x](javascript:alert%281%29) ![alt](https://tracker.test/x)\n\n| a | b |\n|---|---|\n| 1 | 2 |'
    );
    expect(html).toContain('<h1>Heading</h1>');
    expect(html).toContain('<table>');
    expect(html).not.toMatch(/<script|<img|href="javascript:/);
  });
});

it('renders nested and loose tasks without interpreting markers outside list items', () => {
  const html = renderMarkdown(
    '- [ ] Outer\n  - [X] Inner\n\n- [x] Loose\n\n  More text\n\n[ ] Paragraph\n\n`[x] Code`'
  );
  const element = document.createElement('div');
  element.innerHTML = html;
  const checkboxes = [...element.querySelectorAll<HTMLInputElement>('input')];
  expect(checkboxes.map((c) => c.checked)).toEqual([false, true, true]);
  expect(checkboxes.every((c) => c.disabled)).toBe(true);
  expect(element.textContent).toContain('[ ] Paragraph');
  expect(element.textContent).toContain('[x] Code');
});

it('escapes code fences and preserves the language for Pierre rendering', () => {
  const el = document.createElement('div');
  el.innerHTML = renderMarkdown('~~~rust\nlet x = "<script>";\n~~~');
  expect(el.querySelector('code.language-rust')?.textContent).toBe(
    'let x = "<script>";\n'
  );
  expect(el.querySelector('script')).toBeNull();
});

it('renders properties after the heading and uses schema enums', () => {
  const el = document.createElement('div');
  el.innerHTML = renderMarkdown(
    '---\nname: <test>\ntitle: Heading\nstate: inbox\nwaiting_on: null\npriority: high\ntags: [demo, example]\n---\n\n# Heading',
    { properties: { priority: { enum: ['high', 'low'] } } }
  );
  expect(el.firstElementChild?.tagName).toBe('H1');
  expect(el.querySelectorAll('.property-badge')).toHaveLength(3);
  expect(el.querySelector('dl')?.textContent).toBe('Name<test>');
  expect(el.querySelector('test')).toBeNull();
  expect(el.textContent).not.toContain('Waiting');
  expect(el.querySelector('code')).toBeNull();
});
it('preserves malformed YAML as source and reports its error', () => {
  const html = renderMarkdown('---\na: [broken\n---\n# Heading');
  expect(html).toContain('role="alert"');
  expect(html).toContain('language-yaml');
});
it('escapes nested and hostile metadata', () => {
  const html = renderMarkdown(
    '---\nlink: "javascript:alert(1)"\nextra: {nested: "<script>"}\n---\n# Heading'
  );
  expect(html).not.toContain('<script>');
  expect(html).not.toContain('href="javascript:');
  expect(html).toContain('nested');
});
