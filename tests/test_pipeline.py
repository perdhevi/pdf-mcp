import time

import pymupdf
import pytest

from pdf_mcp import rag
from pdf_mcp.watcher import InboxWatcher


def test_sync_ingests_all_customers(app, corpus):
    res = app.sync()
    assert res["ingested"] == 5 and res["errors"] == 0
    assert {c for c, _, _ in app.registry.customers()} == {"ACME", "GLOBEX"}
    assert all(r.status == "ready" for r in app.registry.list())
    # Second sync is a no-op.
    assert app.sync()["unchanged"] == 5


def test_customer_isolation(app, corpus):
    app.sync()
    acme = rag.find_documents(app, "acme", "non-disclosure confidentiality term years")
    assert acme and acme[0]["filename"] == "ACME NDA 2024.pdf"
    assert all("Globex" not in d["filename"] for d in acme)
    globex = rag.search_chunks(app, "GLOBEX", "confidentiality term years")
    assert globex and all(h["filename"] == "Globex NDA.pdf" for h in globex)
    with pytest.raises(ValueError):
        rag.search_chunks(app, "../etc", "x")


def test_find_documents_returns_links_and_pages(app, corpus):
    app.sync()
    docs = rag.find_documents(app, "ACME", "governing law Indonesia signed Surabaya")
    top = docs[0]
    assert top["filename"] == "ACME NDA 2024.pdf"
    assert top["best_page"] == 2
    assert top["file_url_best_page"].endswith("#page=2")
    assert top["file_url"].startswith(app.s.base_url + "/files/ACME/")


def test_ocr_for_scanned_pages(app, corpus):
    app.sync()
    rec = app.registry.by_path(str(corpus["acme_scan"].resolve()))
    (page, source, text), = app.registry.get_pages("ACME", rec.doc_id, 1, 1)
    assert source == "ocr"
    assert "7781" in text and "forklift" in text.lower()
    hits = rag.search_chunks(app, "ACME", "forklift rental invoice")
    assert hits[0]["filename"] == "scanned_invoice.pdf"


def test_vision_describes_charts(app, corpus, fake_llm):
    app.sync()
    hits = rag.search_chunks(app, "ACME", "quarterly revenue bar chart Q4")
    assert hits[0]["filename"] == "annual_report.pdf"
    assert "Figure 1 on page 1" in hits[0]["text"]
    # The scanned page's full-page image must not be sent to vision (OCR covered it).
    assert sum(1 for _, _, imgs in fake_llm.calls if imgs) == 1


def test_ask_returns_cited_sources(app, corpus, fake_llm):
    app.sync()
    nda = app.registry.by_path(str(corpus["acme_nda"].resolve()))
    res = rag.ask(app, "ACME", "How long does confidentiality last?", doc_ids=[nda.doc_id])
    assert "[1]" in res["answer"]
    assert res["sources"] and all(s["doc_id"] == nda.doc_id for s in res["sources"])
    system, user, _ = fake_llm.calls[-1]
    assert "five (5) years" in user and "Globex" not in user


def test_change_rename_and_delete(app, corpus):
    app.sync()
    path = corpus["acme_msa"]
    old = app.registry.by_path(str(path.resolve()))
    # Change content -> old version replaced.
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 72), "AMENDED MASTER SERVICES AGREEMENT with penalty clause.")
    doc.save(path)
    app.sync()
    new = app.registry.by_path(str(path.resolve()))
    assert new.doc_id != old.doc_id and app.registry.get("ACME", old.doc_id) is None
    # Rename -> relocated without re-embedding.
    renamed = path.with_name("MSA v2.pdf")
    path.rename(renamed)
    rec = app.ingest_file(renamed)
    assert rec.doc_id == new.doc_id and rec.filename == "MSA v2.pdf"
    # Delete -> removed from index.
    renamed.unlink()
    assert app.sync()["removed"] == 1
    assert not [h for h in rag.search_chunks(app, "ACME", "penalty clause amended") if h["doc_id"] == new.doc_id]


def test_import_from_outside_inbox(app, corpus, tmp_path):
    outside = tmp_path / "upload.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 72), "Purchase order for Initech laptops.")
    doc.save(outside)
    target = app.import_file(outside, "initech")
    assert target.parent.name == "INITECH"
    rec = app.ingest_file(target)
    assert rec.status == "ready" and rec.customer == "INITECH"


def test_watcher_picks_up_new_files(app, corpus):
    w = InboxWatcher(app)
    w.start()
    try:
        time.sleep(0.5)
        doc = pymupdf.open()
        doc.new_page().insert_text((50, 72), "Warehouse lease agreement for Sidoarjo depot.")
        doc.save(app.s.inbox / "ACME" / "lease.pdf")
        deadline = time.time() + 15
        while time.time() < deadline:
            if any(r.filename == "lease.pdf" and r.status == "ready" for r in app.registry.list("ACME")):
                break
            time.sleep(0.3)
        else:
            pytest.fail("watcher did not ingest lease.pdf")
    finally:
        w.stop()
