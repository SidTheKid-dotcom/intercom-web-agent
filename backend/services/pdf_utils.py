import io
import logging
import re
import shutil
from dataclasses import dataclass
from typing import List, Optional

from services.site_utils import guess_title_from_url

logger = logging.getLogger(__name__)

try:
    import fitz  # type: ignore
except Exception:
    fitz = None

try:
    import pytesseract  # type: ignore
except Exception:
    pytesseract = None

try:
    from PIL import Image  # type: ignore
except Exception:
    Image = None


@dataclass
class PdfPageDocument:
    source_url: str
    title: str
    page_number: int
    text: str
    content_hash: str
    ocr_used: bool = False


def _hash_text(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def ocr_available() -> bool:
    return all([fitz is not None, pytesseract is not None, Image is not None, shutil.which("tesseract")])


def _should_ocr(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return len(compact) < 40


def extract_pdf_documents(pdf_bytes: bytes, source_url: str) -> List[PdfPageDocument]:
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed; cannot process PDFs.")

    documents: List[PdfPageDocument] = []
    title = guess_title_from_url(source_url)
    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        meta_title = (pdf.metadata or {}).get("title") or ""
        if meta_title.strip():
            title = meta_title.strip()

        for idx in range(len(pdf)):
            page = pdf.load_page(idx)
            text = page.get_text("text") or ""
            ocr_used = False

            if _should_ocr(text) and ocr_available():
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    image = Image.open(io.BytesIO(pix.tobytes("png")))
                    ocr_text = pytesseract.image_to_string(image)
                    if ocr_text and ocr_text.strip():
                        text = f"{text}\n\n{ocr_text}".strip()
                        ocr_used = True
                except Exception as exc:
                    logger.warning("OCR failed for %s page %s: %s", source_url, idx + 1, exc)

            cleaned = text.strip()
            if not cleaned:
                continue

            documents.append(
                PdfPageDocument(
                    source_url=source_url,
                    title=title,
                    page_number=idx + 1,
                    text=cleaned,
                    content_hash=_hash_text(cleaned),
                    ocr_used=ocr_used,
                )
            )

    return documents

