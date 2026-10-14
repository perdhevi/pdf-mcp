"""Watch ``inbox/<CUSTOMER>/`` for PDFs being added, changed, moved or deleted.

Events are debounced: a file is only ingested once its size has been stable for
``watch_debounce_seconds`` (so half-copied files are not processed).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from .service import App, is_pdf

log = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "InboxWatcher"):
        self.w = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.w.touch(Path(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.w.touch(Path(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        p = Path(event.src_path)
        if event.is_directory or is_pdf(p):
            self.w.app.enqueue(("remove", str(p)))
            if event.is_directory:
                self.w.app.enqueue(("sync", None, False))

    def on_moved(self, event: FileSystemEvent) -> None:
        src, dest = Path(event.src_path), Path(event.dest_path)
        if event.is_directory:
            self.w.app.enqueue(("sync", None, False))
        elif is_pdf(src) or is_pdf(dest):
            self.w.app.enqueue(("move", str(src), str(dest)))


class InboxWatcher:
    def __init__(self, app: App):
        self.app = app
        self._pending: dict[Path, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        obs_cls = PollingObserver if app.s.watch_polling else Observer
        self._observer = obs_cls()
        self._observer.schedule(_Handler(self), str(app.s.inbox), recursive=True)
        self._ticker = threading.Thread(target=self._tick, name="pdf-mcp-debounce", daemon=True)

    def touch(self, path: Path) -> None:
        if not is_pdf(path):
            return
        with self._lock:
            self._pending[path] = (time.time(), -1)

    def _tick(self) -> None:
        while not self._stop.wait(1.0):
            now = time.time()
            ready: list[Path] = []
            with self._lock:
                for path, (t, size) in list(self._pending.items()):
                    try:
                        cur = path.stat().st_size
                    except OSError:
                        self._pending.pop(path, None)
                        continue
                    if cur != size:
                        self._pending[path] = (now, cur)
                    elif now - t >= self.app.s.watch_debounce_seconds:
                        ready.append(path)
                        self._pending.pop(path, None)
            for p in ready:
                log.info("Detected new/changed PDF: %s", p)
                self.app.enqueue_ingest(p)

    def start(self) -> None:
        self._observer.daemon = True
        self._observer.start()
        self._ticker.start()
        log.info("Watching %s (%s)", self.app.s.inbox, "polling" if self.app.s.watch_polling else "native events")

    def stop(self) -> None:
        self._stop.set()
        self._observer.stop()
