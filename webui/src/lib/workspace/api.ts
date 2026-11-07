export type Page = {
  readonly?: boolean;
  asset?: { oid: string; size: number };
  hash?: string | null;
  revision?: string;
  path: string;
  exists: boolean;
  text: string;
  template: { path: string } | null;
};
export type Directory = {
  revision: string;
  path: string;
  children: { path: string; kind: 'file' | 'directory' }[];
};
export type Draft = Page & { base: string };
export type EditRequest = {
  summary: string;
  edits: (
    | { op: 'replace_page'; path: string; base: string; content: string }
    | { op: 'create_page'; path: string; content: string }
    | { op: 'delete_page'; path: string; base: string }
  )[];
};
export type SearchResult = {
  path: string;
  excerpt: string;
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
type EditError = { message: string; location?: string; schema?: string };
function errorLocation(location: string): { path: string; line?: number } {
  const match = location.match(/:(\d+)$/);
  return {
    path: (match ? location.slice(0, match.index) : location).replace(
      /^\//,
      ''
    ),
    ...(match ? { line: Number(match[1]) } : {})
  };
}

async function mcp<T>(name: string, args: unknown): Promise<T> {
  const body = JSON.stringify({
    jsonrpc: '2.0',
    id: crypto.randomUUID(),
    method: 'tools/call',
    params: { name, arguments: args }
  });
  // Identical edit bytes use mdstore's durable commit receipt on retry.
  const attempts = name === 'edit' ? 2 : 1;
  let response!: Response;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      response = await fetch('/mcp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        // Search uses the daemon's configured embedding and reranking deadlines.
        ...(name === 'search' ? {} : { signal: AbortSignal.timeout(15000) })
      });
      if (response.status >= 500 && attempt + 1 < attempts) {
        await response.body?.cancel();
        continue;
      }
      break;
    } catch {
      if (attempt + 1 === attempts)
        throw new ApiError(
          'Daemon unavailable. Check the connection and retry.'
        );
    }
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
  if (result.isError) {
    const errors = (
      result.structuredContent as {
        errors?: EditError[];
      }
    )?.errors;
    throw new ApiError(
      errors
        ? errors.map((error) => error.message).join('\n')
        : result.content.map((c) => c.text).join('\n'),
      422,
      errors?.flatMap((error): ValidationFinding[] => {
        if (error.location === undefined) return [];
        const source = error.schema ? errorLocation(error.schema) : undefined;
        return [
          {
            ...errorLocation(error.location),
            message: error.message,
            ...(source
              ? { source: { path: source.path, line: source.line! } }
              : {})
          }
        ];
      }) || []
    );
  }
  return result.structuredContent;
}

export async function completeAnchor(text: string): Promise<string> {
  if (text === '') return '0:';
  const lines = text.split(/\r?\n/);
  if (text.endsWith('\n')) lines.pop();
  const hash = async (index: number) => {
    const encoder = new TextEncoder();
    const fields = [
      lines[index - 1] ?? '',
      lines[index],
      lines[index + 1] ?? ''
    ].map((line) => encoder.encode(line));
    const bytes = new Uint8Array(
      fields.reduce((length, field) => length + 8 + field.length, 0)
    );
    const view = new DataView(bytes.buffer);
    let offset = 0;
    for (const field of fields) {
      view.setBigUint64(offset, BigInt(field.length));
      bytes.set(field, offset + 8);
      offset += 8 + field.length;
    }
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
    return `${index + 1}:${Array.from(digest.slice(0, 1), (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
  };
  const first = await hash(0);
  return lines.length === 1
    ? first
    : `${first}..${await hash(lines.length - 1)}`;
}

export function fileUrl(path: string) {
  return (
    '/' + path.replace(/^\//, '').split('/').map(encodeURIComponent).join('/')
  );
}
export type Api = typeof api;
export const api = {
  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(path, {
      ...init,
      headers: {
        ...init.headers
      }
    });
    if (response.status === 204) return undefined as T;
    const value = await response.json().catch(() => ({}));
    if (!response.ok)
      throw new ApiError(
        value.detail || value.title || response.statusText,
        response.status,
        value.findings || []
      );
    return value as T;
  },
  page(path: string) {
    return this.request<Page>(fileUrl(path), {
      headers: { Accept: 'application/vnd.mdstore.page+json' }
    });
  },
  directory(path = '/') {
    return this.request<Directory>(fileUrl(path));
  },
  async search(query: string, variants: string[] = []) {
    const response = await mcp<{
      commit: string;
      results: SearchResult[];
      degraded: string[];
    }>('search', {
      query,
      ...(variants.length ? { variants } : {})
    });
    return {
      ...response,
      results: response.results.map((result) => ({
        path: result.path.replace(/:\d+-\d+$/, '').replace(/^\//, ''),
        excerpt: result.excerpt.replace(/^\d+:[0-9a-f]{2}\|/gm, '')
      }))
    };
  },
  async apply(request: EditRequest) {
    const edits: Record<
      string,
      { op: 'replace'; anchor?: string; content: string }[]
    > = Object.create(null);
    for (const edit of request.edits) {
      const operation = {
        op: 'replace' as const,
        ...(edit.op !== 'create_page'
          ? { anchor: await completeAnchor(edit.base) }
          : {}),
        content: edit.op === 'delete_page' ? '' : edit.content
      };
      (edits[`/${edit.path.replace(/^\//, '')}`] ??= []).push(operation);
    }
    return mcp<{ success: true; commit: string }>('edit', {
      summary: request.summary,
      edits
    });
  }
};
export function editRequest(
  summary: string,
  drafts: Record<string, Draft>,
  deletions: Record<string, string> = {}
): EditRequest {
  return {
    summary: summary,
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
            ? d.text === ''
              ? { op: 'delete_page' as const, path: d.path, base: d.base }
              : {
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
