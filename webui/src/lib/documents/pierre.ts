import {
  getFiletypeFromFileName,
  registerCustomLanguage,
  resolveLanguage
} from '@pierre/diffs';

// Keep the authored language name while sharing Python's TextMate grammar.
async function starlarkGrammar() {
  const { data } = await resolveLanguage('python');
  return data.map((grammar) => ({
    ...grammar,
    aliases: [...(grammar.aliases || []), 'starlark']
  }));
}
registerCustomLanguage(
  'starlark',
  async () => ({ default: await starlarkGrammar() }),
  ['starlark', 'bzl', 'BUILD', 'WORKSPACE']
);

// Markdown embeds languages through explicit fence patterns, independently of aliases.
registerCustomLanguage(
  'mdstore-markdown',
  async () => {
    const [{ data }, python, { data: yaml }] = await Promise.all([
      resolveLanguage('markdown'),
      starlarkGrammar(),
      resolveLanguage('yaml')
    ]);
    const markdown = data.map((source) => {
      let grammar = structuredClone(source);
      if (grammar.name === 'markdown') {
        grammar.name = 'mdstore-markdown';
        grammar.aliases = [];
        // Only the opening document block is frontmatter, never a later thematic break.
        grammar = {
          ...grammar,
          patterns: [
            {
              begin: '\\A---[ \\t]*$',
              end: '^---[ \\t]*$',
              beginCaptures: {
                0: { name: 'punctuation.definition.frontmatter.markdown' }
              },
              endCaptures: {
                0: { name: 'punctuation.definition.frontmatter.markdown' }
              },
              contentName: 'meta.embedded.block.yaml',
              patterns: [{ include: 'source.yaml' }]
            },
            ...(grammar.patterns || [])
          ]
        };
        const rule = grammar.repository?.fenced_code_block_python;
        if (typeof rule?.begin !== 'string' || !rule.begin.includes('python|'))
          throw new Error('Markdown Python fence rule is missing');
        grammar.repository!.fenced_code_block_python = {
          ...rule,
          begin: rule.begin.replace('python|', 'python|starlark|')
        };
      }
      return grammar;
    });
    return { default: [...python, ...yaml, ...markdown] };
  },
  ['md', 'markdown']
);

export function fenceLanguage(info: string): string {
  const name = info.trim().split(/\s+/)[0].toLowerCase();
  const aliases: Record<string, string> = {
    py: 'python',
    rs: 'rust',
    latex: 'tex',
    lean: 'lean4',
    typ: 'typst',
    yml: 'yaml'
  };
  const language = aliases[name] || name;
  // The filename resolver provides a safe text fallback for unknown fences.
  return (
    getFiletypeFromFileName(
      'snippet.' +
        ({
          python: 'py',
          rust: 'rs',
          lean4: 'lean',
          typst: 'typ',
          markdown: 'md',
          javascript: 'js',
          typescript: 'ts'
        }[language] || language)
    ) || 'text'
  );
}
