import os
import shutil
import subprocess
import tempfile
import zipfile
from xml.etree import ElementTree

import cv2
import numpy as np

from utils.LoggerDetector import logger

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class WordDocumentProcessor:
    """Word 可读取时直接返回文本；无有效文本时将文档渲染后 OCR。"""

    TEXT_PARTS = (
        "word/document.xml", "word/header1.xml", "word/header2.xml",
        "word/header3.xml", "word/footer1.xml", "word/footer2.xml",
        "word/footer3.xml", "word/footnotes.xml", "word/endnotes.xml",
    )

    @classmethod
    def process(cls, file_path: str, ocr) -> dict:
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".docx":
            return cls._process_docx(file_path, ocr)
        if ext == ".doc":
            return cls._process_doc(file_path, ocr)
        raise ValueError(f"不支持的 Word 文件扩展名: {ext}")

    @classmethod
    def _process_doc(cls, file_path: str, ocr) -> dict:
        with tempfile.TemporaryDirectory(prefix="ocr_doc_") as output_dir:
            converted = cls._convert(file_path, "docx", output_dir)
            result = cls._process_docx(converted, ocr)
            result["converted_from"] = "doc"
            return result

    @classmethod
    def _process_docx(cls, file_path: str, ocr) -> dict:
        if not zipfile.is_zipfile(file_path):
            raise ValueError("文件不是有效的 DOCX 文档")
        text_blocks: list[str] = []
        with zipfile.ZipFile(file_path) as archive:
            names = set(archive.namelist())
            for part in cls.TEXT_PARTS:
                if part in names:
                    text_blocks.extend(cls._extract_text_blocks(archive.read(part)))
        if text_blocks:
            return {
                "file_type": "word", "processing_method": "direct_read",
                "text_blocks": text_blocks, "text": "\n".join(text_blocks),
            }

        logger.info("Word 未读取到有效文本，转为 PDF 后执行 OCR: %s", file_path)
        with tempfile.TemporaryDirectory(prefix="ocr_word_pdf_") as output_dir:
            pdf_path = cls._convert(file_path, "pdf", output_dir)
            return cls._ocr_pdf(pdf_path, ocr)

    @classmethod
    def _ocr_pdf(cls, pdf_path: str, ocr) -> dict:
        import fitz
        from core.settings import settings

        pages: list[dict] = []
        with fitz.open(pdf_path) as document:
            if document.page_count > settings.max_pdf_pages:
                raise ValueError(f"Word 转换后页数不能超过 {settings.max_pdf_pages} 页")
            matrix = fitz.Matrix(200 / 72, 200 / 72)
            for index, page in enumerate(document, start=1):
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                    pixmap.height, pixmap.width, pixmap.n
                )
                if pixmap.n == 3:
                    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                result = ocr.detect(image) or {
                    "rec_texts": [], "rec_scores": [], "rec_polys": [], "dt_polys": []
                }
                pages.append({"page": index, **result})
        return {
            "file_type": "word", "processing_method": "ocr_fallback", "pages": pages,
        }

    @staticmethod
    def _convert(file_path: str, target_format: str, output_dir: str) -> str:
        converter = shutil.which("soffice") or shutil.which("libreoffice")
        if not converter:
            raise RuntimeError(
                "当前 Word 无法直接读取，执行 OCR 需要安装 LibreOffice/soffice"
            )
        completed = subprocess.run(
            [converter, "--headless", "--convert-to", target_format, "--outdir", output_dir, file_path],
            capture_output=True, text=True, timeout=120, check=False,
        )
        stem = os.path.splitext(os.path.basename(file_path))[0]
        converted = os.path.join(output_dir, f"{stem}.{target_format}")
        if completed.returncode != 0 or not os.path.exists(converted):
            detail = (completed.stderr or completed.stdout or "未知错误").strip()
            raise RuntimeError(f"Word 转换为 {target_format.upper()} 失败: {detail}")
        return converted

    @staticmethod
    def _extract_text_blocks(xml_data: bytes) -> list[str]:
        root = ElementTree.fromstring(xml_data)
        blocks: list[str] = []
        for paragraph in root.iter(f"{WORD_NS}p"):
            chunks: list[str] = []
            for node in paragraph.iter():
                if node.tag == f"{WORD_NS}t" and node.text:
                    chunks.append(node.text)
                elif node.tag == f"{WORD_NS}tab":
                    chunks.append("\t")
                elif node.tag in (f"{WORD_NS}br", f"{WORD_NS}cr"):
                    chunks.append("\n")
            value = "".join(chunks).strip()
            if value:
                blocks.append(value)
        return blocks
