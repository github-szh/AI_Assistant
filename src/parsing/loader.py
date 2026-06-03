"""Unified document loader — route file to appropriate parser by extension."""

import logging
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ParsedDocument:
    """Result of parsing a single document."""

    file_path: str
    file_type: str
    content: str
    metadata: dict = field(default_factory=dict)
    parser_used: str = ""


class DocumentLoader:
    """Entry point for document parsing. Routes by file extension.

    Usage:
        loader = DocumentLoader()
        docs = loader.load("report.pdf")
        for doc in docs:
            print(doc.content)
    """

    def __init__(self):
        self._parsers: dict[str, object] = {}

    def _get_parser(self, ext: str):
        """Lazy-load parser modules to avoid importing heavy deps at startup."""
        ext = ext.lower()

        if ext == ".pdf":
            # Try pymupdf first, marker as fallback for complex layouts
            try:
                from src.parsing.pdf_text import PyMuPDFParser
                return PyMuPDFParser()
            except ImportError:
                logger.warning("pymupdf not available, trying marker")
                try:
                    from src.parsing.pdf_markdown import MarkerParser
                    return MarkerParser()
                except ImportError:
                    raise RuntimeError("No PDF parser available. Install pymupdf or marker-pdf.")

        if ext in (".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp"):
            from src.parsing.ocr import OCRParser
            return OCRParser()

        if ext in (".docx", ".pptx"):
            from src.parsing.office_parser import OfficeParser
            return OfficeParser()

        if ext == ".txt":
            from src.parsing.text_parser import TextParser
            return TextParser()

        raise ValueError(f"Unsupported file type: {ext}")

    def load(self, file_path: str | Path) -> list[ParsedDocument]:
        """Parse a document file into one or more ParsedDocument objects.

        Returns a list because multi-page PDFs may be split into multiple chunks
        at the parser level (e.g. one ParsedDocument per page).

        Auto-detects scanned PDFs and routes them to OCR automatically.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        ext = file_path.suffix.lower()
        logger.info("Loading %s (type=%s)", file_path.name, ext)

        # Auto-detect scanned PDF → route to OCR
        if ext == ".pdf" and self.guess_need_ocr(str(file_path)):
            logger.info("Detected scanned PDF, switching to OCR parser")
            parser = self._get_ocr_parser()
        else:
            parser = self._get_parser(ext)

        if hasattr(parser, "parse"):
            result = parser.parse(str(file_path))
        else:
            raise RuntimeError(f"Parser for {ext} has no parse() method")

        if isinstance(result, list):
            return result
        else:
            return [result]

    def _get_ocr_parser(self):
        """Get the OCR parser (lazy-loaded)."""
        from src.parsing.ocr import OCRParser
        return OCRParser()

    def guess_need_ocr(self, file_path: str | Path) -> bool:
        """Quick heuristic: does this PDF likely need OCR?

        Checks the first 3 pages for Chinese text. A single-page cover (e.g.
        image-only title page) won't fool it — if any of the first 3 pages has
        real CJK sentences, the PDF is treated as native text.

        Scanned PDFs may have OCR artifacts (scattered numbers/symbols) but no
        real Chinese sentences — only count CJK characters as meaningful text.
        """
        file_path = Path(file_path)
        if file_path.suffix.lower() != ".pdf":
            return False

        try:
            import fitz
            doc = fitz.open(str(file_path))
            pages_to_check = min(3, len(doc))
            for i in range(pages_to_check):
                text = doc[i].get_text().strip()
                cjk_chars = sum(1 for c in text if '一' <= c <= '鿿')
                if cjk_chars >= 10:
                    doc.close()
                    return False  # found real text — native PDF
            doc.close()
            return True  # no page has enough CJK — probably scanned
        except Exception as exc:
            logger.warning("OCR检测失败，默认启用OCR: %s", exc)
            return True
