"""Test fixtures: deterministic fake models + generated PDFs (text, scanned, with chart image)."""

from __future__ import annotations

import hashlib
import io
import math
import re
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from pdf_mcp.config import Settings
from pdf_mcp.providers import Models
from pdf_mcp.service import App

DIM = 384
_TOKEN = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Hashed bag-of-words embedding: lexical overlap -> cosine similarity."""

    model = "fake-embed"

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * DIM
            for tok in _TOKEN.findall(t.lower()):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                v[h % DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class FakeLLM:
    model = "fake-llm"

    def __init__(self):
        self.calls = []

    def chat(self, system, user, images=None, temperature=None, max_tokens=None):
        self.calls.append((system, user, images))
        if images:
            return "Bar chart titled Quarterly Revenue: Q1 120, Q2 150, Q3 170, Q4 210 (USD thousands)."
        return "Per the documents, the confidentiality term is five years [1]."


def _text_pdf(path: Path, pages: list[str], title: str) -> None:
    doc = pymupdf.open()
    for body in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), body, fontsize=11)
    doc.set_metadata({"title": title})
    doc.save(path)


def _scanned_pdf(path: Path, text: str) -> None:
    img = Image.new("RGB", (1700, 2200), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 44)
    except OSError:
        font = ImageFont.load_default(size=44)
    y = 150
    for line in text.split("\n"):
        d.text((120, y), line, fill="black", font=font)
        y += 80
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(path)


def _chart_pdf(path: Path) -> None:
    img = Image.new("RGB", (600, 400), "white")
    d = ImageDraw.Draw(img)
    for i, h in enumerate([120, 150, 170, 210]):
        d.rectangle([60 + i * 130, 380 - h, 140 + i * 130, 380], fill=(40, 90, 200))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Annual report 2024. See the chart below for revenue by quarter.", fontsize=11)
    page.insert_image(pymupdf.Rect(50, 100, 500, 400), stream=buf.getvalue())
    doc.save(path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        qdrant_path=tmp_path / "qdrant",
        qdrant_url=None,
        ocr_lang="eng",
        chunk_size=500,
        chunk_overlap=80,
        watch_debounce_seconds=0.5,
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def app(settings, fake_llm) -> App:
    models = Models(settings, embedder=FakeEmbedder(), llm=fake_llm, vision=fake_llm)
    a = App(settings, models=models)
    yield a
    a.stop()
    a.store.client.close()
    a.registry.close()


@pytest.fixture
def corpus(app: App) -> dict[str, Path]:
    acme = app.s.inbox / "ACME"
    globex = app.s.inbox / "globex" / "legal"  # lower-case + nested folder on purpose
    acme.mkdir(parents=True)
    globex.mkdir(parents=True)
    files = {
        "acme_nda": acme / "ACME NDA 2024.pdf",
        "acme_msa": acme / "ACME Master Services Agreement.pdf",
        "acme_scan": acme / "scanned_invoice.pdf",
        "acme_report": acme / "annual_report.pdf",
        "globex_nda": globex / "Globex NDA.pdf",
    }
    _text_pdf(files["acme_nda"], [
        "MUTUAL NON-DISCLOSURE AGREEMENT between ACME Corp and Versata Ltd.\n\n"
        "1. Confidential Information includes source code, pricing and customer lists.\n"
        "2. Term. The confidentiality obligations survive for five (5) years after termination.",
        "3. Governing law. This agreement is governed by the laws of the Republic of Indonesia.\n"
        "4. Signatures. Signed in Surabaya on 12 March 2024.",
    ], title="ACME Mutual NDA")
    _text_pdf(files["acme_msa"], [
        "MASTER SERVICES AGREEMENT. ACME Corp engages the provider for ERP support services.\n"
        "Service levels: priority 1 incidents must be responded to within 30 minutes, 24x7.\n"
        "Fees are invoiced monthly in arrears, payment due within 45 days.",
    ], title="ACME MSA")
    _scanned_pdf(files["acme_scan"], "INVOICE NUMBER 7781\nBill to ACME Corp\nWarehouse forklift rental\nTotal due 4500 USD")
    _chart_pdf(files["acme_report"])
    _text_pdf(files["globex_nda"], [
        "NON-DISCLOSURE AGREEMENT between Globex and Initech.\n"
        "Term. Confidentiality obligations survive for ten (10) years after termination. "
        "Governed by the laws of Singapore.",
    ], title="Globex NDA")
    return files
