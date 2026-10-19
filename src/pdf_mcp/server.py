"""MCP server definition (tools + HTTP file route) and transport runners."""

from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from . import __version__, rag
from .config import Settings, validate_customer_code
from .registry import DocRecord
from .service import App

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
pdf-mcp manages PDF documents that are segregated per customer (customer code = folder name in the inbox).
Every query tool requires `customer_code`; data never crosses customers.

Typical workflow:
1. `list_customers` to discover customer codes, `list_documents` to see a customer's files.
2. `find_documents` to get the documents most related to a query (with links to the original PDF).
3. `search` to get the exact passages (with page numbers) and reason over them yourself, or
   `ask` to have the server's own LLM answer with citations.
4. `read_pages` to read full extracted text of specific pages when you need more context.
5. `find_reference` for exact identifiers (invoice, PO, contract numbers) — semantic search is
   unreliable for those. Separators are ignored (INV-0781 == INV/0781 == INV0781).
New PDFs dropped in inbox/<CUSTOMER>/ are ingested automatically; `ingest_pdf` imports a file explicitly.
Always give the user the `file_url` when they need the original document."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Write tools: all act only on this server's own inbox/index (closed world).
INGEST = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# reindex drops index entries for PDFs no longer in the inbox, and force=true rebuilds embeddings.
REINDEX = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)


def _doc_dict(app: App, rec: DocRecord) -> dict[str, Any]:
    return {
        "doc_id": rec.doc_id,
        "customer_code": rec.customer,
        "filename": rec.filename,
        "title": rec.title,
        "status": rec.status,
        "page_count": rec.page_count,
        "chunk_count": rec.chunk_count,
        "size_bytes": rec.size_bytes,
        "error": rec.error,
        "warnings": json.loads(rec.warnings) if rec.warnings else [],
        "file_url": app.file_url(rec),
        "local_path": rec.path,
    }


def create_server(app: App) -> MCPServer:
    mcp = MCPServer(name="pdf-mcp", version=__version__, instructions=INSTRUCTIONS, log_level=app.s.log_level.upper())

    # ------------------------------------------------------------ discovery
    @mcp.tool(annotations=READ_ONLY)
    def list_customers() -> dict[str, Any]:
        """List customer codes known to the server (inbox folders and ingested documents) with document counts."""
        counts = {c: (n, r) for c, n, r in app.registry.customers()}
        folders = {d.name.upper() for d in app.s.inbox.iterdir() if d.is_dir()} if app.s.inbox.exists() else set()
        out = []
        for code in sorted(set(counts) | folders):
            n, r = counts.get(code, (0, 0))
            out.append({"customer_code": code, "documents": n, "ready": r})
        return {"customers": out, "pending_jobs": app.pending_jobs()}

    @mcp.tool(annotations=READ_ONLY)
    def list_documents(customer_code: str, status: str | None = None) -> dict[str, Any]:
        """List a customer's documents. Optional status filter: pending, processing, ready, error."""
        cust = validate_customer_code(customer_code)
        docs = [_doc_dict(app, r) for r in app.registry.list(cust, status)]
        return {"customer_code": cust, "count": len(docs), "documents": docs}

    @mcp.tool(annotations=READ_ONLY)
    def get_document(customer_code: str, doc_id: str) -> dict[str, Any]:
        """Get metadata, ingestion status/warnings and the original file link for one document."""
        cust = validate_customer_code(customer_code)
        rec = app.registry.get(cust, doc_id)
        if not rec:
            raise ValueError(f"Document {doc_id} not found for customer {cust}")
        return _doc_dict(app, rec)

    # ------------------------------------------------------------ retrieval
    @mcp.tool(annotations=READ_ONLY)
    def find_documents(customer_code: str, query: str, limit: int = 5) -> dict[str, Any]:
        """Find the documents of one customer that are most related to a natural-language query.

        Returns documents ranked by relevance with matching pages, the best snippet and links to the
        original PDF (file_url_best_page opens at the most relevant page)."""
        docs = rag.find_documents(app, customer_code, query, limit=max(1, min(limit, 25)))
        return {"customer_code": validate_customer_code(customer_code), "query": query, "documents": docs}

    @mcp.tool(annotations=READ_ONLY)
    def search(customer_code: str, query: str, limit: int = 8, doc_ids: list[str] | None = None) -> dict[str, Any]:
        """Semantic search over one customer's documents; returns passages with page citations.

        Use this to reason over the evidence yourself. Optionally restrict to specific doc_ids."""
        hits = rag.search_chunks(app, customer_code, query, limit=max(1, min(limit, 50)), doc_ids=doc_ids)
        return {"customer_code": validate_customer_code(customer_code), "query": query, "results": hits}

    @mcp.tool(annotations=READ_ONLY)
    def ask(customer_code: str, question: str, doc_ids: list[str] | None = None, top_k: int = 8) -> dict[str, Any]:
        """Answer a question with the server's own LLM (RAG) using only this customer's documents.

        Returns the answer with [n] citations and the numbered sources (filename, page, file_url).
        Restrict to specific documents with doc_ids to interrogate a single document."""
        return rag.ask(app, customer_code, question, doc_ids=doc_ids, top_k=max(1, min(top_k, 30)))

    @mcp.tool(annotations=READ_ONLY)
    def read_pages(customer_code: str, doc_id: str, start_page: int = 1, end_page: int | None = None) -> dict[str, Any]:
        """Read the extracted text (including OCR and figure descriptions) of a page range (max 20 pages)."""
        cust = validate_customer_code(customer_code)
        rec = app.registry.get(cust, doc_id)
        if not rec:
            raise ValueError(f"Document {doc_id} not found for customer {cust}")
        start = max(1, start_page)
        end = min(end_page or start, start + 19, rec.page_count or start)
        pages = app.registry.get_pages(cust, doc_id, start, end)
        return {
            "doc_id": doc_id, "filename": rec.filename, "page_count": rec.page_count,
            "pages": [{"page": p, "source": s, "text": t, "file_url": app.file_url(rec, p)} for p, s, t in pages],
        }

    @mcp.tool(annotations=READ_ONLY)
    def find_reference(customer_code: str, reference: str, limit: int = 20) -> dict[str, Any]:
        """Exact lookup of an identifier (invoice no., PO no., contract no., NIK, etc.) in one customer's
        documents. Ignores separators and case; partial numbers match (0781 finds INV-2024-0781).
        Returns documents, pages, occurrence counts, a snippet and file links."""
        return rag.find_reference(app, reference, customer=customer_code, limit=max(1, min(limit, 100)))

    if app.s.allow_global_search:
        @mcp.tool(annotations=READ_ONLY)
        def find_reference_all_customers(reference: str, limit: int = 20) -> dict[str, Any]:
            """Exact lookup of an identifier (e.g. an invoice number) across ALL customers.
            Use when the customer is unknown; each match includes its customer_code. Ignores separators
            and case; partial numbers match."""
            return rag.find_reference(app, reference, customer=None, limit=max(1, min(limit, 100)))

    # ------------------------------------------------------------ management
    @mcp.tool(annotations=INGEST)
    def ingest_pdf(path: str, customer_code: str | None = None, wait: bool = True, force: bool = False) -> dict[str, Any]:
        """Ingest a PDF that is on the server's filesystem.

        If `path` is already inside inbox/<CUSTOMER>/, the customer comes from the folder. Otherwise
        `customer_code` is required and the file is copied into inbox/<customer_code>/ first.
        wait=false queues it and returns immediately (check with get_document). force=true re-extracts."""
        p = Path(path).expanduser()
        cust = app.customer_for_path(p)
        if cust is None:
            if not customer_code:
                raise ValueError("customer_code is required for files outside the inbox")
            p = app.import_file(p, customer_code)
        elif customer_code and validate_customer_code(customer_code) != cust:
            raise ValueError(f"File is in customer folder {cust}, not {customer_code}")
        if not wait:
            app.enqueue_ingest(p, force=force)
            return {"queued": True, "path": str(p.resolve())}
        return _doc_dict(app, app.ingest_file(p, force=force))

    @mcp.tool(annotations=REINDEX)
    def reindex(customer_code: str | None = None, force: bool = False) -> dict[str, Any]:
        """Rescan the inbox (one customer or all) in the background: ingest new/changed PDFs, drop deleted ones.
        force=true re-extracts and re-embeds everything (slow; use after changing models)."""
        cust = validate_customer_code(customer_code) if customer_code else None
        app.enqueue(("sync", cust, force))
        return {"queued": True, "customer_code": cust, "force": force}

    @mcp.tool(annotations=DELETE)
    def delete_document(customer_code: str, doc_id: str, delete_file: bool = False) -> dict[str, Any]:
        """Remove a document from the index. delete_file=true also deletes the PDF from the inbox
        (otherwise the watcher would re-ingest it on the next reindex)."""
        rec = app.delete_document(customer_code, doc_id, delete_file=delete_file)
        return {"deleted": True, "doc_id": rec.doc_id, "filename": rec.filename, "file_deleted": delete_file}

    # ------------------------------------------------------------ HTTP routes
    for path, endpoint in file_routes(app):
        mcp.custom_route(path, methods=["GET", "HEAD"])(endpoint)
    return mcp


