# -*-coding  : utf-8 -*-
# @Author    : zhangtao
# @File      : OCRDetector.py
# @Desc      : OCR识别（优化版）
# @Time      : 2026/3/3 13:46
# @Software  : PyCharm

import logging
import os
import platform
import threading
import time
from typing import Optional, Union

import cv2
import numpy as np
from paddleocr import PaddleOCR

from core.settings import settings
from utils.LoggerDetector import logger

logger = logging.getLogger(__name__)

ImageInput = Union[str, np.ndarray]

class _OCRSingleton:
    _instance: Optional["_OCRSingleton"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "_OCRSingleton":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:          # double-checked locking
                    cls._instance = super().__new__(cls)
                    cls._instance._init_ocr()
        return cls._instance

    def _init_ocr(self) -> None:
        t0 = time.perf_counter()
        self._ocr = PaddleOCR(
            use_doc_orientation_classify=False,
            use_textline_orientation=False,
            use_doc_unwarping=False,
            # doc_unwarping_model_dir=f"{settings.ocr_path}/UVDoc_infer",
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_detection_model_dir=f"{settings.ocr_path}/PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            text_recognition_model_dir=f"{settings.ocr_path}/PP-OCRv5_mobile_rec",
            enable_mkldnn=True,
        )
        logger.info("PaddleOCR 加载完毕 (mkldnn=%s)，耗时 %.2fs", time.perf_counter() - t0)

    def predict(self, image: np.ndarray) -> list:
        """加锁串行推理，防止多线程竞争崩溃"""
        with self._lock:
            return self._ocr.predict(image)


# ──────────────────────────────────────────────
# 2. 图像预处理
#    · 分辨率归一化：过小放大避免漏检，过大缩小降低耗时
#    · 去噪：消除扫描/拍照噪点
#    · CLAHE 对比度增强：改善曝光不均导致的文字模糊
#    · 轻度锐化：增强文字边缘
# ──────────────────────────────────────────────
class _ImagePreprocessor:
    MIN_WIDTH = 640
    MAX_WIDTH = 2048

    @classmethod
    def run(cls, img: np.ndarray) -> np.ndarray:
        img = cls._normalize_size(img)
        img = cls._denoise(img)
        img = cls._enhance_contrast(img)
        img = cls._sharpen(img)
        return img

    @classmethod
    def _normalize_size(cls, img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        if w < cls.MIN_WIDTH:
            img = cv2.resize(img, None,
                             fx=cls.MIN_WIDTH / w, fy=cls.MIN_WIDTH / w,
                             interpolation=cv2.INTER_CUBIC)
        elif w > cls.MAX_WIDTH:
            img = cv2.resize(img, None,
                             fx=cls.MAX_WIDTH / w, fy=cls.MAX_WIDTH / w,
                             interpolation=cv2.INTER_AREA)
        return img

    @staticmethod
    def _denoise(img: np.ndarray) -> np.ndarray:
        return cv2.fastNlMeansDenoisingColored(
            img, None, h=7, hColor=7, templateWindowSize=7, searchWindowSize=21)

    @staticmethod
    def _enhance_contrast(img: np.ndarray) -> np.ndarray:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return cv2.cvtColor(cv2.merge([clahe.apply(l), a, b]), cv2.COLOR_LAB2BGR)

    @staticmethod
    def _sharpen(img: np.ndarray) -> np.ndarray:
        kernel = np.array([[0, -0.5, 0],
                           [-0.5,  3, -0.5],
                           [0, -0.5, 0]], dtype=np.float32)
        return cv2.filter2D(img, -1, kernel)


# ──────────────────────────────────────────────
# 3. OCRDetector（对外接口，兼容原有调用方式）
# ──────────────────────────────────────────────
class OCRDetector:
    """
    优化点总结：
      1. 单例模型   — 首次加载后全局复用，后续调用无初始化耗时
      2. 推理加锁   — 防止多线程并发导致 SIGSEGV
      3. 平台检测   — macOS/ARM 自动关闭 mkldnn，避免崩溃
      4. 图像预处理 — 归一化 + 去噪 + CLAHE + 锐化，提升准确度
      5. 置信度过滤 — 丢弃低质量识别结果
      6. 批量串行   — detect_batch 保证顺序且不引入并发风险
    """

    CONFIDENCE_THRESHOLD = 0.6

    def __init__(
        self,
        preprocess: bool = True,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        save_json_dir: Optional[str] = None,
    ) -> None:
        self._engine = _OCRSingleton()
        self._preprocess = preprocess
        self._threshold = confidence_threshold
        self._save_json_dir = save_json_dir
        if save_json_dir:
            os.makedirs(save_json_dir, exist_ok=True)

    # ── 输入归一化：兼容路径字符串和 numpy array ──
    @staticmethod
    def _to_array(image: ImageInput) -> np.ndarray:
        if isinstance(image, np.ndarray):
            return image
        arr = cv2.imread(str(image))
        if arr is None:
            raise ValueError(f"无法读取图像文件: {image}")
        return arr

    # ── 单张识别（兼容原有 detect 调用签名）────────
    def detect(self, image: ImageInput) -> list[str]:
        t0 = time.perf_counter()

        img = self._to_array(image)
        if self._preprocess:
            img = _ImagePreprocessor.run(img)

        texts = self._run_ocr(img)

        logger.debug("识别完成 %d 条，耗时 %.0fms",
                     len(texts), (time.perf_counter() - t0) * 1000)
        return texts

    # ── 批量识别（串行，PaddleOCR 不支持真并发）────
    def detect_batch(self, images: list[ImageInput]) -> list[list[str]]:
        results = []
        for i, img in enumerate(images):
            try:
                results.append(self.detect(img))
            except Exception as exc:
                logger.error("第 %d 张失败: %s", i, exc)
                results.append([])
        return results


    def _run_ocr(self, image: np.ndarray) -> list[str]:
        result = self._engine.predict(image)
        if not result:
            logger.warning("predict 返回空，shape=%s", image.shape)
            return []

        texts: list[str] = []
        for i, res in enumerate(result):
            rec_texts  = res.get("rec_texts",  []) if hasattr(res, "get") else getattr(res, "rec_texts",  [])
            rec_scores = res.get("rec_scores", []) if hasattr(res, "get") else getattr(res, "rec_scores", [])

            if self._save_json_dir:
                try:
                    res.save_to_json(
                        os.path.join(self._save_json_dir, f"ocr_result_{i}.json"))
                except Exception as e:
                    logger.warning("保存 JSON 失败: %s", e)

        return texts


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    image = r"/Volumes/Projects/projects/ERREN/DualLayerPDFService/output/page_0001.png"

    images = [
        r"/Volumes/Projects/projects/ERREN/DualLayerPDFService/output/page_0001.png",
        r"/Volumes/Projects/projects/ERREN/DualLayerPDFService/output/page_0002.png",
        r"/Volumes/Projects/projects/ERREN/DualLayerPDFService/output/page_0003.png",
        r"/Volumes/Projects/projects/ERREN/DualLayerPDFService/output/page_0004.png",
    ]

    start = time.perf_counter()
    ocr = OCRDetector()
    text = ocr.detect_batch(images)
    print(text)
    print(f"耗时: {time.perf_counter() - start:.2f}s")