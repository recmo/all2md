<script lang="ts">
  import {
    loadDocuments,
    saveDocuments,
    clearDocuments
  } from '$lib/workspace/documentCache';
  import {
    rewriteSchemaReferences,
    assertReferencesPreserved
  } from '$lib/navigation/schemaReferences';
  import { documentReferences } from '$lib/validation/localValidation';
  import { readInventory } from '$lib/workspace/inventory';
  import { buildSnapshot } from '$lib/validation/localValidation';
  import { onMount, tick } from 'svelte';
  import { navigationDrawer } from '$lib/navigation/navigationDrawer';
  let mobile = $state(false);
  let drawerOpen = $state(false);
  function closeDrawer() {
    if (!drawerOpen) return;
    drawerOpen = false;
    void tick().then(() => {
      if (!drawerOpen && mobile)
        document
          .querySelector<HTMLButtonElement>('.sidebar-toggle')
          ?.focus({ preventScroll: true });
    });
  }
  onMount(() => {
    const media = window.matchMedia('(max-width: 620px)');
    const update = () => {
      mobile = media.matches;
      if (!mobile) drawerOpen = false;
    };
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  });
  import AppViews from '$lib/apps/AppViews.svelte';
  import SpeechReview from '$lib/speech/SpeechReview.svelte';
  import AssetView from '$lib/speech/AssetView.svelte';
  import ImportFiles from '$lib/imports/ImportFiles.svelte';
  let importDialog: ImportFiles;
  import { importKind, type ImportFile } from '$lib/imports/files';
  import { isRecording } from '$lib/speech/recording';
  import { isApp, applyAppEdit, type AppEdit } from '$lib/apps/apps';
  import ConflictResolver from '$lib/workspace/ConflictResolver.svelte';
  import {
    reconcilePage,
    resolvePage,
    localVersion,
    type Conflict
  } from '$lib/workspace/reconcile';
  import StagedDiff from '$lib/workspace/StagedDiff.svelte';
  import ConnectionSettings from '$lib/workspace/ConnectionSettings.svelte';
  import WorkspaceNavigation from '$lib/navigation/WorkspaceNavigation.svelte';
  let view = $state<'document' | 'search' | 'settings' | 'submit'>('document');
  function navigate(next: 'search' | 'settings' | 'submit') {
    closeDrawer();
    ++readVersion;
    view = next;
    showCreate = false;
    history.replaceState(null, '', '#@' + next);
    if (next === 'submit' && online) void run(syncDrafts);
  }
  import {
    api,
    ApiError,
    editRequest,
    readonly as documentReadonly,
    type Page,
    type Draft,
    type ValidationFinding,
    type SearchResult
  } from '$lib/workspace/api';
  import {
    loadCache,
    saveCache,
    clearCache,
    cachedSearch,
    emptyCache,
    type Cache
  } from '$lib/workspace/cache';
  import {
    validateLocally,
    disposeValidation
  } from '$lib/validation/localValidation';
  import { markdownView } from '$lib/documents/markdownView';
  import CodeEditor from '$lib/documents/CodeEditor.svelte';
  let findings = $state<ValidationFinding[]>([]);
  let allowConfigEdits = $state(false);
  let allowTemplateEdits = $state(false);
  let mode = $state<'rendered' | 'code'>('rendered');
  import {
    treePaths,
    addFolder,
    moveTree,
    deleteTree
  } from '$lib/navigation/treeEdits';
  import DocumentTree from '$lib/navigation/DocumentTree.svelte';
  let cache = $state.raw<Cache>(emptyCache(''));
  const current = $derived(
    cache.drafts[cache.selected] || cache.pages[cache.selected] || null
  );
  let online = $state(false),
    busy = $state(false),
    saved = $state(true),
    showCreate = $state(false);
  let newPath = $state(''),
    query = $state(''),
    variants = $state('');
  let feedback = $state('Open a document to begin.'),
    error = $state(false),
    validated = $state('');
  let results = $state.raw<SearchResult[] | null>(null),
    searchLabel = $state('ALL DOCUMENTS');
  let fetching = $state<string[]>([]);
  let validating = $state(false);
  let validationVersion = 0;
  let readVersion = 0,
    searchVersion = 0;
  const currentPath = $derived(current?.path);
  const sourceOnly = (path?: string) =>
    !!path && (path === 'config.yaml' || /(^|\/)\.?rumdl\.toml$/.test(path));
  $effect(() => {
    if (sourceOnly(currentPath)) mode = 'code';
  });
  const currentFindings = $derived(
    findings.filter((finding) => finding.path === currentPath)
  );
  const draftCount = $derived(
    Object.keys(cache.drafts).length + Object.keys(cache.deletions || {}).length
  );
  const batch = $derived(
    editRequest(cache.summary, cache.drafts, cache.deletions)
  );
  const signature = $derived(JSON.stringify(batch.edits));
  $effect(() => {
    // With no draft left, there is no validation result to retain.
    if (!draftCount) findings = [];
  });
  $effect(() => {
    // Only content, connectivity and foreground operations schedule validation.
    // A description change does not invalidate the checked document edits.
    const edits = signature;
    const snapshotRevision = cache.validationSnapshot?.revision;
    const connected = online;
    const ready =
      !busy &&
      !reconciling &&
      !conflictPaths.length &&
      draftCount > 0 &&
      (connected || !!snapshotRevision);
    validationVersion++;
    validating = false;
    validated = '';
    submissionOnly = '';
    if (!ready) return;
    message('Changes saved locally. Waiting to validate…');
    const scheduledVersion = validationVersion;
    const timer = setTimeout(() => {
      if (signature === edits && validationVersion === scheduledVersion)
        void validate();
    }, 600);
    return () => {
      clearTimeout(timer);
      validationVersion++;
      validating = false;
    };
  });
  const title = $derived(
    current?.path
      .split('/')
      .at(-1)
      ?.replace(/\.md$/, '')
      .replaceAll('-', ' ') || 'A place for everything you know.'
  );
  const paths = $derived(treePaths(cache));
  function message(text: string, failed = false) {
    feedback = text;
    error = failed;
  }
  function failure(e: unknown) {
    if (e instanceof ApiError && e.findings.length) {
      findings = e.findings;
    }
    if (e instanceof ApiError && e.status === 401) navigate('settings');
    if (e instanceof ApiError && e.status === 0) {
      online = false;
      validated = '';
    }
    message(
      e instanceof ApiError && e.findings.length
        ? 'Validation failed. Errors are shown inline in the editor.'
        : e instanceof Error
          ? e.message
          : String(e),
      true
    );
  }
  async function run(fn: () => Promise<void>) {
    try {
      await fn();
    } catch (e) {
      failure(e);
    }
  }
  async function restoreCache(repository?: string) {
    const journal = loadCache(localStorage, repository);
    try {
      return await loadDocuments(journal);
    } catch {
      return journal;
    }
  }
  function persist() {
    try {
      saveCache(localStorage, cache);
      saved = true;
      const repository = cache.repository;
      void saveDocuments(cache).catch(() => {
        if (cache.repository === repository)
          message(
            'Drafts are saved. Offline document caching is unavailable; reconnect to load uncached documents.',
            true
          );
      });
    } catch (e) {
      saved = false;
      message(
        e instanceof Error && e.message.startsWith('Another tab')
          ? e.message
          : 'Local storage is full or unavailable. Your changes are still in this tab. Keep it open until they can be saved.',
        true
      );
    }
  }
  let baselineVersion = 0;
  async function refreshValidationSnapshot() {
    const version = ++baselineVersion;
    const inventory = await readInventory(api, cache.pages);
    if (inventory.root.repository !== cache.repository)
      throw Error('Repository changed. Reconnect before validating.');
    const snapshot =
      cache.validationSnapshot?.revision === inventory.root.revision
        ? cache.validationSnapshot
        : await buildSnapshot(
            inventory.root.revision,
            Object.fromEntries(
              Object.entries(inventory.pages)
                .filter(([, page]) => !page.asset)
                .map(([path, page]) => [path, page.text])
            )
          );
    if (
      version !== baselineVersion ||
      inventory.root.repository !== cache.repository
    )
      return;
    allowTemplateEdits = inventory.root.allow_template_edits;
    allowConfigEdits = inventory.root.allow_config_edits;
    cache = {
      ...cache,
      allowTemplateEdits,
      allowConfigEdits,
      paths: inventory.paths,
      pages: inventory.pages,
      validationSnapshot: snapshot
    };
    persist();
  }
  let reconciling = $state(false);
  const conflictPaths = $derived(
    Object.keys(cache.conflicts || {}).filter(
      (path) => localVersion(cache, path) !== undefined
    )
  );
  function acceptReconciliation(next: Cache) {
    cache = next;
    persist();
  }
  async function syncDrafts() {
    if (!online || reconciling) return;
    reconciling = true;
    const repository = cache.repository;
    try {
      const paths = [
        ...new Set([
          ...Object.keys(cache.drafts),
          ...Object.keys(cache.deletions || {})
        ])
      ];
      // Fetch the entire batch before changing any local bases.
      const pages = await Promise.all(paths.map((path) => api.page(path)));
      if (cache.repository !== repository) return;
      let next = cache;
      for (const page of pages) next = reconcilePage(next, page);
      acceptReconciliation(next);
      await refreshValidationSnapshot();
      if (Object.keys(next.conflicts || {}).length)
        message('Resolve overlapping server changes in Submit.', true);
    } finally {
      reconciling = false;
    }
  }
  function resolveConflict(
    path: string,
    conflict: Conflict,
    text: string | null
  ) {
    if (
      localVersion(cache, path) !== conflict.local ||
      cache.conflicts?.[path] !== conflict
    ) {
      message(
        'The draft changed while resolving. Check the updated conflict.',
        true
      );
      return;
    }
    acceptReconciliation(resolvePage(cache, conflict.server, text));
    validated = '';
  }
  function saveResolution(path: string, choices: Record<number, string>) {
    const conflict = cache.conflicts?.[path];
    if (!conflict) return;
    cache = {
      ...cache,
      conflicts: { ...cache.conflicts, [path]: { ...conflict, choices } }
    };
    persist();
  }
  function readonly(
    path: string,
    templates: boolean,
    config: boolean
  ): boolean {
    return (
      !!cache.pages[path]?.readonly || documentReadonly(path, templates, config)
    );
  }
  async function stageAppAction(edits: AppEdit[]) {
    if (busy) throw Error('Another operation is in progress');
    busy = true;
    try {
      const originalDrafts = cache.drafts,
        originalDeletions = cache.deletions,
        repository = cache.repository;
      const paths = new Set<string>();
      const drafts = { ...cache.drafts };
      for (const change of edits) {
        if (typeof change.path !== 'string' || paths.has(change.path))
          throw Error('An action must edit each document at most once');
        paths.add(change.path);
        if (!cache.paths.includes(change.path) && !cache.drafts[change.path])
          throw Error('Actions may only edit existing workspace documents');
        if (
          readonly(change.path, allowTemplateEdits, allowConfigEdits) ||
          change.path in (cache.deletions || {}) ||
          cache.conflicts?.[change.path]
        )
          throw Error(
            'Resolve permissions, deletion or conflicts before editing ' +
              change.path
          );
        let page: Page | undefined =
          cache.drafts[change.path] || cache.pages[change.path];
        if (!page) {
          if (!online)
            throw Error(
              'Open or cache ' + change.path + ' before editing offline'
            );
          page = await api.page(change.path);
        }
        if (!page.exists && !cache.drafts[change.path])
          throw Error('Document was deleted: ' + change.path);
        const text = applyAppEdit(page.text, change);
        drafts[change.path] = {
          ...page,
          text,
          base: cache.drafts[change.path]?.base ?? page.text
        };
      }
      if (
        cache.drafts !== originalDrafts ||
        cache.deletions !== originalDeletions ||
        cache.repository !== repository
      )
        throw Error('Workspace changed while preparing the action. Try again.');
      cache = { ...cache, drafts };
      validated = '';
      persist();
    } finally {
      busy = false;
    }
  }
  async function refresh() {
    const listing = await api.directory();
    allowTemplateEdits = listing.allow_template_edits === true;
    allowConfigEdits = listing.allow_config_edits === true;
    const changed = cache.repository !== listing.repository;
    if (changed) {
      cache = await restoreCache(listing.repository);

      validated = '';
    }
    cache = {
      ...cache,
      allowTemplateEdits,
      allowConfigEdits
    };
    online = true;
    await syncDrafts();
    if (view === 'document' && !current && cache.selected)
      await openPage(cache.selected);
  }
  async function openPage(path: string) {
    if (busy) return;
    if (path in (cache.deletions || {})) {
      navigate('submit');
      return;
    }
    const version = ++readVersion;
    const draft = cache.drafts[path];
    const cached = draft || cache.pages[path];
    if (cached) select(cached);
    if (!online) {
      if (!cached)
        throw Error(
          'This document has not been cached. Connect to the daemon to open it.'
        );
      return;
    }
    fetching = [...new Set([...fetching, path])];
    try {
      const page = await api.page(path);
      if (version !== readVersion) return;
      acceptReconciliation(reconcilePage(cache, page));
      if (cache.conflicts?.[path])
        message(
          'This document has overlapping server changes. Resolve them in Submit.',
          true
        );
      else if (!cache.drafts[path]) select(page);
    } catch (e) {
      if (!cached) throw e;
      failure(e);
    } finally {
      fetching = fetching.filter((p) => p !== path);
    }
  }
  let codeEditor = $state<CodeEditor>();
  async function openSchemaSource(path: string, line: number) {
    await openPage(path);
    mode = 'code';
    await tick();
    codeEditor?.revealLine(line);
  }
  function select(page: Page) {
    closeDrawer();
    view = 'document';
    if (
      (sourceOnly(current?.path) || isApp(page.text)) &&
      page.path !== current?.path
    )
      mode = 'rendered';
    cache = {
      ...cache,
      selected: page.path,
      pages: cache.drafts[page.path]
        ? cache.pages
        : { ...cache.pages, [page.path]: page }
    };
    persist();
    history.replaceState(null, '', `#${encodeURIComponent(page.path)}`);
  }
  function edit(text: string) {
    if (
      !current ||
      readonly(current.path, allowTemplateEdits, allowConfigEdits) ||
      busy
    )
      return;
    const draft: Draft = {
      ...current,
      base: cache.drafts[current.path]?.base ?? current.text,
      text
    };
    const drafts = { ...cache.drafts };
    if (draft.exists && text === draft.base) delete drafts[draft.path];
    else drafts[draft.path] = draft;
    cache = { ...cache, drafts };
    if (cache.conflicts?.[draft.path]) {
      cache = reconcilePage(cache, cache.conflicts[draft.path].server);
    }
    validated = '';
    persist();
  }
  async function search() {
    const version = ++searchVersion;
    if (!query.trim()) {
      results = null;
      searchLabel = 'ALL DOCUMENTS';
      return;
    }
    if (!online) {
      results = cachedSearch(cache, query);
      searchLabel = 'OFFLINE · CACHED TEXT';
      return;
    }
    searchLabel = 'SEARCHING…';
    try {
      const response = await api.search(
        query,
        variants
          .split('\n')
          .map((s) => s.trim())
          .filter(Boolean)
      );
      if (version !== searchVersion) return;
      results = response.results;
      searchLabel = `${results.length} MCP RESULTS`;
      if (response.degraded.length)
        message(`Search has reduced coverage: ${response.degraded.join(', ')}`);
    } catch (e) {
      if (version !== searchVersion) return;
      searchLabel = 'SEARCH FAILED';
      if (e instanceof ApiError && e.status === 0) {
        results = cachedSearch(cache, query);
        searchLabel = 'OFFLINE · CACHED TEXT';
      }
      throw e;
    }
  }
  async function validate(refreshBaseline = false) {
    if (busy || reconciling || conflictPaths.length || !draftCount) return;
    if (refreshBaseline && online) {
      await refreshValidationSnapshot();
      await tick();
    }
    const version = ++validationVersion;
    const checkedSignature = signature;
    validated = '';
    submissionOnly = '';
    // Keep the last completed diagnostics until this run has a replacement.
    validating = true;
    message('Validating locally…');
    const request = batch;
    try {
      const snapshot = cache.validationSnapshot;
      if (!snapshot) {
        message('Connect once to build the local validation baseline.');
        return;
      }
      const sources = Object.fromEntries(
        Object.entries(cache.pages)
          .filter(([, page]) => !page.asset)
          .map(([path, page]) => [path, page.text])
      );
      let result = await validateLocally(snapshot, request, sources);
      if (version !== validationVersion || checkedSignature !== signature)
        return;
      if (result.needs.length && online) {
        const fetched = await Promise.all(
          result.needs.map((path) => api.page(path))
        );
        if (version !== validationVersion || checkedSignature !== signature)
          return;
        const pages = { ...cache.pages };
        for (const page of fetched) {
          sources[page.path] = page.text;
          pages[page.path] = page;
        }
        cache = { ...cache, pages };
        persist();
        result = await validateLocally(snapshot, request, sources);
      }
      if (version !== validationVersion || checkedSignature !== signature)
        return;
      if (result.needs.length) {
        message(
          `Validation incomplete: ${result.needs.length} affected document(s) need current source. ${online ? 'Reconnect to refresh the baseline.' : 'Reconnect to fetch them.'}`
        );
        return;
      }
      if (result.findings.length)
        throw new ApiError('Validation failed.', 422, result.findings);
      if (result.server_required) {
        findings = [];
        submissionOnly = checkedSignature;
        message(
          `Local validation incomplete: ${result.server_required} Submission will run all server checks.`
        );
        return;
      }
      if (!result.valid)
        throw Error('Validation did not complete successfully.');
      findings = [];
      validated = checkedSignature;
      message(
        result.restart_required
          ? 'Validation passed. Listener or authentication changes require a daemon restart after submission.'
          : !online
            ? 'Validation passed offline against the cached baseline. Reconnect to submit.'
            : cache.summary.trim()
              ? 'Validation passed. Ready to submit; the daemon checks the current repository before committing.'
              : 'Validation passed.'
      );
    } catch (e) {
      if (version === validationVersion && checkedSignature === signature)
        failure(e);
    } finally {
      if (version === validationVersion) validating = false;
    }
  }
  let submitNotice = $state('');
  let submitError = $state('');
  let submissionOnly = $state('');
  const canSubmit = $derived(
    draftCount > 0 &&
      !conflictPaths.length &&
      !reconciling &&
      !!cache.summary.trim() &&
      online &&
      saved &&
      !busy &&
      !validating &&
      (validated === signature || submissionOnly === signature)
  );
  function describe(summary: string) {
    cache = { ...cache, summary };
    persist();
  }
  async function submit() {
    if (!canSubmit) return;
    const checkedSignature = signature;
    busy = true;
    submitNotice = '';
    submitError = '';
    try {
      await syncDrafts();
      await tick();
      if (conflictPaths.length || signature !== checkedSignature) {
        submitNotice = conflictPaths.length
          ? 'Resolve conflicts before submitting.'
          : 'Server changes merged. Review the updated diff while validation runs, then submit again.';
        return;
      }
      const request = { ...batch, edit_summary: cache.summary.trim() };
      // apply_edits validates the entire batch against the current repository
      // and commits it atomically; local validation is only the preview.
      const result = await api.apply(request);
      const pages = { ...cache.pages };
      const remainingPaths = new Set(cache.paths);
      for (const edit of request.edits) {
        if (edit.op === 'delete_page') {
          delete pages[edit.path];
          remainingPaths.delete(edit.path);
        } else {
          pages[edit.path] = {
            path: edit.path,
            exists: true,
            text: edit.content,
            template: cache.drafts[edit.path]?.template ?? null
          };
          remainingPaths.add(edit.path);
        }
      }
      cache = {
        ...cache,
        pages,
        paths: [...remainingPaths].sort(),
        drafts: {},
        deletions: {},
        conflicts: {},
        summary: '',
        validationSnapshot: undefined
      };
      findings = [];
      validated = '';
      persist();
      submitNotice =
        (result.status === 'already_applied'
          ? 'This change set was already submitted.'
          : 'Changes committed successfully.') +
        ({
          disabled: ' Remote push is disabled.',
          pushed: ' Pushed to the remote.',
          queued: ' Remote push is queued.',
          diverged: ' Remote push is blocked by diverged history.'
        }[result.push] || '') +
        (result.restart_required
          ? ' Restart the daemon to apply listener or authentication changes.'
          : '');
      try {
        await refresh();
      } catch (error) {
        submitNotice +=
          ' The commit succeeded, but refreshing the workspace failed. Reconnect to refresh.';
        failure(error);
      }
    } catch (e) {
      submitError = e instanceof Error ? e.message : String(e);
      try {
        await syncDrafts();
      } catch {
        /* Preserve the original submit error. */
      }
      validated = '';
      failure(e);
    } finally {
      busy = false;
    }
  }
  let treeDialog: HTMLDialogElement;
  let treeAction = $state<'folder' | 'move'>('folder');
  let treeSource = $state(''),
    treeValue = $state(''),
    treeError = $state('');
  let treeDirectory = false;
  function treeActionOpen(
    action: 'folder' | 'move',
    path: string,
    directory: boolean
  ) {
    treeAction = action;
    treeSource = path;
    treeDirectory = directory;
    treeError = '';
    treeValue = action === 'folder' ? '' : path;
    treeDialog.showModal();
  }
  let deletionUndo = $state.raw<{
    before: Cache;
    after: Cache;
    path: string;
  } | null>(null);
  const canUndoDeletion = $derived(
    !!deletionUndo &&
      cache.drafts === deletionUndo.after.drafts &&
      cache.deletions === deletionUndo.after.deletions &&
      cache.folders === deletionUndo.after.folders
  );
  async function stageDelete(path: string, directory: boolean) {
    if (busy) return;
    ++readVersion;
    busy = true;
    try {
      const sources = { ...cache.pages };
      for (const item of paths.filter(
        (item) =>
          !item.endsWith('/') &&
          (item === path || (directory && item.startsWith(path + '/')))
      )) {
        if (!cache.drafts[item] && !sources[item]) {
          if (!online) throw Error(`Cache ${item} before deleting it offline.`);
          sources[item] = await api.page(item);
        }
      }
      const before = { ...cache, pages: sources };
      const after = deleteTree(
        before,
        path,
        directory,
        sources,
        allowTemplateEdits,
        allowConfigEdits
      );
      cache = after;
      deletionUndo = { before, after, path };
      results = null;
      persist();
      navigate('submit');
    } finally {
      busy = false;
    }
  }
  function undoDeletion() {
    if (!canUndoDeletion || busy || !deletionUndo) return;
    cache = {
      ...cache,
      drafts: deletionUndo.before.drafts,
      deletions: deletionUndo.before.deletions,
      folders: deletionUndo.before.folders,
      selected: deletionUndo.before.selected
    };
    deletionUndo = null;
    persist();
  }
  async function stageMoves(
    moves: { from: string; to: string; directory: boolean }[]
  ) {
    const original = cache;
    const sources = { ...cache.pages };
    // Incoming links can occur anywhere. Keep the operation atomic if any
    // source is unavailable, rather than silently leaving stale backlinks.
    for (const path of paths.filter((path) => !path.endsWith('/'))) {
      if (!cache.drafts[path] && !sources[path]) {
        if (!online)
          throw Error(
            `Cache ${path} before moving offline so its links can be updated.`
          );
        sources[path] = await api.page(path);
      }
    }
    let next = { ...cache, pages: sources };
    for (const move of moves) {
      if (move.from === move.to) continue;
      const paths = treePaths(next).filter((path) => !path.endsWith('/'));
      const mapping = new Map(
        paths
          .filter(
            (path) =>
              path === move.from ||
              (move.directory && path.startsWith(move.from + '/'))
          )
          .map((path) => [path, move.to + path.slice(move.from.length)])
      );
      const texts = Object.fromEntries(
        paths.map((path) => [
          path,
          (next.drafts[path] || next.pages[path]).text
        ])
      );
      const references = await documentReferences(texts);
      next = rewriteSchemaReferences(
        next,
        references,
        mapping,
        allowTemplateEdits,
        allowConfigEdits
      );
      next = moveTree(
        next,
        move.from,
        move.to,
        move.directory,
        sources,
        allowTemplateEdits,
        allowConfigEdits
      );
      const afterSources = Object.fromEntries(
        treePaths(next)
          .filter((path) => !path.endsWith('/'))
          .map((path) => [path, (next.drafts[path] || next.pages[path]).text])
      );
      assertReferencesPreserved(
        references,
        await documentReferences(afterSources),
        mapping
      );
    }
    if (cache !== original)
      throw Error('Workspace changed while preparing the move. Try again.');
    cache = next;
    if (current) {
      history.replaceState(null, '', `#${encodeURIComponent(cache.selected)}`);
    }
    results = null;
  }
  async function dropMoves(
    moves: { from: string; to: string; directory: boolean }[]
  ) {
    if (busy) throw Error('Wait for the current operation to finish.');
    ++readVersion;
    busy = true;
    try {
      await stageMoves(moves);
      persist();
    } finally {
      busy = false;
    }
  }
  async function treeActionSubmit() {
    ++readVersion;
    busy = true;
    treeError = '';
    try {
      if (!cache.repository) throw Error('Connect to a repository first.');
      const value = treeValue.trim();
      if (!value) throw Error('Enter a name or path.');
      if (treeAction !== 'move' && value.includes('/'))
        throw Error('Enter a name without slashes.');
      if (treeAction === 'folder')
        cache = addFolder(cache, [treeSource, value].filter(Boolean).join('/'));
      else {
        const destination = value;
        await stageMoves([
          { from: treeSource, to: destination, directory: treeDirectory }
        ]);
      }
      persist();
      treeDialog.close();
    } catch (e) {
      treeError = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }
  async function importFiles(
    files: ImportFile[],
    report: (path: string, status: string) => void
  ) {
    if (busy) throw Error('Another operation is in progress');
    if (!cache.repository) throw Error('Connect to a repository first.');
    busy = true;
    try {
      const documents = new Map<string, string>();
      const known = [
        ...cache.paths,
        ...Object.keys(cache.drafts),
        ...Object.keys(cache.deletions || {})
      ];
      if (online) {
        const inventory = await readInventory(api, cache.pages);
        if (inventory.root.repository !== cache.repository)
          throw Error('Repository changed. Reconnect before importing.');
        known.push(...Object.keys(inventory.pages));
      }
      // Preflight the complete batch before writing anything, including local conflicts.
      for (const { path, file } of files) {
        if (
          known.some(
            (item) =>
              item === path ||
              item.startsWith(path + '/') ||
              (!item.endsWith('/') && path.startsWith(item + '/'))
          ) ||
          files.some(
            (other) => other.path !== path && other.path.startsWith(path + '/')
          )
        )
          throw Error(
            'Destination already exists or conflicts with another file: ' + path
          );
        if (importKind(path) === 'markdown') {
          if (readonly(path, allowTemplateEdits, allowConfigEdits))
            throw Error('No write access: ' + path);
          if (file.size > 8 * 1024 * 1024)
            throw Error('Markdown exceeds the 8 MiB import limit: ' + path);
          documents.set(
            path,
            new TextDecoder('utf-8', { fatal: true }).decode(
              await file.arrayBuffer()
            )
          );
        } else if (!online)
          throw Error(
            'Connect before uploading media. Markdown can be staged offline.'
          );
      }
      let failed = 0;
      for (const { path, file } of files) {
        report(path, documents.has(path) ? 'Staging…' : 'Uploading…');
        try {
          if (documents.has(path)) {
            const draft: Draft = {
              path,
              text: documents.get(path)!,
              base: '',
              exists: false,
              template: null
            };
            cache = { ...cache, drafts: { ...cache.drafts, [path]: draft } };
            validated = '';
            report(path, 'Staged');
          } else {
            const asset = await api.request<{ oid: string; size: number }>(
              '/assets?path=' + encodeURIComponent(path),
              { method: 'PUT', body: file }
            );
            const page: Page = {
              path,
              text: '',
              exists: true,
              template: null,
              readonly: true,
              hash: asset.oid,
              asset
            };
            cache = {
              ...cache,
              paths: [...new Set([...cache.paths, path])],
              pages: { ...cache.pages, [path]: page }
            };
            report(path, 'Uploaded');
          }
          persist();
        } catch (e) {
          failed++;
          report(path, 'Failed: ' + String(e));
        }
      }
      if (failed)
        throw Error(
          `${failed} imports failed. Successful imports are listed above and have been kept.`
        );
    } finally {
      busy = false;
    }
  }
  async function createHere(folder: string) {
    view = 'document';
    newPath = folder ? folder + '/' : '';
    showCreate = true;
    await tick();
    const input = document.querySelector<HTMLInputElement>('#new-path');
    input?.focus();
    input?.setSelectionRange(newPath.length, newPath.length);
  }
  async function create() {
    const path = newPath.trim();
    if (
      !path.endsWith('.md') ||
      path.split('/').includes('..') ||
      path.startsWith('/') ||
      /[{}]/.test(path) ||
      readonly(path, allowTemplateEdits, allowConfigEdits)
    )
      throw Error('Enter an explicit document path ending in .md.');
    if (!cache.repository) throw Error('Connect to a repository first.');
    let page: Page;
    if (online) page = await api.page(path);
    else
      page = cache.pages[path] || {
        path,
        exists: false,
        text: '',
        template: null
      };
    select(cache.drafts[path] || page);
    showCreate = false;
    if (!page.exists) edit(page.text);
  }
  function follow(href: string) {
    if (!href || href.startsWith('#') || !current) return;
    if (/^(https?:|mailto:)/i.test(href)) {
      window.open(href, '_blank', 'noopener,noreferrer');
      return;
    }
    const url = new URL(href, `https://mdstore.invalid/${current.path}`);
    if (url.origin === 'https://mdstore.invalid')
      void run(() => openPage(decodeURIComponent(url.pathname.slice(1))));
  }
  function linkClick(event: MouseEvent) {
    const a = (event.target as HTMLElement).closest('a');
    if (a) {
      event.preventDefault();
      follow(a.getAttribute('href') || '');
    }
  }
  function renderedLinks(node: HTMLElement) {
    node.addEventListener('click', linkClick);
    return { destroy: () => node.removeEventListener('click', linkClick) };
  }
  onMount(() => {
    const offline = () => {
      online = false;
      validated = '';
      message(
        'Offline. Reading, local rendering, and drafting remain available.'
      );
    };
    const connect = () => {
      validated = '';
      void run(refresh);
    };
    const unload = (event: BeforeUnloadEvent) => {
      if (!saved) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('offline', offline);
    window.addEventListener('online', connect);
    window.addEventListener('beforeunload', unload);
    const requestedPath = location.hash
      ? decodeURIComponent(location.hash.slice(1))
      : cache.selected;
    if (
      requestedPath === '@search' ||
      requestedPath === '@settings' ||
      requestedPath === '@submit'
    )
      view = requestedPath.slice(1) as 'search' | 'settings' | 'submit';
    void run(async () => {
      cache = await restoreCache();
      allowTemplateEdits = cache.allowTemplateEdits === true;
      allowConfigEdits = cache.allowConfigEdits === true;
      try {
        await refresh();
      } catch (e) {
        failure(e);
      }
      const path = requestedPath || cache.selected;
      if (view === 'document' && path) await openPage(path);
    });
    return () => {
      disposeValidation();
      window.removeEventListener('offline', offline);
      window.removeEventListener('online', connect);
      window.removeEventListener('beforeunload', unload);
    };
  });
</script>

<svelte:head
  ><title
    >{view === 'search'
      ? 'Search · mdstore'
      : view === 'settings'
        ? 'Settings · mdstore'
        : current
          ? `${title} · mdstore`
          : 'mdstore · Library'}</title
  ><meta
    name="description"
    content="An offline-capable Markdown workspace"
  /></svelte:head
>
<dialog bind:this={treeDialog} class="tree-dialog">
  <form
    onsubmit={(event) => {
      event.preventDefault();
      void treeActionSubmit();
    }}
  >
    <h2>
      {treeAction === 'folder' ? 'New folder' : 'Move'}
    </h2>
    <label for="tree-path"
      >{treeAction === 'move' ? 'Destination path' : 'Name'}</label
    >
    <input id="tree-path" bind:value={treeValue} disabled={busy} required />
    {#if treeError}<p role="alert">{treeError}</p>{/if}
    <p class="muted">
      {treeAction === 'folder'
        ? 'Empty folders are saved locally until they contain documents.'
        : 'This stages the move and updates document links locally.'}
    </p>
    <button type="button" disabled={busy} onclick={() => treeDialog.close()}
      >Cancel</button
    >
    <button disabled={busy}>Save</button>
  </form>
</dialog>
{#snippet sidebarToggle()}
  <button
    class="sidebar-toggle"
    aria-label="Open navigation"
    aria-expanded={drawerOpen}
    aria-controls="navigation-sidebar"
    onclick={() => (drawerOpen = true)}
  >
    <svg
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.6"
      aria-hidden="true"
      ><rect x="3" y="4" width="18" height="16" rx="2" /><path
        d="M9 4v16"
      /></svg
    >
  </button>
{/snippet}
{#if mobile && drawerOpen}<button
    class="drawer-backdrop"
    aria-label="Close navigation"
    tabindex="-1"
    onclick={closeDrawer}
  ></button>{/if}
<aside
  id="navigation-sidebar"
  class="tree-sidebar"
  class:drawer-open={drawerOpen}
  role={mobile ? 'dialog' : 'complementary'}
  aria-modal={mobile && drawerOpen ? true : undefined}
  aria-label="Navigation"
  inert={mobile && !drawerOpen}
  use:navigationDrawer={{ open: drawerOpen, mobile, close: closeDrawer }}
>
  <div class="drawer-heading">
    <span>Library</span><button
      aria-label="Close navigation"
      onclick={closeDrawer}>✕</button
    >
  </div>
  <WorkspaceNavigation
    selected={view}
    onopen={navigate}
    onimport={() => importDialog.open()}
    disabled={busy}
  />
  <nav id="documents" aria-label="Documents">
    <DocumentTree
      paths={[
        ...new Set([...paths, ...Object.keys(cache.deletions || {})])
      ].sort()}
      deleted={Object.keys(cache.deletions || {})}
      conflicts={conflictPaths}
      validation={{
        valid: draftCount
          ? validated === signature
          : !!cache.validationSnapshot,
        invalid: findings.map((finding) => finding.path),
        pending: draftCount > 0 && validated !== signature
      }}
      selected={view === 'document' ? current?.path : undefined}
      drafts={Object.keys(cache.drafts)}
      cached={Object.keys(cache.pages)}
      {fetching}
      {saved}
      {busy}
      onaction={treeActionOpen}
      onmove={dropMoves}
      ondelete={stageDelete}
      oncreate={(folder) => void createHere(folder)}
      onopen={(path) =>
        path in (cache.deletions || {}) || conflictPaths.includes(path)
          ? navigate('submit')
          : run(() => openPage(path))}
    />
  </nav>
</aside>
<ImportFiles
  bind:this={importDialog}
  {busy}
  {paths}
  onshow={closeDrawer}
  onimport={importFiles}
/>
<main class="document-main" inert={mobile && drawerOpen}>
  {#if view === 'submit'}
    <header class="document-header">
      {@render sidebarToggle()}
      <h1 class="pane-title">Submit</h1>
      <span class="muted"
        >{draftCount} staged {draftCount === 1 ? 'file' : 'files'}</span
      >
    </header>
    <div class="submit-page">
      {#if canUndoDeletion && deletionUndo}<p>
          Deleted {deletionUndo.path} locally.
          <button disabled={busy} onclick={undoDeletion}>Undo deletion</button>
        </p>{/if}
      <form
        class="submit-form"
        onsubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label for="summary">Description</label>
        <textarea
          id="summary"
          rows="3"
          value={cache.summary}
          oninput={(event) => describe(event.currentTarget.value)}
          placeholder="Describe your changes…"
          required
          disabled={busy}></textarea>
        <div class="submit-actions">
          <button id="submit" class="primary" disabled={!canSubmit}
            >{busy ? 'Submitting…' : 'Submit changes'}</button
          >
          {#if !online}<span class="muted">Reconnect to submit.</span>{/if}
        </div>
      </form>
      {#if !saved}<p class="error" role="alert">{feedback}</p>{/if}
      {#if submitNotice}<p class="submit-notice" role="status">
          {submitNotice}
        </p>{/if}
      {#if submitError}<p class="error" role="alert">
          {submitError} Your staged changes are preserved.
        </p>{/if}
      <section aria-label="Reconcile server changes">
        <div class="submit-actions">
          <h2>Server changes</h2>
          <button
            disabled={busy || reconciling || !online}
            onclick={() => run(syncDrafts)}
            >{reconciling ? 'Checking…' : 'Check server changes'}</button
          >
        </div>
        {#each conflictPaths as path (path)}
          {@const conflict = cache.conflicts![path]}
          {#key JSON.stringify( [conflict.base, conflict.local, conflict.server] )}
            <ConflictResolver
              {path}
              {conflict}
              disabled={busy || reconciling}
              onprogress={(choices) => saveResolution(path, choices)}
              onresolve={(text) =>
                resolveConflict(path, cache.conflicts![path], text)}
            />
          {/key}
        {:else}<p class="muted">No unresolved conflicts.</p>{/each}
      </section>
      <section class="validation-results" aria-label="Validation results">
        <div class="submit-actions">
          <h2>Validation</h2>
          <button
            disabled={busy || !draftCount || validating}
            onclick={() => run(() => validate(true))}>Revalidate</button
          >
        </div>
        <p role="status">
          {!draftCount
            ? 'No staged changes.'
            : validating
              ? 'Revalidating in the background…'
              : validated === signature
                ? 'Validation passed. The server validates again before committing.'
                : findings.length
                  ? 'Validation failed.'
                  : 'Validation pending or incomplete.'}
        </p>
        {#if draftCount && validated !== signature}<p class="muted">
            {feedback}
          </p>{/if}
        {#if findings.length}
          <ul aria-label="Validation errors">
            {#each findings as finding}
              <li>
                <button
                  disabled={busy}
                  onclick={() => {
                    mode = 'code';
                    void run(() => openPage(finding.path));
                  }}
                  >{finding.path}{finding.line
                    ? ':' + finding.line
                    : ''}</button
                >
                — {finding.message}
              </li>
            {/each}
          </ul>
        {/if}
      </section>
      <section aria-label="Staged diff">
        <h2>Staged diff</h2>
        {#each batch.edits as edit (edit.path)}
          <details class="staged-file" open>
            <summary
              ><strong>{edit.path}</strong><span
                >{edit.op === 'delete_page'
                  ? 'Deleted'
                  : edit.op === 'create_page'
                    ? 'Added'
                    : 'Modified'}</span
              ></summary
            >
            <StagedDiff
              path={edit.path}
              before={edit.op === 'create_page' ? null : edit.base}
              after={edit.op === 'delete_page' ? null : edit.content}
            />
          </details>
        {:else}<p class="muted">Your workspace has no staged changes.</p>{/each}
      </section>
    </div>
  {:else if view === 'settings'}
    <header class="document-header">
      {@render sidebarToggle()}
      <h1 class="pane-title">Settings</h1>
    </header>
    <div class="workspace-page">
      <ConnectionSettings onconnect={refresh} />
      <section class="review">
        <h2>Offline storage</h2>
        <p role="status">{online ? 'Connected' : 'Offline · local cache'}</p>
        <p class:error>{feedback}</p>
        <details class="cache-settings">
          <summary
            >Local storage · {Object.keys(cache.pages).length} cached</summary
          >
          <p>Documents and drafts are stored in this browser.</p>
          <button
            disabled={busy}
            onclick={() => {
              if (
                confirm(
                  'Clear cached documents and local drafts for this repository?'
                )
              ) {
                void clearDocuments(cache.repository).catch(() =>
                  message('Could not clear offline documents.', true)
                );
                clearCache(localStorage, cache.repository);
                cache = emptyCache(cache.repository);

                validated = '';
                saved = true;
              }
            }}>Clear local cache</button
          >
        </details>
      </section>
    </div>
  {:else if view === 'search'}
    <header class="document-header">
      {@render sidebarToggle()}
      <h1 class="pane-title">Search</h1>
    </header>
    <div class="workspace-page">
      <form
        onsubmit={(e) => {
          e.preventDefault();
          void run(search);
        }}
      >
        <label for="search">Search your documents</label>
        <div class="search-box">
          <input
            id="search"
            type="search"
            bind:value={query}
            placeholder={online ? 'Search with MCP…' : 'Search cached text…'}
          /><button aria-label="Search">↵</button>
        </div>
        <details class="search-options">
          <summary>Query variants</summary><label for="variants"
            >One search expansion per line</label
          ><textarea id="variants" bind:value={variants} rows="3"></textarea>
        </details>
      </form>
      <p class="muted">
        {searchLabel === 'ALL DOCUMENTS'
          ? 'Search the library using the MCP search API, or cached text when offline.'
          : searchLabel}
      </p>
      {#if results}
        <div class="search-results">
          {#each results as item}
            <button
              disabled={busy}
              onclick={() => run(() => openPage(item.path))}
            >
              <strong>{item.path}</strong>
              {#if item.excerpt}<span>{item.excerpt}</span>{/if}
            </button>
          {:else}<p>No matching documents.</p>{/each}
        </div>
      {/if}
      {#if error}<p role="alert">{feedback}</p>{/if}
    </div>
  {:else}
    <section class="document-workspace" aria-label="Document">
      <header class="document-header">
        {@render sidebarToggle()}
        <nav id="path" class="document-breadcrumb" aria-label="Document path">
          {#if current}
            {#each current.path.split('/') as part, index}
              {#if index > 0}<span class="path-separator" aria-hidden="true"
                  >/</span
                >{/if}
              <span
                aria-current={index === current.path.split('/').length - 1
                  ? 'page'
                  : undefined}>{part}</span
              >
            {/each}
          {:else}<span>Library</span>{/if}
        </nav>
        {#if current}
          <div class="document-controls">
            {#if current.template}
              <a
                class="governing-template"
                href={`#${encodeURIComponent(current.template.path)}`}
                title="Governing template"
                onclick={(event) => {
                  event.preventDefault();
                  void run(() => openPage(current!.template!.path));
                }}>{current.template.path}</a
              >
            {/if}
            <span class="word-count"
              >{current.text.trim().split(/\s+/).filter(Boolean).length} words</span
            >
            <div
              class="document-status"
              role="status"
              aria-label="Document status"
              title={feedback}
            >
              {#if error && !findings.length}<span class="status-error"
                  >Error</span
                >{/if}
              <span
                class:status-error={!saved}
                class:status-muted={!online}
                title={!saved
                  ? 'Local storage could not save your changes'
                  : !online
                    ? 'Working from the offline cache'
                    : cache.drafts[current.path]
                      ? 'Changes saved locally; pending submission'
                      : 'Up to date with the server'}
                >{!saved
                  ? 'Not saved'
                  : !online
                    ? 'Offline'
                    : cache.drafts[current.path]
                      ? 'Local'
                      : 'Synced'}</span
              >
              {#if cache.drafts[current.path]}<span class="status-modified"
                  >Modified</span
                >{/if}
              {#if currentFindings.length}<span class="status-error"
                  >Invalid</span
                >
              {:else if cache.drafts[current.path]}
                <span class:status-muted={validated !== signature}
                  >{validating
                    ? 'Validating…'
                    : validated === signature
                      ? 'Valid'
                      : 'Unchecked'}</span
                >
              {/if}
              {#if readonly(current.path, allowTemplateEdits, allowConfigEdits)}<span
                  class="status-muted">Read only</span
                >{/if}
            </div>
            <div
              class="view-switch"
              role="radiogroup"
              aria-label="Document view"
            >
              <label
                ><input
                  type="radio"
                  name="document-view"
                  value="rendered"
                  bind:group={mode}
                  disabled={sourceOnly(current.path)}
                /><span>Render</span></label
              >
              <label
                ><input
                  type="radio"
                  name="document-view"
                  value="code"
                  bind:group={mode}
                /><span>Edit</span></label
              >
            </div>
          </div>
        {/if}
      </header>
      {#if showCreate}<form
          class="connection-form"
          onsubmit={(e) => {
            e.preventDefault();
            void run(create);
          }}
        >
          <label for="new-path">New document path</label><input
            id="new-path"
            bind:value={newPath}
            placeholder="notes/my-document.md"
            required
          /><button class="primary">Create draft</button><button
            type="button"
            onclick={() => (showCreate = false)}>Cancel</button
          >
        </form>{/if}
      {#if current}
        {#key current.path}
          {#if current.asset}
            <AssetView page={current} {api} />
          {:else}
            <div class="document-views">
              <div
                class="code-layer"
                class:inactive={mode !== 'code' && !sourceOnly(current.path)}
                inert={mode !== 'code' && !sourceOnly(current.path)}
                aria-hidden={mode !== 'code' && !sourceOnly(current.path)}
              >
                <CodeEditor
                  bind:this={codeEditor}
                  onsource={(path, line) =>
                    void run(() => openSchemaSource(path, line))}
                  findings={currentFindings}
                  value={current.text}
                  path={current.path}
                  readonly={readonly(
                    current.path,
                    allowTemplateEdits,
                    allowConfigEdits
                  )}
                  disabled={busy}
                  onchange={edit}
                />
              </div>
              {#if mode === 'rendered' && !sourceOnly(current.path)}
                {#if isRecording(current.text)}
                  <SpeechReview
                    page={current}
                    {api}
                    dirty={!!cache.drafts[current.path]}
                    onstage={stageAppAction}
                    onopen={(path) => void run(() => openPage(path))}
                  />
                {:else if isApp(current.text)}
                  <AppViews
                    path={current.path}
                    {cache}
                    {online}
                    {busy}
                    onopen={(path) => void run(() => openPage(path))}
                    onstage={stageAppAction}
                  />
                {:else}
                  <article
                    id="preview"
                    class="markdown"
                    use:renderedLinks
                    use:markdownView={{
                      text: current.text,
                      schema: current.template?.definition?.frontmatter
                    }}
                  ></article>
                {/if}
              {/if}
            </div>
          {/if}
        {/key}
      {:else}<div class="empty">
          <span>▧</span>
          <h2>Your knowledge, connected.</h2>
          {#if error}<p class="error" role="alert">{feedback}</p>{/if}
          <p>Explore your library. Write in place. Keep working offline.</p>
        </div>{/if}
    </section>
  {/if}
</main>
