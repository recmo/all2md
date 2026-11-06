/** One shared connection per tab. Every connection starts with current state. */
export type Change = { revision: string; jobs: number };
const listeners = new Set<(change: Change) => void>();
let source: EventSource | undefined;
let latest: Change | undefined;

export function reconnectChanges() {
  source?.close();
  source = undefined;
  latest = undefined;
  if (!listeners.size || !navigator.onLine) return;
  source = new EventSource('/mcp/events');
  source.addEventListener('change', (event) => {
    const value: Change = JSON.parse(event.data);
    if (typeof value.revision !== 'string' || typeof value.jobs !== 'number')
      return;
    latest = value;
    for (const listener of listeners) listener(value);
  });
}
export function subscribeChanges(listener: (change: Change) => void) {
  const first = !listeners.size;
  listeners.add(listener);
  if (first) {
    window.addEventListener('online', reconnectChanges);
    window.addEventListener('offline', reconnectChanges);
    reconnectChanges();
  } else if (latest) listener(latest);
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      source?.close();
      source = undefined;
      latest = undefined;
      window.removeEventListener('online', reconnectChanges);
      window.removeEventListener('offline', reconnectChanges);
    }
  };
}
