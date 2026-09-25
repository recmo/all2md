import { File } from '@pierre/diffs';
import { renderMarkdown, type PropertySchema } from './markdown';
import { fenceLanguage } from './pierre';

// Svelte action: sanitized prose plus locally rendered Pierre code blocks.
export function markdownView(
  node: HTMLElement,
  text: string | { text: string; schema?: PropertySchema }
) {
  let files: File[] = [];
  function clear() {
    for (const file of files) file.cleanUp();
    files = [];
  }
  function render(value: typeof text) {
    clear();
    node.innerHTML = renderMarkdown(
      typeof value === 'string' ? value : value.text,
      typeof value === 'string' ? undefined : value.schema
    );
    for (const code of node.querySelectorAll('pre > code')) {
      const source = code.textContent || '';
      const languageClass = [...code.classList].find((c) =>
        c.startsWith('language-')
      );
      const language = fenceLanguage(languageClass?.slice(9) || '');
      const host = document.createElement('div');
      host.className = 'markdown-code';
      host.dataset.language = language;
      code.parentElement!.replaceWith(host);
      const file = new File({
        theme: 'pierre-light',
        unsafeCSS: 'pre { --diffs-bg: #f8f8f8; font-weight: 500; }',
        disableFileHeader: true,
        disableLineNumbers: true,
        overflow: 'wrap'
      });
      file.render({
        file: { name: 'snippet', contents: source, lang: language },
        containerWrapper: host
      });
      files.push(file);
    }
  }
  render(text);
  return { update: render, destroy: clear };
}
