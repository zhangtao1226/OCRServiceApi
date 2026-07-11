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
    """Word 文档处理：优先提取可编辑文本，再 OCR 文档内嵌图片。"""

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
        converter = shutil.which("soffice") or shutil.which("libreoffice")
        if not converter:
            raise RuntimeError("处理 .doc 需要安装 LibreOffice/soffice；建议优先上传 .docx 文件")
        with tempfile.TemporaryDirectory(prefix="ocr_doc_") as output_dir:
            completed = subprocess.run(
                [converter, "--headless", "--convert-to", "docx", "--outdir", output_dir, file_path],
                capture_output=True, text=True, timeout=120, check=False,
            )
            converted = os.path.join(
                output_dir, f"{os.path.splitext(os.path.basename(file_path))[0]}.docx"
            )
            if completed.returncode != 0 or not os.path.exists(converted):
                detail = (completed.stderr or completed.stdout or "未知错误").strip()
                raise RuntimeError(f".doc 转换失败: {detail}")
            result = cls._process_docx(converted, ocr)
            result["converted_from"] = "doc"
            return result

    @classmethod
    def _process_docx(cls, file_path: str, ocr) -> dict:
        if not zipfile.is_zipfile(file_path):
            raise ValueError("文件不是有效的 DOCX 文档")
        text_blocks: list[str] = []
        image_results: list[dict] = []
        with zipfile.ZipFile(file_path) as archive:
            names = set(archive.namelist())
            for part in cls.TEXT_PARTS:
                if part in names:
                    text_blocks.extend(cls._extract_text_blocks(archive.read(part)))
            media_names = sorted(
                name for name in names if name.startswith("word/media/") and not name.endswith("/")
            )
            for index, name in enumerate(media_names, start=1):
                image = cv2.imdecode(np.frombuffer(archive.read(name), dtype=np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    logger.warning("无法解码 Word 内嵌媒体: %s", name)
                    continue
                result = ocr.detect(image)
                if result and result.get("rec_texts"):
                    image_results.append({"image": index, "name": name, **result})
        return {
            "file_type": "word", "extraction_method": "text_first",
            "text_blocks": text_blocks, "text": "\n".join(text_blocks),
            "embedded_images": image_results,
        }

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
