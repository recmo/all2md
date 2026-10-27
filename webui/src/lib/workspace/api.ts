export type Page = {
  path: string;
  exists: boolean;
  text: string;
  template: { path: string } | null;
};
export type Directory = {
  path: string;
  children: { path: string; kind: 'file' | 'directory' }[];
};
export type Draft = Page & { base: string };
export type EditRequest = {
  edit_summary: string;
  edits: (
    | { op: 'replace_page'; path: string; base: string; content: string }
    | { op: 'create_page'; path: string; content: string }
    | { op: 'delete_page'; path: string; base: string }
  )[];
};
export type SearchResult = {
  path: string;
  excerpt: string;
  matched_arms: string[];
  start_line: number;
  end_line: number;
};
export type ValidationFinding = {
  source?: { path: string; line: number } | null;
  path: string;
  message: string;
  line?: number | null;
};

export class ApiError extends Error {
  constructor(
    message: string,
    public status = 0,
    public findings: ValidationFinding[] = []
  ) {
    super(message);
  }
}
async function mcp<T>(
  name: string,
  args: unknown,
  format: 'structured' | 'text' = 'structured'
): Promise<T> {
  let response: Response;
  try {
    response = await fetch('/mcp', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: crypto.randomUUID(),
        method: 'tools/call',
        params: { name, arguments: args }
      }),
      signal: AbortSignal.timeout(15000)
    });
  } catch {
    throw new ApiError(
      'Daemon unavailable. Cached documents and local drafts remain available.'
    );
  }
  const value = await response.json().catch(() => {
    throw new ApiError(
      `Request failed: HTTP ${response.status} ${response.statusText}`,
      response.status
    );
  });
  if (!response.ok)
    throw new ApiError(value.error || response.statusText, response.status);
  if (value.error) throw new ApiError(value.error.message, 400);
  const result: {
    isError: boolean;
    content: { text: string }[];
    structuredContent: T;
  } = value.result;
  if (result.isError)
    throw new ApiError(
      result.content.map((c) => c.text).join('\n'),
      422,
      (
        result.structuredContent as {
          validation_findings?: ValidationFinding[];
        }
      )?.validation_findings || []
    );
  return format === 'text'
    ? (result.content.map((block) => block.text).join('\n') as T)
    : result.structuredContent;
}

export const api = {
  async page(path: string): Promise<Page> {
    const page = await mcp<{
      path: string;
      template: string | null;
      content: string | null;
    }>('get_page', { path, format: 'raw' });
    const { content, ...metadata } = page;
    const relative = (path: string) => path.replace(/^\//, '');
    return {
      ...metadata,
      path: relative(metadata.path),
      template: metadata.template
        ? { path: relative(metadata.template) }
        : null,
      exists: content !== null,
      text: content ?? ''
    };
  },
  async directory(path = '/'): Promise<Directory> {
    const text = await mcp<string>('get_page', { path }, 'text');
    return {
      path: path === '/' ? '/' : path.replace(/^\//, ''),
      children: text
        .split('\n')
        .filter(Boolean)
        .map((path) => ({
          path: path.replace(/^\//, ''),
          kind: path.endsWith('/') ? 'directory' : 'file'
        }))
    };
  },
  search(query: string, variants: string[]) {
    return mcp<{ results: SearchResult[]; degraded: string[] }>('search', {
      query,
      variants
    });
  },
  apply(request: EditRequest) {
    return mcp<{
      touched_paths: string[];
      push: 'disabled' | 'pushed' | 'queued' | 'diverged';
      status: 'accepted' | 'already_applied';
      restart_required?: boolean;
    }>('apply_edits', request);
  }
};
export function editRequest(
  summary: string,
  drafts: Record<string, Draft>,
  deletions: Record<string, string> = {}
): EditRequest {
  return {
    edit_summary: summary,
    edits: [
      ...Object.entries(deletions).map(([path, base]) => ({
        op: 'delete_page' as const,
        path,
        base
      })),
      ...Object.values(drafts)
        .sort((a, b) => a.path.localeCompare(b.path))
        .map((d) =>
          d.exists
            ? {
                op: 'replace_page' as const,
                path: d.path,
                base: d.base,
                content: d.text
              }
            : { op: 'create_page' as const, path: d.path, content: d.text }
        )
    ]
  };
}
export function readonly(
  path: string,
  allowTemplateEdits = false,
  allowConfigEdits = false
) {
  return (
    ((path === 'config.yaml' || /(^|\/)\.?rumdl\.toml$/.test(path)) &&
      !allowConfigEdits) ||
    (!allowTemplateEdits && path.split('/').at(-1) === 'schema.md')
  );
}
