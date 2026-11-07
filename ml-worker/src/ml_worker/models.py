"""Idle eviction for the two on-demand model groups in our sequential worker."""
from contextlib import contextmanager
import gc
import sys
import threading
import time

from . import audio, recipes


def clear_speech():
    # Do not import model runtimes just to evict an unused cache.
    for module, loader in (("speech2md.moss", "load_moss_engine"),
                           ("speech2md.redimnet2", "get_redimnet2_embedder")):
        if module in sys.modules:
            getattr(sys.modules[module], loader).cache_clear()
    gc.collect()
    if "mlx.core" in sys.modules:
        sys.modules["mlx.core"].clear_cache()
    torch = sys.modules.get("torch")
    if torch is not None and torch.backends.mps.is_available():
        torch.mps.empty_cache()


class Models:
    def __init__(self, idle_seconds=300):
        self.idle_seconds = idle_seconds
        self.groups = {audio.RECIPE: audio.close_denoiser, recipes.SPEECH_RECIPE: clear_speech}
        self.locks = {recipe: threading.Lock() for recipe in self.groups}
        self.finished = {}
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._maintain, daemon=True)
        self.thread.start()

    @contextmanager
    def use(self, recipe):
        if recipe not in self.groups:
            yield
            return
        with self.locks[recipe]:
            try:
                yield
            finally:
                self.finished[recipe] = time.monotonic()

    def evict(self):
        for recipe, clear in self.groups.items():
            if not self.locks[recipe].acquire(blocking=False):
                continue
            try:
                finished = self.finished.get(recipe)
                if finished is not None and time.monotonic() - finished >= self.idle_seconds:
                    clear()
                    del self.finished[recipe]
            finally:
                self.locks[recipe].release()

    def _maintain(self):
        while not self.stopped.wait(1):
            try:
                self.evict()
            except Exception as error:
                print(f"worker model eviction: {error}", file=sys.stderr)

    def close(self):
        self.stopped.set()
        self.thread.join()
        for clear in self.groups.values():
            clear()
