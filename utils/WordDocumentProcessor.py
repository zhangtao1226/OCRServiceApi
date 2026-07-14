import os
import tempfile

import cv2
import numpy as np

from utils.LoggerDetector import logger

class WordDocumentProcessor:
    """Word 有可读内容时直接返回全文，否则转换后 OCR 并合并为全文。"""

    @classmethod
    def process(cls, file_path: str, ocr) -> dict:
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in {".doc", ".docx"}:
            raise ValueError(f"不支持的 Word 文件扩展名: {ext}")
        aw, document = cls._load_document(file_path)
        text = (document.get_text() or "").strip()
        if text:
            rec_texts = [line.strip() for line in text.splitlines() if line.strip()]
            result = {
                "file_type": "word",
                "processing_method": "direct_read",
                "pages": [{
                    "page": 1,
                    "rec_texts": rec_texts,
                    "rec_scores": [1.0] * len(rec_texts),
                    "rec_polys": [],
                    "dt_polys": [],
                }],
            }
            if ext == ".doc":
                result["converted_from"] = "doc"
            return result

        with tempfile.TemporaryDirectory(prefix="ocr_word_pdf_") as output_dir:
            pdf_path = os.path.join(
                output_dir, f"{os.path.splitext(os.path.basename(file_path))[0]}.pdf"
            )
            try:
                document.save(pdf_path, aw.SaveFormat.PDF)
            except Exception as exc:
                raise RuntimeError(f"Word 转换为 PDF 失败: {exc}") from exc
            result = cls._ocr_pdf_to_text(pdf_path, ocr)
            if ext == ".doc":
                result["converted_from"] = "doc"
            return result

    @classmethod
    def _ocr_pdf_to_text(cls, pdf_path: str, ocr) -> dict:
        import fitz
        from core.settings import settings

        rec_texts: list[str] = []
        rec_scores: list[float] = []
        rec_polys: list = []
        dt_polys: list = []
        with fitz.open(pdf_path) as document:
            if document.page_count > settings.max_pdf_pages:
                raise ValueError(f"Word 转换后页数不能超过 {settings.max_pdf_pages} 页")
            matrix = fitz.Matrix(200 / 72, 200 / 72)
            for page in document:
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                    pixmap.height, pixmap.width, pixmap.n
                )
                if pixmap.n == 3:
                    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                result = ocr.detect(image) or {
                    "rec_texts": [], "rec_scores": [], "rec_polys": [], "dt_polys": []
                }
                rec_texts.extend(result.get("rec_texts", []))
                rec_scores.extend(result.get("rec_scores", []))
                rec_polys.extend(result.get("rec_polys", []))
                dt_polys.extend(result.get("dt_polys", []))
        return {
            "file_type": "word",
            "processing_method": "ocr_fallback",
            "pages": [{
                "page": 1,
                "rec_texts": rec_texts,
                "rec_scores": rec_scores,
                "rec_polys": rec_polys,
                "dt_polys": dt_polys,
            }],
        }

    @staticmethod
    def _load_document(file_path: str):
        try:
            import aspose.words_foss as aw
        except ImportError as exc:
            raise RuntimeError(
                "缺少 Python 依赖 aspose-words-foss；请使用 Python 3.10-3.12 "
                "并从离线依赖包安装 requirements.txt"
            ) from exc

        try:
            document = aw.Document(file_path)
        except Exception as exc:
            raise RuntimeError(f"Word 文档读取失败: {exc}") from exc
        return aw, document
