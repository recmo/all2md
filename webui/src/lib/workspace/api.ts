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
    return `${index + 1}:${Array.from(digest.slice(0, 2), (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
  };
  const first = await hash(0);
  return lines.length === 1
    ? first
    : `${first}..${await hash(lines.length - 1)}`;
}

async function rawRead(path: string): Promise<Response> {
  const url =
    '/path/' +
    path.replace(/^\//, '').split('/').map(encodeURIComponent).join('/');
  let response: Response;
  try {
    response = await fetch(url, {
      cache: 'no-store',
      signal: AbortSignal.timeout(15000)
    });
  } catch {
    throw new ApiError(
      'Daemon unavailable. Cached documents and local drafts remain available.'
    );
  }
  if (!response.ok && response.status !== 404) {
    throw new ApiError(await response.text(), response.status);
  }
  return response;
}

export const api = {
  async page(path: string): Promise<Page> {
    const response = await rawRead(path);
    const link = response.headers
      .get('link')
      ?.match(/<([^>]+)>;\s*rel="describedby"/)?.[1];
    const template = link
      ? decodeURIComponent(
          new URL(link, 'http://mdstore.invalid').pathname.slice(
            '/path/'.length
          )
        )
      : null;
    return {
      path: path.replace(/^\//, ''),
      template: template ? { path: template } : null,
      exists: response.ok,
      text: response.ok ? await response.text() : ''
    };
  },
  async directory(path = '/'): Promise<Directory> {
    const response = await rawRead(path);
    if (!response.ok)
      throw new ApiError(await response.text(), response.status);
    const text = await response.text();
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
  async search(query: string, variants: string[] = []) {
    const response = await mcp<{ results: SearchResult[]; degraded: string[] }>(
      'search',
      {
        query,
        ...(variants.length ? { variants } : {})
      }
    );
    return {
      ...response,
      results: response.results.map((result) => ({
        path: result.path.replace(/:\d+-\d+$/, '').replace(/^\//, ''),
        excerpt: result.excerpt.replace(/^\d+:[0-9a-f]{4}\|/gm, '')
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
    return mcp<{ success: true }>('edit', {
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
