# -*-coding  : utf-8 -*-
# @Author    : zhangtao
# @File      : OCRDetector.py
# @Desc      : OCR 识别（单例引擎 + 图像预处理）
# @Time      : 2026/3/3

import os
import threading
import time
from typing import Optional, Union

import cv2
import numpy as np
from paddleocr import PaddleOCR

from core.settings import settings
from utils.LoggerDetector import logger

ImageInput = Union[str, np.ndarray]


# ── mkldnn 运行时检测 ──────────────────────────────────────────────────
def _mkldnn_available() -> bool:
    """
    运行时检测 mkldnn 是否可用：直接做一次最小推理，成功则启用。
    paddle.fluid 在新版 PaddlePaddle 中已移除，不再依赖该 API。
    统信(UOS)、部分国产 CPU 环境下 enable_mkldnn=True 会触发
    ConvertPirAttribute2RuntimeAttribute 未实现错误，捕获后自动降级。
    """
    import platform
    # macOS ARM (Apple Silicon) 不支持 mkldnn，直接跳过避免报错
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return False
    try:
        ocr_test = PaddleOCR(
            use_doc_orientation_classify=False,
            use_textline_orientation=False,
            use_doc_unwarping=False,
            enable_mkldnn=True,
        )
        ocr_test.predict(np.ones((64, 64, 3), dtype=np.uint8) * 255)
        del ocr_test
        return True
    except Exception as e:
        logger.warning("mkldnn 不可用，已自动降级为 CPU 模式: %s", e)
        return False


_USE_MKLDNN: bool = _mkldnn_available()
logger.info("mkldnn 加速: %s", _USE_MKLDNN)


# ── OCR 引擎单例 ───────────────────────────────────────────────────────
class _OCRSingleton:
    _instance: Optional["_OCRSingleton"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "_OCRSingleton":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init_ocr()
        return cls._instance

    def _init_ocr(self) -> None:
        t0 = time.perf_counter()
        self._ocr = PaddleOCR(
            use_doc_orientation_classify=False,
            use_textline_orientation=False,
            use_doc_unwarping=False,
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_detection_model_dir=f"{settings.ocr_path}/PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            text_recognition_model_dir=f"{settings.ocr_path}/PP-OCRv5_mobile_rec",
            enable_mkldnn=_USE_MKLDNN,
        )
        logger.info("PaddleOCR 加载完毕 (mkldnn=%s)，耗时 %.2fs",
                    _USE_MKLDNN, time.perf_counter() - t0)

    def predict(self, image: np.ndarray) -> list:
        with self._lock:
            return self._ocr.predict(image)


# ── 图像预处理 ─────────────────────────────────────────────────────────
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


# ── 对外 OCR 检测器 ────────────────────────────────────────────────────
class OCRDetector:

    CONFIDENCE_THRESHOLD = 0.6

    def __init__(
        self,
        preprocess: bool = True,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
    ) -> None:
        self._engine     = _OCRSingleton()
        self._preprocess = preprocess
        self._threshold  = confidence_threshold

    # ── 预热（lifespan 调用）────────────────────────────────────────────

    @classmethod
    def warmup(cls) -> None:
        """在服务启动阶段（线程池中）触发单例初始化，避免首次请求阻塞。"""
        logger.info("OCR 模型预热开始 ···")
        t0 = time.perf_counter()
        _OCRSingleton()
        logger.info("OCR 模型预热完成，耗时 %.2fs", time.perf_counter() - t0)

    # ── 单张识别 ────────────────────────────────────────────────────────

    def detect(self, image: ImageInput) -> Optional[dict]:
        """
        识别单张图片，返回可直接 JSON 序列化的结果字典：
        {
            "rec_texts":  list[str],          # 识别文本列表
            "rec_scores": list[float],         # 对应置信度
            "rec_polys":  list[list],          # 文本行坐标（已转为 Python list）
            "dt_polys":   list[list],          # 检测框坐标
        }
        识别失败或无结果返回 None。
        """
        t0 = time.perf_counter()

        img = self._load_image(image)
        if self._preprocess:
            img = _ImagePreprocessor.run(img)

        result = self._run_ocr(img)
        logger.debug(
            "识别完成 %d 条，耗时 %.0fms",
            len(result.get("rec_texts", [])) if result else 0,
            (time.perf_counter() - t0) * 1000,
        )
        return result

    # ── 批量识别 ────────────────────────────────────────────────────────

    def detect_batch(self, images: list[ImageInput]) -> list[Optional[dict]]:
        """批量识别，每张对应一个结果字典（失败时为 None）。"""
        results = []
        for i, img in enumerate(images):
            try:
                results.append(self.detect(img))
            except Exception as exc:
                logger.error("第 %d 张识别失败: %s", i, exc)
                results.append(None)
        return results

    # ── 内部实现 ────────────────────────────────────────────────────────

    @staticmethod
    def _load_image(image: ImageInput) -> np.ndarray:
        if isinstance(image, np.ndarray):
            return image
        arr = cv2.imread(str(image))
        if arr is None:
            raise ValueError(f"无法读取图像文件: {image}")
        return arr

    def _run_ocr(self, image: np.ndarray) -> Optional[dict]:
        """
        调用引擎并将 numpy 数组转为纯 Python 类型，保证结果可 JSON 序列化。
        置信度低于阈值的条目会被过滤。
        """
        raw = self._engine.predict(image)
        if not raw:
            logger.warning("predict 返回空，shape=%s", image.shape)
            return None

        for res in raw:
            rec_texts  = res.get("rec_texts")  or []
            rec_scores = res.get("rec_scores") or []
            rec_polys  = res.get("rec_polys")  or []
            dt_polys   = res.get("dt_polys")   or []

            # 过滤低置信度条目
            filtered = [
                (t, float(s), self._to_list(rp), self._to_list(dp))
                for t, s, rp, dp in zip(rec_texts, rec_scores, rec_polys, dt_polys)
                if float(s) >= self._threshold
            ]

            if not filtered:
                return {"rec_texts": [], "rec_scores": [], "rec_polys": [], "dt_polys": []}

            texts, scores, r_polys, d_polys = zip(*filtered)
            return {
                "rec_texts":  list(texts),
                "rec_scores": list(scores),
                "rec_polys":  list(r_polys),
                "dt_polys":   list(d_polys),
            }

        return None

    @staticmethod
    def _to_list(value) -> list:
        """将 numpy ndarray（或其嵌套列表）递归转为 Python list。"""
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, (list, tuple)):
            return [OCRDetector._to_list(v) for v in value]
        return value