export type Page = {
  path: string;
  exists: boolean;
  text: string;
  template: { path: string; content: string; definition?: { frontmatter?: import("./markdown").PropertySchema } } | null;
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
  source?: {path: string; line: number} | null;
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
export class Api {
  token = '';
  async request<T>(path: string, body?: unknown): Promise<T> {
    let response: Response;
    try {
      response = await fetch(path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: {
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
          ...(this.token ? { Authorization: `Bearer ${this.token}` } : {})
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(15000)
      });
    } catch {
      throw new ApiError(
        'Daemon unavailable. Cached documents and local drafts remain available.'
      );
    }
    if (response.status === 401)
      throw new ApiError('Enter your bearer token to connect.', 401);
    const value = await response.json().catch(() => ({ error: `Request failed: HTTP ${response.status} ${response.statusText}` }));
    if (!response.ok)
      throw new ApiError(
        value.findings?.length
          ? 'Validation failed.'
          : value.error || response.statusText,
        response.status,
        value.findings || []
      );
    return value;
  }
  async mcp<T>(name: string, args: unknown): Promise<T> {
    const response = await this.request<{
      error?: { message: string };
      result: {
        isError: boolean;
        content: { text: string }[];
        structuredContent: T;
      };
    }>('/mcp', {
      jsonrpc: '2.0',
      id: crypto.randomUUID(),
      method: 'tools/call',
      params: { name, arguments: args }
    });
    if (response.error) throw new ApiError(response.error.message, 400);
    if (response.result.isError)
      throw new ApiError(
        response.result.content.map((c) => c.text).join('\n') +
          '\n' +
          JSON.stringify(response.result.structuredContent || '', null, 2),
        422,
        (
          response.result.structuredContent as {
            validation_findings?: ValidationFinding[];
          }
        )?.validation_findings || []
      );
    return response.result.structuredContent;
  }
  page(path: string) {
    return this.mcp<Page>('get_page', { path });
  }
  search(query: string, variants: string[]) {
    return this.mcp<{ results: SearchResult[]; degraded: string[] }>('search', {
      query,
      variants
    });
  }
  validate(request: EditRequest) {
    return this.request<{ valid: boolean; restart_required?: boolean }>(
      '/validate',
      request
    );
  }
  apply(request: EditRequest) {
    return this.mcp<{
      touched_paths: string[];
      push: 'disabled' | 'pushed' | 'queued' | 'diverged';
      status: 'accepted' | 'already_applied';
      restart_required?: boolean;
    }>('apply_edits', request);
  }
}
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
    (!allowTemplateEdits && path.split('/').at(-1) === 'template.md')
  );
}

// Shared by SPA routes; credentials never enter persistent storage.
export const api = new Api();
