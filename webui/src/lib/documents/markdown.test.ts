import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './markdown';
describe('local Markdown', () => {
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
  el.innerHTML = renderMarkdown('---\nname: <test>\ntitle: Heading\nstate: inbox\nwaiting_on: null\npriority: high\ntags: [demo, example]\n---\n\n# Heading', { properties: { priority: { enum: ['high', 'low'] } } });
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
  const html = renderMarkdown('---\nlink: "javascript:alert(1)"\nextra: {nested: "<script>"}\n---\n# Heading');
  expect(html).not.toContain('<script>');
  expect(html).not.toContain('href="javascript:');
  expect(html).toContain('nested');
});
