export type Page = {
  path: string;
  revision?: string;
  hash?: string | null;
  exists: boolean;
  text: string;
  template: { path: string; content: string; definition?: { frontmatter?: import("./markdown").PropertySchema } } | null;
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
  private async mcp<T>(name: string, args: unknown): Promise<T> {
    let response: Response;
    try {
      response = await fetch('/mcp', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(this.token ? { Authorization: `Bearer ${this.token}` } : {})
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
    if (response.status === 401)
      throw new ApiError('Enter your bearer token to connect.', 401);
    const value = await response.json().catch(() => {
      throw new ApiError(`Request failed: HTTP ${response.status} ${response.statusText}`, response.status);
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
    return result.structuredContent;
  }
  page(path: string) {
    return this.mcp<Page>('get_page', { path });
  }
  directory(path = '/') {
    return this.mcp<Directory>('get_page', { path });
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