def file_routes(app: App):
    async def serve_file(request: Request) -> Response:
        try:
            cust = validate_customer_code(request.path_params["customer"])
        except ValueError:
            return JSONResponse({"error": "invalid customer"}, status_code=400)
        rec = app.registry.get(cust, request.path_params["doc_id"])
        if not rec or not Path(rec.path).is_file():
            return JSONResponse({"error": "document not found"}, status_code=404)
        return FileResponse(rec.path, media_type="application/pdf", filename=rec.filename,
                            content_disposition_type="inline")

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__, "pending_jobs": app.pending_jobs()})

    return [
        ("/files/{customer}/{doc_id}", serve_file),
        ("/files/{customer}/{doc_id}/{filename:path}", serve_file),
        ("/health", health),
    ]


# ---------------------------------------------------------------- runners
def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def start_file_server_thread(app: App) -> None:
    """In stdio mode, serve /files on http_port so links in results open the original PDF."""
    import uvicorn

    s: Settings = app.s
    if not _port_free(s.http_host, s.http_port):
        log.warning("Port %s:%d busy (another pdf-mcp instance?); file links rely on it.", s.http_host, s.http_port)
        return
    starlette_app = Starlette(routes=[Route(p, e, methods=["GET", "HEAD"]) for p, e in file_routes(app)])
    config = uvicorn.Config(starlette_app, host=s.http_host, port=s.http_port, log_config=None,
                            access_log=False, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="pdf-mcp-files", daemon=True).start()
    log.info("File server on %s", s.base_url)


def run(app: App, transport: str) -> None:
    mcp = create_server(app)
    if transport == "stdio":
        if app.s.file_server_in_stdio:
            start_file_server_thread(app)
        mcp.run("stdio")
    else:
        log.info("MCP streamable HTTP on http://%s:%d/mcp ; files at %s/files/...",
                 app.s.http_host, app.s.http_port, app.s.base_url)
        mcp.run("streamable-http", host=app.s.http_host, port=app.s.http_port)
