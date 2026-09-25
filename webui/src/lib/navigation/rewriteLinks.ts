import MarkdownIt from 'markdown-it';

const md = new MarkdownIt();
const encodePathPart = (part: string) =>
  encodeURIComponent(part).replace(
    /[!'()*]/g,
    (char) => '%' + char.charCodeAt(0).toString(16).toUpperCase()
  );
// Parse destinations with MarkdownIt's own helpers, preserving titles, labels,
// whitespace, code spans and fenced/indented code verbatim.
export function rewriteLinks(
  text: string,
  oldPath: string,
  newPath: string,
  moves: Map<string, string>,
  inventory: string[] = [...moves.keys()]
): string {
  const lines = text.split('\n');
  const excluded = new Set<number>();
  for (const token of md.parse(text, {})) {
    if (
      (token.type === 'fence' ||
        token.type === 'code_block' ||
        token.type === 'html_block') &&
      token.map
    )
      for (let n = token.map[0]; n < token.map[1]; n++) excluded.add(n);
  }
  function destination(raw: string): string {
    const href = md.utils.unescapeAll(raw);
    if (!href || /^[a-z][a-z\d+.-]*:|^\/\//i.test(href)) return raw;
    const url = new URL(href, `https://mdstore.invalid/${oldPath}`);
    let target: string;
    try {
      target = decodeURIComponent(url.pathname.slice(1));
    } catch {
      return raw;
    }
    // Match the server's extensionless and unique short-name resolution.
    if (!/\.[^/]+$/.test(target) || target.endsWith('.md')) {
      const exact = target.endsWith('.md') ? target : target + '.md';
      if (inventory.includes(exact)) target = exact;
      else {
        const stem = target.replace(/\.md$/, '');
        const matches = inventory.filter(
          (path) =>
            path.endsWith('.md') &&
            (path.slice(0, -3) === stem ||
              path.split('/').at(-1)!.slice(0, -3) === stem)
        );
        if (matches.length === 1) target = matches[0];
      }
    }
    const moved = moves.get(target) ?? target;
    if (moved === target && oldPath === newPath) return raw;
    // Fragment-only links continue to refer to the same moved document.
    if (href.startsWith('#')) return raw;
    const suffix = url.search + url.hash;
    if (href.startsWith('/'))
      return '/' + moved.split('/').map(encodePathPart).join('/') + suffix;
    const parent = newPath.split('/').slice(0, -1);
    const parts = moved.split('/');
    while (parent.length && parts.length && parent[0] === parts[0]) {
      parent.shift();
      parts.shift();
    }
    return (
      '../'.repeat(parent.length) + parts.map(encodePathPart).join('/') + suffix
    );
  }
  const spans: { start: number; end: number }[] = [];
  const parser = new MarkdownIt();
  const env: { references?: Record<string, { href: string; title: string }> } =
    {};
  parser.parse(text, env);
  const masked = lines
    .map((line, n) => (excluded.has(n) ? ' '.repeat(line.length) : line))
    .join('\n');
  // Select each stock rule through the public ruler API; function names may
  // change during production minification.
  for (const name of ['link', 'image']) {
    const rules = new MarkdownIt().inline.ruler;
    rules.enableOnly(name);
    const original = rules.getRules('')[0];
    parser.inline.ruler.at(name, (state, silent) => {
      const start = state.pos;
      const result = original(state, silent);
      if (result && !silent && state.src === masked) {
        const bracket = start + (name === 'image' ? 1 : 0);
        const end = parser.helpers.parseLinkLabel(
          state,
          bracket,
          name === 'link'
        );
        if (end >= 0 && state.src[end + 1] === '(') {
          let a = end + 2;
          while (/\s/.test(state.src[a] || '') && a < state.src.length) a++;
          const parsed = parser.helpers.parseLinkDestination(
            state.src,
            a,
            state.src.length
          );
          if (parsed.ok)
            spans.push({
              start: a + (state.src[a] === '<' ? 1 : 0),
              end: parsed.pos - (state.src[a] === '<' ? 1 : 0)
            });
        }
      }
      return result;
    });
  }
  parser.inline.parse(masked, parser, env, []);
  let offset = 0;
  for (const [n, line] of lines.entries()) {
    const ref = /^ {0,3}\[([^\]\n]+)\]:\s*/.exec(line);
    if (
      !excluded.has(n) &&
      ref &&
      env.references?.[md.utils.normalizeReference(ref[1])]
    ) {
      let start = offset + ref[0].length;
      while (/\s/.test(text[start] || '') && start < text.length) start++;
      const parsed = md.helpers.parseLinkDestination(text, start, text.length);
      if (parsed.ok)
        spans.push({
          start: start + (text[start] === '<' ? 1 : 0),
          end: parsed.pos - (text[start] === '<' ? 1 : 0)
        });
    }
    offset += line.length + 1;
  }
  for (const { start, end } of spans.sort((a, b) => b.start - a.start)) {
    text =
      text.slice(0, start) +
      destination(text.slice(start, end)) +
      text.slice(end);
  }
  return text;
}
