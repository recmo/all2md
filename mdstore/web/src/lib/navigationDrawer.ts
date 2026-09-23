// Keep the tree mounted so expansion, selection and scroll survive closing.
export function navigationDrawer(node: HTMLElement, initial: { open: boolean; mobile: boolean; close: () => void }) {
  let options = initial;
  let unlock: (() => void) | undefined;
  let start: { x: number; y: number } | undefined;
  function focusable(root: HTMLElement | ShadowRoot): HTMLElement[] {
    const result: HTMLElement[] = [];
    for (const element of root.querySelectorAll<HTMLElement>('*')) {
      if (element.tabIndex >= 0 && !element.matches(':disabled') && element.getClientRects().length && !element.closest('[inert]')) result.push(element);
      if (element.shadowRoot) result.push(...focusable(element.shadowRoot));
    }
    return result;
  }
  function keydown(event: KeyboardEvent) {
    if (!unlock || document.querySelector('dialog[open]')) return;
    if (event.key === 'Escape') { event.preventDefault(); options.close(); }
    if (event.key !== 'Tab') return;
    const items = focusable(node);
    const active = event.composedPath()[0];
    const index = items.indexOf(active as HTMLElement);
    if (event.shiftKey && index <= 0) { event.preventDefault(); items.at(-1)?.focus(); }
    else if (!event.shiftKey && (index < 0 || index === items.length - 1)) { event.preventDefault(); items[0]?.focus(); }
  }
  function touchstart(event: TouchEvent) {
    const touch = event.touches[0];
    start = event.touches.length === 1 ? { x: touch.clientX, y: touch.clientY } : undefined;
  }
  function touchend(event: TouchEvent) {
    const touch = event.changedTouches[0];
    if (unlock && start && start.x - touch.clientX > 70 && Math.abs(start.y - touch.clientY) < 50) options.close();
    start = undefined;
  }
  function update(next: typeof initial) {
    options = next;
    if (next.mobile && next.open && !unlock) {
      const scroll = window.scrollY;
      const old = { position: document.body.style.position, top: document.body.style.top, width: document.body.style.width, overflow: document.body.style.overflow };
      Object.assign(document.body.style, { position: 'fixed', top: `-${scroll}px`, width: '100%', overflow: 'hidden' });
      unlock = () => { Object.assign(document.body.style, old); window.scrollTo(0, scroll); };
      requestAnimationFrame(() => { if (unlock) focusable(node)[0]?.focus({ preventScroll: true }); });
    } else if ((!next.open || !next.mobile) && unlock) {
      unlock(); unlock = undefined;
    }
  }
  document.addEventListener('keydown', keydown, true);
  node.addEventListener('touchstart', touchstart, { passive: true });
  node.addEventListener('touchend', touchend, { passive: true });
  update(initial);
  return { update, destroy() {
    unlock?.();
    document.removeEventListener('keydown', keydown, true);
    node.removeEventListener('touchstart', touchstart);
    node.removeEventListener('touchend', touchend);
  } };
}
