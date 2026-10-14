"""PDF -> per-page text.

Strategy per page:
  1. Native text layer via PyMuPDF.
  2. If the page has (almost) no text, it is treated as scanned and OCR'd
     (Tesseract by default, or the vision model when ``ocr_engine=vision``).
  3. Embedded raster images (charts, diagrams, photos) and vector-drawn charts
     are described by the vision model; descriptions are appended to the
     page text as ``[Figure ...]`` blocks so they become searchable.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .config import Settings
from .providers import Models, ProviderError

log = logging.getLogger(__name__)

FIGURE_PROMPT = (
    "You are indexing a business document for search. Describe this image from page {page} precisely. "
    "If it is a chart or graph: state the chart type, title, axes, legend, and list the key data points and "
    "trends with their numbers. If it is a table: transcribe it as a markdown table. If it is a diagram: "
    "explain the components and relationships. If it is a photo, logo, stamp or signature: say so in one "
    "short sentence. Transcribe any readable text. Do not speculate beyond what is visible."
)
PAGE_FIGURES_PROMPT = (
    "This is page {page} of a business document. It contains vector-drawn charts, graphs or diagrams. "
    "For each chart/diagram on the page, describe its type, title, axes, legend and the key values and trends "
    "with numbers. Ignore ordinary body text paragraphs."
)
OCR_PROMPT = (
    "Transcribe all text on this scanned document page exactly, preserving reading order, headings, lists "
    "and tables (as markdown). Output only the transcription."
)
VISION_SYSTEM = "You are a meticulous document analyst. Answer in the document's language."


@dataclass
class PageContent:
    page: int  # 1-based
    text: str
    source: str  # text | ocr | vision-ocr
    figures: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        parts = [self.text.strip()] if self.text.strip() else []
        for i, f in enumerate(self.figures, 1):
            parts.append(f"[Figure {i} on page {self.page}]\n{f.strip()}")
        return "\n\n".join(parts)


@dataclass
class ExtractedDoc:
    title: str
    page_count: int
    pages: list[PageContent]
    warnings: list[str] = field(default_factory=list)


class Extractor:
    def __init__(self, settings: Settings, models: Models):
        self.s = settings
        self.models = models
        if settings.tesseract_cmd:
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

    # ------------------------------------------------------------------
    def extract(self, path: Path) -> ExtractedDoc:
        warnings: list[str] = []
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                raise ValueError("PDF is password protected")
            title = (doc.metadata or {}).get("title") or path.stem
            seen_xrefs: set[int] = set()
            images_described = 0
            pages: list[PageContent] = []
            for page in doc:
                pno = page.number + 1
                text = page.get_text("text", sort=True) or ""
                source = "text"
                if self.s.ocr_enabled and len(text.strip()) < self.s.ocr_min_chars:
                    ocr = self._ocr_page(page, warnings)
                    if ocr and len(ocr.strip()) > len(text.strip()):
                        text, source = ocr, ("vision-ocr" if self.s.ocr_engine == "vision" else "ocr")
                pc = PageContent(page=pno, text=text, source=source)

                if self.s.vision_enabled:
                    budget = self.s.vision_max_images_per_doc - images_described
                    images_described += self._describe_images(doc, page, pc, seen_xrefs, budget, warnings)
                pages.append(pc)
            return ExtractedDoc(title=title, page_count=doc.page_count, pages=pages, warnings=warnings)

    # ------------------------------------------------------------------
    def _render(self, page: pymupdf.Page, dpi: int) -> bytes:
        return page.get_pixmap(dpi=dpi).tobytes("png")

    def _ocr_page(self, page: pymupdf.Page, warnings: list[str]) -> str:
        pno = page.number + 1
        try:
            if self.s.ocr_engine == "vision":
                img = self._render(page, min(self.s.ocr_dpi, 200))
                return self.models.vision.chat(VISION_SYSTEM, OCR_PROMPT, images=[img], temperature=0)
            import pytesseract
            from PIL import Image

            img = Image.open(io.BytesIO(self._render(page, self.s.ocr_dpi)))
            return pytesseract.image_to_string(img, lang=self.s.ocr_lang)
        except (ProviderError, OSError, RuntimeError) as e:
            msg = f"OCR failed on page {pno}: {e}"
            log.warning(msg)
            warnings.append(msg)
            return ""
        except Exception as e:  # pytesseract.TesseractNotFoundError etc.
            msg = f"OCR failed on page {pno}: {type(e).__name__}: {e}"
            log.warning(msg)
            warnings.append(msg)
            return ""

    def _describe_images(self, doc, page, pc: PageContent, seen: set[int], budget: int, warnings) -> int:
        if budget <= 0:
            return 0
        used = 0
        page_area = abs(page.rect) or 1.0
        for info in page.get_images(full=True):
            if used >= budget:
                break
            xref = info[0]
            if xref in seen:
                continue
            seen.add(xref)
            w, h = info[2], info[3]
            if min(w, h) < self.s.vision_min_image_px:
                continue
            # Skip the full-page scan image of an OCR'd page: OCR already covered it.
            if pc.source != "text":
                rects = page.get_image_rects(xref)
                if rects and max(abs(r) for r in rects) / page_area > 0.6:
                    continue
            try:
                png = self._image_png(doc, xref)
                desc = self.models.vision.chat(
                    VISION_SYSTEM, FIGURE_PROMPT.format(page=pc.page), images=[png], temperature=0
                )
            except Exception as e:
                msg = f"Image description failed on page {pc.page} (xref {xref}): {e}"
                log.warning(msg)
                warnings.append(msg)
                # Provider unreachable -> stop trying for the rest of the doc.
                if isinstance(e, ProviderError) and "unreachable" in str(e):
                    return budget
                continue
            if desc:
                pc.figures.append(desc)
            used += 1

        # Vector charts (no raster image): many drawing operations on the page.
        if used < budget and pc.source == "text":
            try:
                n_drawings = len(page.get_drawings())
            except Exception:
                n_drawings = 0
            min_ops = self.s.vision_vector_min_drawings
            if min_ops > 0 and n_drawings >= min_ops:
                try:
                    desc = self.models.vision.chat(
                        VISION_SYSTEM,
                        PAGE_FIGURES_PROMPT.format(page=pc.page),
                        images=[self._render(page, 150)],
                        temperature=0,
                    )
                    if desc:
                        pc.figures.append(desc)
                    used += 1
                except Exception as e:
                    warnings.append(f"Vector figure description failed on page {pc.page}: {e}")
        return used

    @staticmethod
    def _image_png(doc, xref: int) -> bytes:
        pix = pymupdf.Pixmap(doc, xref)
        if pix.colorspace is None:
            raise ValueError("image mask without colorspace")
        if pix.colorspace.n not in (1, 3):  # CMYK etc. -> RGB
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        # Downscale very large images to keep vision requests fast.
        if max(pix.width, pix.height) > 1600:
            factor = max(pix.width, pix.height) / 1600
            pix = pymupdf.Pixmap(pix, int(pix.width / factor), int(pix.height / factor), None)
        return pix.tobytes("png")
