import { reconnectChanges } from './events';
export type Page = {
  readonly?: boolean;
  asset?: { oid: string; size: number };
  path: string;
  revision?: string;
  hash?: string | null;
  exists: boolean;
  text: string;
  template: {
    path: string;
    content: string;
    definition?: {
      frontmatter?: import('../documents/markdown').PropertySchema;
    };
  } | null;
};
export type Directory = {
  path: string;
  kind: 'directory';
  revision: string;
  repository: string;
  children: { path: string; kind: 'file' | 'directory'; hash?: string }[];
  allow_template_edits: boolean;
  allow_config_edits: boolean;
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
export function fileUrl(path: string) {
  return (
    '/' + path.replace(/^\//, '').split('/').map(encodeURIComponent).join('/')
  );
}
export class Api {
  async login(token: string) {
    const result = await this.request('/mcp/session', {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {}
    });
    reconnectChanges();
    return result;
  }
  async logout() {
    await this.request('/mcp/session', { method: 'DELETE' });
    reconnectChanges();
  }
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
  }
  async mcp<T>(name: string, args: unknown): Promise<T> {
    // Identical document edit requests have durable, content-derived receipts.
    // Retry the original bytes once if the commit response was lost.
    const body = JSON.stringify({
      jsonrpc: '2.0',
      id: crypto.randomUUID(),
      method: 'tools/call',
      params: { name, arguments: args }
    });
    let response: Response | undefined;
    let value;
    const attempts =
      name === 'edit' &&
      typeof args === 'object' &&
      args !== null &&
      'edits' in args
        ? 2
        : 1;
    for (let attempt = 0; attempt < attempts; attempt++) {
      try {
        response = await fetch('/mcp', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body,
          signal: AbortSignal.timeout(15000)
        });
        if (response.status === 401)
          throw new ApiError('Enter your bearer token to connect.', 401);
        if (response.status >= 500 && attempt + 1 < attempts) {
          await response.body?.cancel();
          continue;
        }
        value = await response.json();
        break;
      } catch (error) {
        if (error instanceof ApiError) throw error;
        if (attempt + 1 === attempts)
          throw new ApiError(
            'Daemon unavailable. Cached documents and local drafts remain available.'
          );
      }
    }
    if (!response) throw new ApiError('Daemon unavailable.');
    if (!response.ok)
      throw new ApiError(
        value.detail || value.title || response.statusText,
        response.status,
        value.findings || []
      );
    if (value.error) throw new ApiError(value.error.message, 400);
    const result: {
      isError: boolean;
      content: { text: string }[];
      structuredContent: T;
    } = value.result;
    if (result.isError) {
      const problem = result.structuredContent as {
        detail?: string;
        status?: number;
        findings?: ValidationFinding[];
      };
      throw new ApiError(
        problem?.detail || result.content.map((c) => c.text).join('\n'),
        problem?.status || 422,
        problem?.findings || []
      );
    }
    return result.structuredContent;
  }
  page(path: string) {
    return this.request<Page>(fileUrl(path), {
      headers: { Accept: 'application/vnd.mdstore.page+json' }
    });
  }
  directory(path = '/') {
    return this.request<Directory>(fileUrl(path));
  }
  search(query: string, variants: string[]) {
    return this.mcp<{ results: SearchResult[]; degraded: string[] }>('search', {
      query,
      variants
    });
  }
  apply(request: EditRequest) {
    return this.mcp<{
      touched_paths: string[];
      push: 'disabled' | 'pushed' | 'queued' | 'diverged';
      status: 'accepted' | 'already_applied';
      restart_required?: boolean;
    }>('edit', request);
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
    (!allowTemplateEdits && path.split('/').at(-1) === 'schema.md')
  );
}

// Shared by SPA routes; credentials never enter persistent storage.
export const api = new Api();
