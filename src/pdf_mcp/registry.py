"""SQLite registry of ingested documents and their extracted page text.

The registry is the source of truth for *which* files exist per customer, their
status, and the path used to serve the original file. Qdrant holds the vectors.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    customer     TEXT NOT NULL,
    doc_id       TEXT NOT NULL,
    path         TEXT NOT NULL,
    filename     TEXT NOT NULL,
    title        TEXT,
    sha256       TEXT NOT NULL,
    size_bytes   INTEGER,
    mtime        REAL,
    page_count   INTEGER,
    chunk_count  INTEGER,
    status       TEXT NOT NULL,          -- pending | processing | ready | error
    error        TEXT,
    warnings     TEXT,
    embed_model  TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (customer, doc_id)
);
CREATE INDEX IF NOT EXISTS ix_documents_path ON documents(path);
CREATE TABLE IF NOT EXISTS pages (
    customer TEXT NOT NULL,
    doc_id   TEXT NOT NULL,
    page     INTEGER NOT NULL,
    source   TEXT,
    text     TEXT,
    PRIMARY KEY (customer, doc_id, page)
);
"""

log = logging.getLogger(__name__)

# Separators inside identifiers are dropped so INV-2024-0781, INV/2024/0781 and
# INV20240781 all normalise to "inv20240781".
# OCR often adds spaces around separators ("INV-2026- 0106"), so those are dropped too.
_ID_SEP = re.compile(r"(?<=[0-9A-Za-z])[ \t]*[-/_.:#][ \t]*(?=[0-9A-Za-z])")
# Bump when normalize_ref changes: existing indexes are rebuilt on start.
NORM_VERSION = 2


def normalize_ref(text: str) -> str:
    return _ID_SEP.sub("", text or "").lower()


COLUMNS = (
    "customer, doc_id, path, filename, title, sha256, size_bytes, mtime, page_count, "
    "chunk_count, status, error, warnings, embed_model, created_at, updated_at"
)


@dataclass
class DocRecord:
    customer: str
    doc_id: str
    path: str
    filename: str
    title: str | None
    sha256: str
    size_bytes: int | None
    mtime: float | None
    page_count: int | None
    chunk_count: int | None
    status: str
    error: str | None
    warnings: str | None
    embed_model: str | None
    created_at: float
    updated_at: float


