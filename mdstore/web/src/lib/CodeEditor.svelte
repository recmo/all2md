<script lang="ts">
  import './pierre';
  import { onMount } from 'svelte';
  import { CodeView } from '@pierre/diffs';
  import type { ValidationFinding } from './api';
  import { Editor } from '@pierre/diffs/edit';

  let {
    value,
    path,
    readonly = false,
    disabled = false,
    findings = [],
    onsource,
    onchange
  }: {
    findings?: ValidationFinding[];
    onsource?: (path: string, line: number) => void;
    value: string;
    path: string;
    readonly?: boolean;
    disabled?: boolean;
    onchange: (text: string) => void;
  } = $props();
  let host: HTMLDivElement;
  let viewer = $state.raw<CodeView<ValidationFinding>>();
  let lastReadonly = true;
  let lastText = '';
  let version = 0;
  let lastFindings = '';
  let pendingLine: number | undefined;
  let revealFrame = 0;
  function revealPendingLine() {
    cancelAnimationFrame(revealFrame);
    revealFrame = requestAnimationFrame(() => {
      if (pendingLine === undefined) return;
      for (const item of viewer?.getRenderedItems() || []) {
        const root = item.element.shadowRoot || item.element;
        const line = root.querySelector<HTMLElement>(`[data-line="${pendingLine}"]`);
        if (line) {
          // The document pane owns scrolling; CodeView's own scroller is expanded.
          line.scrollIntoView({ block: 'center' });
          pendingLine = undefined;
          break;
        }
      }
    });
  }
  export function revealLine(lineNumber: number) {
    pendingLine = Math.max(1, Math.trunc(lineNumber));
    revealPendingLine();
  }

  onMount(() => {
    lastReadonly = readonly;
    lastText = value;
    lastFindings = JSON.stringify(findings);
    const view = new CodeView<ValidationFinding>({
      renderAnnotation(annotation) {
        const note = document.createElement('div');
        note.className = 'validation-diagnostic';
        note.setAttribute('role', 'note');
        const finding = annotation.metadata;
        if (!finding) return note;
        note.textContent = finding.message;
        if (finding.source) {
          const {path, line} = finding.source;
          const link = document.createElement('a');
          link.textContent = `${path}:${line}`;
          link.href = `#${encodeURIComponent(path)}`;
          link.style.cssText = 'display:block;color:inherit;text-decoration:underline';
          link.onclick = (event) => { if (onsource) {event.preventDefault(); onsource(path, line);} };
          note.append(link);
        }
        note.style.cssText =
          'padding:8px 12px;color:#a52b20;background:#faeae4;border-left:3px solid #bc3528;white-space:pre-wrap;font:13px/1.5 system-ui';
        return note;
      },
      onPostRender: revealPendingLine,
      theme: 'pierre-light',
      overflow: 'wrap',
      disableFileHeader: true,
      createEditor: (type, options, key) => new Editor(type, options, key),
      onItemEditChange(event) {
        lastText = event.file.contents;
        onchange(lastText);
      },
      // Changes are saved through the live stream, including before teardown.
      onItemEditComplete: () => 'accept'
    });
    view.setup(host);
    view.setItems([
      {
        id: path,
        type: 'file',
        file: { name: path, contents: value },
        edit: !readonly,
        annotations: findings.map((finding) => ({
          lineNumber: finding.line || 0,
          metadata: finding
        })),
        version
      }
    ]);
    viewer = view;
    return () => { cancelAnimationFrame(revealFrame); view.cleanUp(); };
  });

  $effect(() => {
    const view = viewer;
    const text = value;
    const diagnostics = findings;
    const diagnosticSignature = JSON.stringify(diagnostics);
    if (
      view &&
      (text !== lastText ||
        diagnosticSignature !== lastFindings ||
        readonly !== lastReadonly)
    ) {
      lastReadonly = readonly;
      lastFindings = diagnosticSignature;
      lastText = text;
      view.updateItem({
        id: path,
        type: 'file',
        file: { name: path, contents: text },
        edit: !readonly,
        annotations: diagnostics.map((finding) => ({
          lineNumber: finding.line || 0,
          metadata: finding
        })),
        version: ++version
      });
    }
  });
</script>

<div
  class="code-editor"
  bind:this={host}
  inert={disabled}
  role="region"
  aria-label={readonly ? 'Source code' : 'Code editor'}
></div>

<style>
  .code-editor {
    height: auto;
    min-height: 0;
    overflow: visible;
    border: 0;
    border-radius: 0;
  }
</style>
