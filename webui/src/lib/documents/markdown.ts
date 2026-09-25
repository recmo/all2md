import MarkdownIt from 'markdown-it';
import DOMPurify from 'dompurify';
import { parseDocument } from 'yaml';
export type PropertySchema = {
  properties?: Record<string, { enum?: unknown[]; type?: unknown }>;
  required?: string[];
};
const parser = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: false
});
// Recognize task markers only at the start of a list item's first paragraph.
parser.core.ruler.after('inline', 'task_lists', (state) => {
  for (let i = 2; i < state.tokens.length; i++) {
    const token = state.tokens[i];
    if (
      token.type !== 'inline' ||
      state.tokens[i - 1].type !== 'paragraph_open' ||
      state.tokens[i - 2].type !== 'list_item_open'
    )
      continue;
    const first = token.children?.[0];
    const marker =
      first?.type === 'text' && /^\[([ xX])\](?:\s+|$)/.exec(first.content);
    if (!marker || !first) continue;
    first.content = first.content.slice(marker[0].length);
    const checkbox = new state.Token('html_inline', '', 0);
    checkbox.content =
      '<input type="checkbox" disabled' +
      (marker[1].toLowerCase() === 'x' ? ' checked' : '') +
      ' aria-label="' +
      (marker[1] === ' ' ? 'Incomplete task' : 'Completed task') +
      '"> ';
    token.children!.unshift(checkbox);
    state.tokens[i - 2].attrJoin('class', 'task-list-item');
  }
});
parser.renderer.rules.image = (tokens, index) =>
  `<span class="image-description">${parser.utils.escapeHtml(tokens[index].content)}</span>`;
const escape = parser.utils.escapeHtml;
function propertyValue(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (
    Array.isArray(value) &&
    value.every((v) => typeof v === 'string' && v.length < 60)
  )
    return value
      .map((v) => `<span class="property-pill">${escape(v)}</span>`)
      .join(' ');
  if (typeof value === 'string' && /^https?:\/\//i.test(value))
    return `<a href="${escape(value)}" rel="noreferrer">${escape(value)}</a>`;
  return escape(
    typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
  );
}
export function renderMarkdown(text: string, schema?: PropertySchema): string {
  const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(text);
  if (!frontmatter && /^---\r?\n/.test(text))
    return (
      '<p class="frontmatter-error" role="alert">Invalid frontmatter: missing closing delimiter.</p><pre><code class="language-yaml">' +
      escape(text) +
      '</code></pre>'
    );
  let html = parser.render(
    frontmatter ? text.slice(frontmatter[0].length) : text
  );
  if (frontmatter) {
    try {
      const document = parseDocument(frontmatter[1]);
      if (document.errors.length) throw document.errors[0];
      const values = document.toJS({ maxAliasCount: 100 }) ?? {};
      if (!values || typeof values !== 'object' || Array.isArray(values))
        throw new Error('Frontmatter must be a YAML mapping.');
      const tokens = parser.parse(text.slice(frontmatter[0].length), {});
      const headingIndex = tokens.findIndex(
        (t) => t.type === 'heading_open' && t.tag === 'h1'
      );
      const title =
        headingIndex < 0
          ? ''
          : (tokens[headingIndex + 1].children || [])
              .filter((t) => ['text', 'code_inline'].includes(t.type))
              .map((t) => t.content)
              .join('');
      const badges: string[] = [];
      const rows: string[] = [];
      for (const [key, value] of Object.entries(values)) {
        if (
          value === null ||
          value === '' ||
          (Array.isArray(value) && !value.length)
        )
          continue;
        if (key === 'title' && value === title) continue;
        const label = key
          .replace(/[_-]/g, ' ')
          .replace(/^./, (c) => c.toUpperCase());
        const field = schema?.properties?.[key];
        if (field?.enum || ['state', 'status', 'tags'].includes(key)) {
          badges.push(
            `<span class="property-badge" title="${escape(label)}" aria-label="${escape(label)}: ${escape(String(value))}">${propertyValue(value)}</span>`
          );
        } else {
          rows.push(
            `<dt>${escape(label)}</dt><dd>${propertyValue(value)}</dd>`
          );
        }
      }
      const properties = `<div class="document-properties">${badges.length ? `<div class="property-badges">${badges.join('')}</div>` : ''}${rows.length ? `<details><summary>Properties</summary><dl>${rows.join('')}</dl></details>` : ''}</div>`;
      if (badges.length || rows.length) {
        const end = html.indexOf('</h1>');
        html =
          end < 0
            ? properties + html
            : html.slice(0, end + 5) + properties + html.slice(end + 5);
      }
    } catch (error) {
      html =
        `<p class="frontmatter-error" role="alert">Invalid frontmatter: ${escape(String(error))}</p><pre><code class="language-yaml">${escape(frontmatter[0])}</code></pre>` +
        html;
    }
  }
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['img', 'style'],
    FORBID_ATTR: ['style']
  });
}
