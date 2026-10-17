import anyio
import pymupdf
import pytest
from mcp import Client

from pdf_mcp import rag
from pdf_mcp.registry import Registry, normalize_ref
from pdf_mcp.server import create_server


def _pdf(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=11)
    doc.save(path)


@pytest.fixture
def invoices(app, corpus):
    _pdf(app.s.inbox / "GLOBEX" / "inv_0781.pdf",
         "TAX INVOICE\nInvoice No: INV/2024/0781\nDate: 3 May 2024\nTotal: IDR 12.500.000")
    _pdf(app.s.inbox / "ACME" / "remittance.pdf",
         "Remittance advice. Payment received for INV-2024-0781 and INV-2024-0790.")
    app.sync()


def test_normalize_ref():
    assert normalize_ref("INV-2024-0781") == normalize_ref("inv/2024/0781") == "inv20240781"
    assert normalize_ref("Total due 4.500 USD") == "total due 4500 usd"
    # OCR artefact: spaces around a separator.
    assert normalize_ref("INVOICE INV-2026- 0106") == "invoice inv20260106"
    assert normalize_ref("PO- ACME- 5506") == "poacme5506"


def test_reference_across_customers(app, invoices):
    for variant in ("INV-2024-0781", "inv/2024/0781", "INV20240781", "2024-0781"):
        res = rag.find_reference(app, variant)
        assert res["customers"] == ["ACME", "GLOBEX"], variant
    res = rag.find_reference(app, "INV-2024-0781")
    globex = next(m for m in res["matches"] if m["customer_code"] == "GLOBEX")
    assert "INV/2024/0781" in globex["snippet"] and globex["file_url"].endswith("#page=1")


def test_reference_scoped_and_partial(app, invoices):
    res = rag.find_reference(app, "0781", customer="globex")
    assert [m["filename"] for m in res["matches"]] == ["inv_0781.pdf"]
    # OCR'd scanned invoice is searchable by number.
    res = rag.find_reference(app, "7781", customer="ACME")
    assert res["matches"][0]["filename"] == "scanned_invoice.pdf"
    assert rag.find_reference(app, "INV-9999-0000")["total_documents"] == 0
    with pytest.raises(ValueError):
        rag.find_reference(app, "07")


def test_index_follows_deletes(app, invoices):
    rec = next(r for r in app.registry.list("GLOBEX") if r.filename == "inv_0781.pdf")
    app.delete_document("GLOBEX", rec.doc_id, delete_file=True)
    assert rag.find_reference(app, "INV-2024-0781")["customers"] == ["ACME"]


def test_ocr_spacing_snippet(app, invoices):
    from pdf_mcp.rag import _ref_pattern, _ref_snippet

    snip = _ref_snippet("INVOICE INV-2026- 0106 Bill to ACME", _ref_pattern("inv20260106"))
    assert "INV-2026- 0106" in snip


def test_backfill_existing_registry(app, invoices):
    app.registry._db.execute("DROP TABLE pages_fts")
    app.registry._db.commit()
    reg = Registry(app.s.registry_path)  # simulates first start after upgrade
    assert reg.fts and reg.find_text("inv20240781")
    reg.close()


def test_global_tool_is_opt_in(app, invoices):
    async def tool_names(server):
        async with Client(server) as c:
            return {t.name for t in (await c.list_tools()).tools}

    names = anyio.run(tool_names, create_server(app))
    assert "find_reference" in names and "find_reference_all_customers" not in names

    app.s.allow_global_search = True
    server = create_server(app)

    async def call():
        async with Client(server) as c:
            assert "find_reference_all_customers" in {t.name for t in (await c.list_tools()).tools}
            r = await c.call_tool("find_reference_all_customers", {"reference": "INV/2024/0781"})
            return r.structured_content

    out = anyio.run(call)
    assert out["customers"] == ["ACME", "GLOBEX"]


def test_rebuild_on_normalization_change(app, invoices):
    db = app.registry._db
    db.execute("UPDATE pages_fts SET norm = 'stale'")
    db.execute("PRAGMA user_version = 1")
    db.commit()
    reg = Registry(app.s.registry_path)
    assert reg.find_text("inv20240781")
    reg.close()


@pytest.fixture
def lookalikes(app):
    acme = app.s.inbox / "ACME"
    _pdf(acme / "invoice_0781.pdf", "TAX INVOICE\nInvoice No: INV-2024-0781\nTotal: IDR 12.500.000")
    _pdf(acme / "invoice_0718.pdf", "TAX INVOICE\nInvoice No: INV-2024-0718\nTotal: IDR 3.200.000")
    _pdf(acme / "delivery_note.pdf", "Delivery note 10781. Goods shipped to ACME warehouse.")
    _pdf(acme / "spaced_invoice.pdf", "Payment received for invoice INV 2024 0781.")
    app.sync()


def _files(app, reference):
    return {m["filename"] for m in rag.find_reference(app, reference, customer="ACME")["matches"]}


def test_near_identical_numbers_do_not_cross_match(app, lookalikes):
    assert "invoice_0781.pdf" in _files(app, "INV-2024-0781")
    assert "invoice_0718.pdf" not in _files(app, "INV-2024-0781")
    assert "invoice_0718.pdf" in _files(app, "INV-2024-0718")
    assert "invoice_0781.pdf" not in _files(app, "INV-2024-0718")


def test_partial_number_matches_longer_numbers(app, lookalikes):
    # Substring search: a short partial number also hits unrelated longer numbers.
    assert "delivery_note.pdf" in _files(app, "0781")
    assert "delivery_note.pdf" not in _files(app, "INV-2024-0781")


def test_space_only_separators_are_not_normalised(app, lookalikes):
    # Known limitation: only - / _ . : # count as separators, so "INV 2024 0781" (spaces only)
    # is not found by INV-2024-0781. Changing this is a deliberate decision: update this test,
    # normalize_ref, NORM_VERSION and the README together.
    assert normalize_ref("INV 2024 0781") == "inv 2024 0781"
    assert "spaced_invoice.pdf" not in _files(app, "INV-2024-0781")
    assert "spaced_invoice.pdf" in _files(app, "INV 2024 0781")  # indexed; only the separator differs
