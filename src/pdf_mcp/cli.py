"""Command line entry point: ``pdf-mcp <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import get_settings


def _setup_logging(level: str) -> None:
    # Always log to stderr: stdout is the MCP channel in stdio mode.
    logging.basicConfig(
        level=level.upper(), stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "watchdog"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _app():
    from .service import App

    return App(get_settings())


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_serve(args) -> None:
    from .server import run
    from .watcher import InboxWatcher

    s = get_settings()
    if args.host:
        s.http_host = args.host
    if args.port:
        s.http_port = args.port
    app = _app()
    if s.watch_enabled and not args.no_watch:
        InboxWatcher(app).start()
    if s.sync_on_start:
        app.enqueue(("sync", None, False))
    run(app, args.transport)


def cmd_ingest(args) -> None:
    from .server import _doc_dict

    app = _app()
    p = Path(args.path)
    from .service import is_pdf

    targets = sorted(x for x in p.rglob("*") if x.is_file() and is_pdf(x)) if p.is_dir() else [p]
    for t in targets:
        if app.customer_for_path(t) is None:
            if not args.customer:
                sys.exit(f"{t} is outside the inbox: pass --customer CODE")
            t = app.import_file(t, args.customer)
        rec = app.ingest_file(t, force=args.force)
        d = _doc_dict(app, rec)
        print(f"[{d['status']}] {d['customer_code']}/{d['filename']} doc_id={d['doc_id']} "
              f"pages={d['page_count']} chunks={d['chunk_count']}" + (f" error={d['error']}" if d["error"] else ""))
        for w in d["warnings"]:
            print(f"    warning: {w}")


def cmd_reindex(args) -> None:
    app = _app()
    if args.recreate:
        if not args.customer:
            sys.exit("--recreate requires --customer")
        from .config import validate_customer_code

        app.store.drop_collection(validate_customer_code(args.customer))
        args.force = True
    _print(app.sync(args.customer, force=args.force))


def cmd_list(args) -> None:
    app = _app()
    if not args.customer:
        _print([{"customer_code": c, "documents": n, "ready": r} for c, n, r in app.registry.customers()])
        return
    from .config import validate_customer_code

    for r in app.registry.list(validate_customer_code(args.customer)):
        print(f"{r.doc_id}  {r.status:<10} p={r.page_count or '-':<4} {r.filename}" + (f"  ({r.error})" if r.error else ""))


def cmd_find(args) -> None:
    from . import rag

    _print(rag.find_documents(_app(), args.customer, args.query, limit=args.limit))


def cmd_search(args) -> None:
    from . import rag

    _print(rag.search_chunks(_app(), args.customer, args.query, limit=args.limit))


def cmd_ask(args) -> None:
    from . import rag

    res = rag.ask(_app(), args.customer, args.question, doc_ids=args.doc or None)
    print(res["answer"], "\n")
    for s in res["sources"]:
        print(f"[{s['n']}] {s['filename']} p.{s['page']}  {s['file_url']}")


def cmd_ref(args) -> None:
    from . import rag

    res = rag.find_reference(_app(), args.reference, customer=args.customer, limit=args.limit)
    print(f"{res['total_documents']} document(s) contain {args.reference!r} (normalized {res['normalized']!r})")
    for m in res["matches"]:
        print(f"\n{m['customer_code']}/{m['filename']}  pages={m['pages']}  x{m['occurrences']}\n  {m['snippet']}\n  {m['file_url']}")


def cmd_doctor(args) -> None:
    s = get_settings()
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            print(f"[ OK ] {name}: {fn()}")
        except Exception as e:
            ok = False
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")

    print(f"inbox: {s.inbox}\nregistry: {s.registry_path}\nfile links: {s.base_url}/files/...\n")
    from .providers import Models
    from .store import VectorStore

    models = Models(s)
    check("Qdrant " + (str(s.qdrant_path) if s.qdrant_path else str(s.qdrant_url)), lambda: VectorStore(s).ping())
    check(f"Embeddings {s.embed_provider}:{s.embed_model}", lambda: f"dim={len(models.embed(['ping'])[0])}")
    check(f"LLM {s.llm_provider}:{s.llm_model}",
          lambda: repr(models.llm.chat("Reply with exactly: OK", "Say OK", max_tokens=20)[:40]))
    if s.vision_enabled:
        def vision():
            import pymupdf

            doc = pymupdf.open()
            page = doc.new_page(width=300, height=120)
            page.insert_text((20, 60), "PDF-MCP 42", fontsize=28)
            png = page.get_pixmap(dpi=72).tobytes("png")
            return repr(models.vision.chat("Read the image.", "What text is in this image?", images=[png],
                                           max_tokens=40)[:60])
        check(f"Vision {s.vision_provider}:{s.vision_model}", vision)
    if s.ocr_enabled and s.ocr_engine == "tesseract":
        def tess():
            import pytesseract

            if s.tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = s.tesseract_cmd
            langs = pytesseract.get_languages(config="")
            missing = [l for l in s.ocr_lang.split("+") if l not in langs]
            if missing:
                raise RuntimeError(f"language data missing: {missing}; installed: {langs}")
            return f"v{pytesseract.get_tesseract_version()} langs={s.ocr_lang}"
        check("Tesseract OCR", tess)
    sys.exit(0 if ok else 1)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="pdf-mcp", description="Per-customer PDF RAG MCP server")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="run the MCP server")
    sp.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.add_argument("--no-watch", action="store_true", help="disable the inbox watcher")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("ingest", help="ingest a PDF or folder now")
    sp.add_argument("path")
    sp.add_argument("--customer", help="required when the path is outside the inbox (file is copied in)")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_ingest)

    sp = sub.add_parser("reindex", help="sync inbox with the index")
    sp.add_argument("--customer")
    sp.add_argument("--force", action="store_true", help="re-extract and re-embed everything")
    sp.add_argument("--recreate", action="store_true", help="drop the customer's collection first")
    sp.set_defaults(fn=cmd_reindex)

    sp = sub.add_parser("list", help="list customers, or documents of one customer")
    sp.add_argument("customer", nargs="?")
    sp.set_defaults(fn=cmd_list)

    for name, fn in (("find", cmd_find), ("search", cmd_search)):
        sp = sub.add_parser(name, help=f"{name} within a customer's documents")
        sp.add_argument("customer")
        sp.add_argument("query")
        sp.add_argument("--limit", type=int, default=5 if name == "find" else 8)
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("ref", help="exact lookup of an invoice/PO/contract number (all customers by default)")
    sp.add_argument("reference")
    sp.add_argument("--customer", help="limit to one customer")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_ref)

    sp = sub.add_parser("ask", help="RAG answer from a customer's documents")
    sp.add_argument("customer")
    sp.add_argument("question")
    sp.add_argument("--doc", action="append", help="restrict to doc_id (repeatable)")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("doctor", help="check Qdrant, models and Tesseract")
    sp.set_defaults(fn=cmd_doctor)

    args = p.parse_args(argv)
    _setup_logging(get_settings().log_level)
    try:
        args.fn(args)
    except ValueError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
