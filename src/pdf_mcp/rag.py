"""Retrieval and server-side question answering, always scoped to one customer."""

from __future__ import annotations

import re
from collections import OrderedDict

from .config import validate_customer_code
from .registry import normalize_ref
from .service import App
from .store import Hit

ANSWER_SYSTEM = """You answer questions about a customer's documents using ONLY the numbered sources provided.
Rules:
- Cite every claim with the source number in square brackets, e.g. [1] or [2][3].
- If the sources do not contain the answer, say so plainly; do not guess or use outside knowledge.
- Quote exact figures, dates, parties and clause numbers when they are present.
- Answer in the same language as the question. Be concise."""


def _snippet(text: str, n: int = 320) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def _check_doc_ids(app: App, customer: str, doc_ids: list[str] | None) -> None:
    for d in doc_ids or []:
        if not app.registry.get(customer, d):
            raise ValueError(f"Document {d} not found for customer {customer}")


def retrieve(app: App, customer: str, query: str, limit: int, doc_ids: list[str] | None = None) -> list[Hit]:
    customer = validate_customer_code(customer)
    if not query.strip():
        raise ValueError("query must not be empty")
    _check_doc_ids(app, customer, doc_ids)
    vector = app.models.embed([query])[0]
    return app.store.search(customer, vector, limit=limit, doc_ids=doc_ids)


def _hit_dict(app: App, customer: str, h: Hit, full_text: bool) -> dict:
    rec = app.registry.get(customer, h.doc_id)
    return {
        "doc_id": h.doc_id,
        "filename": h.filename,
        "title": h.title,
        "page": h.page,
        "score": round(h.score, 4),
        "text": h.text if full_text else _snippet(h.text),
        "file_url": app.file_url(rec, h.page) if rec else None,
    }


def search_chunks(app: App, customer: str, query: str, limit: int = 8,
                  doc_ids: list[str] | None = None) -> list[dict]:
    customer = validate_customer_code(customer)
    hits = retrieve(app, customer, query, limit, doc_ids)
    return [_hit_dict(app, customer, h, full_text=True) for h in hits]


def find_documents(app: App, customer: str, query: str, limit: int = 5) -> list[dict]:
    customer = validate_customer_code(customer)
    hits = retrieve(app, customer, query, limit=min(100, max(20, limit * 8)))
    grouped: OrderedDict[str, list[Hit]] = OrderedDict()
    for h in hits:
        grouped.setdefault(h.doc_id, []).append(h)
    docs = []
    for doc_id, hs in grouped.items():
        rec = app.registry.get(customer, doc_id)
        if not rec:
            continue
        best = max(hs, key=lambda h: h.score)
        # Best chunk dominates; extra matching chunks add a small bonus.
        score = best.score + 0.02 * min(len(hs) - 1, 5)
        pages = sorted({h.page for h in hs})
        docs.append({
            "doc_id": doc_id,
            "filename": rec.filename,
            "title": rec.title,
            "relevance": round(score, 4),
            "matching_pages": pages,
            "best_page": best.page,
            "best_snippet": _snippet(best.text),
            "page_count": rec.page_count,
            "file_url": app.file_url(rec),
            "file_url_best_page": app.file_url(rec, best.page),
        })
    docs.sort(key=lambda d: d["relevance"], reverse=True)
    return docs[:limit]


def ask(app: App, customer: str, question: str, doc_ids: list[str] | None = None, top_k: int = 8) -> dict:
    customer = validate_customer_code(customer)
    hits = retrieve(app, customer, question, limit=top_k, doc_ids=doc_ids)
    if not hits:
        return {"answer": "No indexed content was found for this customer (or document filter).", "sources": []}
    blocks, sources = [], []
    for i, h in enumerate(hits, 1):
        label = f"[{i}] {h.filename} — page {h.page}"
        blocks.append(f"{label}\n{h.text}")
        d = _hit_dict(app, customer, h, full_text=False)
        d["n"] = i
        sources.append(d)
    user = "Sources:\n\n" + "\n\n---\n\n".join(blocks) + f"\n\nQuestion: {question}"
    answer = app.models.llm.chat(ANSWER_SYSTEM, user)
    return {"answer": answer, "model": app.models.llm.model, "sources": sources}


# --------------------------------------------------------------- exact references
def _ref_pattern(norm: str) -> re.Pattern:
    # Match the reference in the original text with optional separators between characters.
    return re.compile(r"(?:[ \t]*[-/_.:#][ \t]*)?".join(re.escape(ch) for ch in norm), re.IGNORECASE)


def _ref_snippet(text: str, pat: re.Pattern, width: int = 90) -> str:
    m = pat.search(text or "")
    if not m:
        return _snippet(text or "", 2 * width)
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    s = " ".join(text[a:b].split())
    return ("…" if a else "") + s + ("…" if b < len(text) else "")


def find_reference(app: App, reference: str, customer: str | None = None, limit: int = 20) -> dict:
    """Exact lookup of an identifier (invoice / PO / contract number) in page text.

    Separators are ignored: INV-2024-0781 == INV/2024/0781 == INV20240781. Substrings match too
    (searching 0781 finds INV-2024-0781). ``customer=None`` searches every customer."""
    norm = normalize_ref(" ".join(reference.split()))
    if len(norm) < 3:
        raise ValueError("reference must be at least 3 characters")
    cust = validate_customer_code(customer) if customer else None
    rows = app.registry.find_text(norm, cust, limit=1000)
    pat = _ref_pattern(norm)
    grouped: OrderedDict[tuple[str, str], list[tuple[int, str]]] = OrderedDict()
    for c, d, page, text in rows:
        grouped.setdefault((c, d), []).append((page, text))
    matches = []
    for (c, d), pages in grouped.items():
        rec = app.registry.get(c, d)
        if not rec:
            continue
        first_page, first_text = pages[0]
        matches.append({
            "customer_code": c,
            "doc_id": d,
            "filename": rec.filename,
            "title": rec.title,
            "pages": [p for p, _ in pages],
            "occurrences": sum(len(pat.findall(t or "")) for _, t in pages),
            "snippet": _ref_snippet(first_text, pat),
            "file_url": app.file_url(rec, first_page),
        })
    # Documents with the reference in the filename first, then by occurrences.
    matches.sort(key=lambda m: (norm not in normalize_ref(m["filename"]), -m["occurrences"]))
    return {"reference": reference, "normalized": norm, "total_documents": len(matches),
            "customers": sorted({m["customer_code"] for m in matches}), "matches": matches[:limit]}