class Registry:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()
        self.fts = self._init_fts()

    def _init_fts(self) -> bool:
        """Trigram full-text index over normalised page text (substring search)."""
        try:
            self._db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5("
                "norm, customer UNINDEXED, doc_id UNINDEXED, page UNINDEXED, tokenize='trigram')"
            )
        except sqlite3.OperationalError as e:
            log.warning("SQLite FTS5 trigram unavailable (%s); reference search will scan page text", e)
            return False
        n_pages = self._db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        n_fts = self._db.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0]
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if n_fts != n_pages or version != NORM_VERSION:  # upgrade: (re)build from stored page text
            log.info("Building reference index for %d pages", n_pages)
            self._db.execute("DELETE FROM pages_fts")
            rows = self._db.execute("SELECT customer, doc_id, page, text FROM pages").fetchall()
            self._db.executemany(
                "INSERT INTO pages_fts (norm, customer, doc_id, page) VALUES (?, ?, ?, ?)",
                [(normalize_ref(t), c, d, p) for c, d, p, t in rows],
            )
            self._db.execute(f"PRAGMA user_version = {NORM_VERSION}")
        self._db.commit()
        return True

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- documents --------------------------------------------------------
    def _rows(self, sql: str, args=()) -> list[DocRecord]:
        with self._lock:
            cur = self._db.execute(f"SELECT {COLUMNS} FROM documents {sql}", args)
            return [DocRecord(*row) for row in cur.fetchall()]

    def get(self, customer: str, doc_id: str) -> DocRecord | None:
        rows = self._rows("WHERE customer=? AND doc_id=?", (customer, doc_id))
        return rows[0] if rows else None

    def by_path(self, path: str) -> DocRecord | None:
        rows = self._rows("WHERE path=?", (path,))
        return rows[0] if rows else None

    def list(self, customer: str | None = None, status: str | None = None) -> list[DocRecord]:
        clauses, args = [], []
        if customer:
            clauses.append("customer=?")
            args.append(customer)
        if status:
            clauses.append("status=?")
            args.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._rows(f"{where} ORDER BY customer, filename", args)

    def customers(self) -> list[tuple[str, int, int]]:
        """(customer, total docs, ready docs)"""
        with self._lock:
            cur = self._db.execute(
                "SELECT customer, COUNT(*), SUM(status='ready') FROM documents GROUP BY customer ORDER BY customer"
            )
            return [(c, n, r or 0) for c, n, r in cur.fetchall()]

    def upsert(self, **fields) -> None:
        now = time.time()
        with self._lock:
            existing = self.get(fields["customer"], fields["doc_id"])
            if existing:
                sets = ", ".join(f"{k}=?" for k in fields if k not in ("customer", "doc_id"))
                vals = [v for k, v in fields.items() if k not in ("customer", "doc_id")]
                self._db.execute(
                    f"UPDATE documents SET {sets}, updated_at=? WHERE customer=? AND doc_id=?",
                    (*vals, now, fields["customer"], fields["doc_id"]),
                )
            else:
                fields.setdefault("status", "pending")
                cols = ", ".join([*fields.keys(), "created_at", "updated_at"])
                qs = ", ".join("?" * (len(fields) + 2))
                self._db.execute(f"INSERT INTO documents ({cols}) VALUES ({qs})", (*fields.values(), now, now))
            self._db.commit()

    def delete(self, customer: str, doc_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM documents WHERE customer=? AND doc_id=?", (customer, doc_id))
            self._db.execute("DELETE FROM pages WHERE customer=? AND doc_id=?", (customer, doc_id))
            if self.fts:
                self._db.execute("DELETE FROM pages_fts WHERE customer=? AND doc_id=?", (customer, doc_id))
            self._db.commit()

    # --- pages ------------------------------------------------------------
    def save_pages(self, customer: str, doc_id: str, pages: list[tuple[int, str, str]]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pages WHERE customer=? AND doc_id=?", (customer, doc_id))
            self._db.executemany(
                "INSERT INTO pages (customer, doc_id, page, source, text) VALUES (?, ?, ?, ?, ?)",
                [(customer, doc_id, p, src, txt) for p, src, txt in pages],
            )
            if self.fts:
                self._db.execute("DELETE FROM pages_fts WHERE customer=? AND doc_id=?", (customer, doc_id))
                self._db.executemany(
                    "INSERT INTO pages_fts (norm, customer, doc_id, page) VALUES (?, ?, ?, ?)",
                    [(normalize_ref(txt), customer, doc_id, p) for p, _src, txt in pages],
                )
            self._db.commit()

    def get_pages(self, customer: str, doc_id: str, start: int, end: int) -> list[tuple[int, str, str]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT page, source, text FROM pages WHERE customer=? AND doc_id=? AND page BETWEEN ? AND ? "
                "ORDER BY page",
                (customer, doc_id, start, end),
            )
            return cur.fetchall()

    def find_text(self, needle: str, customer: str | None = None, limit: int = 500) -> list[tuple[str, str, int, str]]:
        """Pages whose normalised text contains ``needle`` (already normalised).
        Returns (customer, doc_id, page, original text)."""
        with self._lock:
            if self.fts:
                sql = (
                    "SELECT f.customer, f.doc_id, f.page, p.text FROM pages_fts f "
                    "JOIN pages p ON p.customer=f.customer AND p.doc_id=f.doc_id AND p.page=f.page "
                    "WHERE pages_fts MATCH ?"
                )
                args: list = ['norm:"' + needle.replace('"', '""') + '"']
                if customer:
                    sql += " AND f.customer=?"
                    args.append(customer)
                sql += " ORDER BY f.customer, f.doc_id, f.page LIMIT ?"
                args.append(limit)
                return [(c, d, int(p), t) for c, d, p, t in self._db.execute(sql, args).fetchall()]
            sql, args = "SELECT customer, doc_id, page, text FROM pages", []
            if customer:
                sql += " WHERE customer=?"
                args.append(customer)
            out = []
            for c, d, p, t in self._db.execute(sql + " ORDER BY customer, doc_id, page", args):
                if needle in normalize_ref(t or ""):
                    out.append((c, d, int(p), t))
                    if len(out) >= limit:
                        break
            return out
