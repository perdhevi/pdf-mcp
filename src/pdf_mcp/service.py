"""Application service: ingestion pipeline, background worker and file links.

Customer isolation rule: the customer code is the first folder under the inbox,
e.g. ``inbox/ACME/contracts/2024/msa.pdf`` belongs to customer ``ACME``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote

from .chunking import chunk_pages
from .config import Settings, validate_customer_code
from .extract import Extractor
from .providers import Models
from .registry import DocRecord, Registry
from .store import VectorStore

log = logging.getLogger(__name__)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf" and not path.name.startswith(("~$", "."))


class App:
    def __init__(self, settings: Settings, models: Models | None = None, store: VectorStore | None = None):
        settings.ensure_dirs()
        self.s = settings
        self.models = models or Models(settings)
        self.registry = Registry(settings.registry_path)
        self.store = store or VectorStore(settings)
        self.extractor = Extractor(settings, self.models)
        self._ingest_lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue()
        self._queued: set[tuple] = set()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ paths
    def customer_for_path(self, path: Path) -> str | None:
        try:
            rel = path.resolve().relative_to(self.s.inbox)
        except ValueError:
            return None
        if len(rel.parts) < 2:
            return None  # file directly in inbox root: no customer
        try:
            return validate_customer_code(rel.parts[0])
        except ValueError:
            return None

    def customer_dir(self, customer: str) -> Path:
        customer = validate_customer_code(customer)
        # Re-use an existing folder whose name matches case-insensitively.
        if self.s.inbox.exists():
            for d in self.s.inbox.iterdir():
                if d.is_dir() and d.name.upper() == customer:
                    return d
        d = self.s.inbox / customer
        d.mkdir(parents=True, exist_ok=True)
        return d

    def file_url(self, rec: DocRecord, page: int | None = None) -> str:
        url = f"{self.s.base_url}/files/{quote(rec.customer)}/{rec.doc_id}/{quote(rec.filename)}"
        return f"{url}#page={page}" if page else url

    # -------------------------------------------------------------- ingestion
    def ingest_file(self, path: Path, force: bool = False) -> DocRecord:
        """Ingest one PDF located inside ``inbox/<CUSTOMER>/``. Idempotent."""
        path = path.resolve()
        customer = self.customer_for_path(path)
        if not customer:
            raise ValueError(f"{path} is not inside a customer folder under {self.s.inbox}")
        if not path.is_file() or not is_pdf(path):
            raise ValueError(f"{path} is not a PDF file")

        with self._ingest_lock:
            sha = sha256_file(path)
            doc_id = sha[:16]
            spath = str(path)
            stat = path.stat()

            # Same path, different content -> drop the old version first.
            old = self.registry.by_path(spath)
            if old and old.doc_id != doc_id:
                log.info("Content changed for %s; replacing %s", path.name, old.doc_id)
                self._delete(old.customer, old.doc_id)

            existing = self.registry.get(customer, doc_id)
            if existing and not force:
                if existing.path != spath:
                    if Path(existing.path).exists():
                        log.info("%s is a duplicate of %s; skipped", path, existing.path)
                        return existing
                    log.info("Relocated %s -> %s", existing.path, spath)
                    self.registry.upsert(customer=customer, doc_id=doc_id, path=spath, filename=path.name)
                    existing = self.registry.get(customer, doc_id)
                if existing.status == "ready" and existing.embed_model == self.s.embed_model:
                    return existing

            self.registry.upsert(
                customer=customer, doc_id=doc_id, path=spath, filename=path.name, sha256=sha,
                size_bytes=stat.st_size, mtime=stat.st_mtime, status="processing", error=None,
            )
            try:
                return self._process(customer, doc_id, path)
            except Exception as e:
                log.exception("Ingestion failed for %s", path)
                self.registry.upsert(customer=customer, doc_id=doc_id, status="error", error=str(e)[:2000])
                return self.registry.get(customer, doc_id)

    def _process(self, customer: str, doc_id: str, path: Path) -> DocRecord:
        t0 = time.time()
        log.info("Ingesting %s/%s (%s)", customer, path.name, doc_id)
        doc = self.extractor.extract(path)
        chunks = chunk_pages(doc.pages, self.s.chunk_size, self.s.chunk_overlap)
        self.registry.save_pages(customer, doc_id, [(p.page, p.source, p.full_text) for p in doc.pages])

        self.store.delete_doc(customer, doc_id)
        if chunks:
            # Prefix with the title so the embedding carries document context.
            vectors = self.models.embed([f"{doc.title}\n\n{c.text}" for c in chunks])
            self.store.ensure_collection(customer, len(vectors[0]))
            payloads = [
                {
                    "doc_id": doc_id, "customer": customer, "filename": path.name, "title": doc.title,
                    "page": c.page, "chunk_index": c.index, "source": c.source, "text": c.text,
                }
                for c in chunks
            ]
            self.store.upsert(customer, doc_id, vectors, payloads)
        else:
            doc.warnings.append("No text could be extracted (enable OCR / check Tesseract install).")

        self.registry.upsert(
            customer=customer, doc_id=doc_id, title=doc.title, page_count=doc.page_count,
            chunk_count=len(chunks), status="ready", error=None, embed_model=self.s.embed_model,
            warnings=json.dumps(doc.warnings) if doc.warnings else None,
        )
        log.info("Ingested %s/%s: %d pages, %d chunks in %.1fs",
                 customer, path.name, doc.page_count, len(chunks), time.time() - t0)
        return self.registry.get(customer, doc_id)

    def import_file(self, source: Path, customer: str) -> Path:
        """Copy a PDF from anywhere into ``inbox/<CUSTOMER>/`` and return its new path."""
        source = source.resolve()
        if not source.is_file() or not is_pdf(source):
            raise ValueError(f"{source} is not a PDF file")
        target_dir = self.customer_dir(customer)
        target = target_dir / source.name
        if target.exists() and target.resolve() != source:
            if sha256_file(target) == sha256_file(source):
                return target
            stem, n = source.stem, 1
            while target.exists():
                target = target_dir / f"{stem} ({n}){source.suffix}"
                n += 1
        if target.resolve() != source:
            shutil.copy2(source, target)
        return target

    def _delete(self, customer: str, doc_id: str) -> None:
        self.store.delete_doc(customer, doc_id)
        self.registry.delete(customer, doc_id)

    def delete_document(self, customer: str, doc_id: str, delete_file: bool = False) -> DocRecord:
        customer = validate_customer_code(customer)
        rec = self.registry.get(customer, doc_id)
        if not rec:
            raise ValueError(f"Document {doc_id} not found for customer {customer}")
        with self._ingest_lock:
            self._delete(customer, doc_id)
            if delete_file:
                Path(rec.path).unlink(missing_ok=True)
        return rec

    def remove_path(self, path: Path) -> None:
        with self._ingest_lock:
            rec = self.registry.by_path(str(path.resolve()))
            if rec and not Path(rec.path).exists():
                log.info("File removed: %s; deleting %s/%s", path, rec.customer, rec.doc_id)
                self._delete(rec.customer, rec.doc_id)

    def move_path(self, src: Path, dest: Path) -> None:
        with self._ingest_lock:
            rec = self.registry.by_path(str(src.resolve()))
            if rec and is_pdf(dest) and self.customer_for_path(dest) == rec.customer:
                self.registry.upsert(customer=rec.customer, doc_id=rec.doc_id,
                                     path=str(dest.resolve()), filename=dest.name)
                return
        self.remove_path(src)
        if is_pdf(dest):
            self.enqueue_ingest(dest)

    def sync(self, customer: str | None = None, force: bool = False) -> dict:
        """Reconcile the inbox with the registry: ingest new/changed, drop deleted."""
        root = self.customer_dir(customer) if customer else self.s.inbox
        seen, results = set(), {"ingested": 0, "unchanged": 0, "errors": 0, "removed": 0}
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not is_pdf(path) or not self.customer_for_path(path):
                continue
            seen.add(str(path.resolve()))
            before = self.registry.by_path(str(path.resolve()))
            rec = self.ingest_file(path, force=force)
            if rec.status == "error":
                results["errors"] += 1
            elif before and before.status == "ready" and before.updated_at == rec.updated_at:
                results["unchanged"] += 1
            else:
                results["ingested"] += 1
        cust = validate_customer_code(customer) if customer else None
        for rec in self.registry.list(cust):
            if rec.path not in seen and not Path(rec.path).exists():
                self._delete(rec.customer, rec.doc_id)
                results["removed"] += 1
        return results

    # ----------------------------------------------------------------- worker
    def enqueue_ingest(self, path: Path, force: bool = False) -> None:
        self._put(("ingest", str(path.resolve()), force))

    def enqueue(self, item: tuple) -> None:
        self._put(item)

    def _put(self, item: tuple) -> None:
        if item in self._queued:
            return
        self._queued.add(item)
        self._queue.put(item)
        self.start_worker()

    def start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run_worker, name="pdf-mcp-ingest", daemon=True)
        self._worker.start()

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            self._queued.discard(item)
            try:
                kind = item[0]
                if kind == "ingest":
                    p = Path(item[1])
                    if p.exists():
                        self.ingest_file(p, force=item[2])
                elif kind == "remove":
                    self.remove_path(Path(item[1]))
                elif kind == "move":
                    self.move_path(Path(item[1]), Path(item[2]))
                elif kind == "sync":
                    log.info("Sync finished: %s", self.sync(item[1], force=item[2]))
            except Exception:
                log.exception("Worker failed on %s", item)
            finally:
                self._queue.task_done()

    def pending_jobs(self) -> int:
        return self._queue.qsize()

    def wait_idle(self, timeout: float = 600) -> None:
        end = time.time() + timeout
        while (self._queue.unfinished_tasks > 0) and time.time() < end:
            time.sleep(0.2)

    def stop(self) -> None:
        self._stop.set()
