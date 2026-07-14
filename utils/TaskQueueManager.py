# -*-coding  : utf-8 -*-
# @Author    : zhangtao
# @File      : TaskQueueManager.py
# @Desc      : 异步任务队列管理器（OCR 识别）
# @Time      : 2026/5/25

import asyncio
import json
import os
import time
import uuid
from typing import Optional

from utils.LoggerDetector import logger
from utils.MemoryGuard import MemoryGuard
from utils.TaskStore import TaskStore, OCRTask, TaskStatus

# 已完成/失败任务的 TTL（秒），默认 7 天
TASK_TTL_SECONDS:      int = 7 * 24 * 3600


class TaskQueueManager:
    """
    单例任务队列管理器。
    - asyncio.Queue 负责调度，TaskStore 负责持久化
    - 启动 N 个异步 worker 消费队列，每个 worker 在线程池中执行阻塞 OCR
    """

    _instance: Optional["TaskQueueManager"] = None

    def __new__(cls, **kwargs) -> "TaskQueueManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        max_workers:   int = 2,
        db_path:       str = "tasks.db",
        soft_limit_mb: int = 2048,
        hard_limit_mb: int = 3072,
        max_queue_size: int = 100,
    ):
        if self._initialized:
            return
        self._queue: asyncio.Queue[OCRTask] = asyncio.Queue(maxsize=max_queue_size)
        self._store        = TaskStore(db_path=db_path)
        self._max_workers  = max_workers
        self._memory_guard = MemoryGuard(
            soft_limit_mb=soft_limit_mb,
            hard_limit_mb=hard_limit_mb,
        )
        self._initialized  = True
        self._background_tasks: list[asyncio.Task] = []
        logger.info(
            "TaskQueueManager 初始化完成，worker=%d  db=%s  %s",
            max_workers, db_path, self._memory_guard.report(),
        )

    # ── 公共接口 ──────────────────────────────────────────────────────

    def enqueue(
        self,
        file_path:    str,
        file_type:    str,
        callback_url: Optional[str] = None,
    ) -> OCRTask:
        """创建 OCR 任务 → 持久化 → 推入队列，返回任务对象。"""
        task = OCRTask(
            task_id      = str(uuid.uuid4()),
            file_path    = file_path,
            file_type    = file_type,
            callback_url = callback_url,
        )
        self._store.save(task)
        self._queue.put_nowait(task)
        logger.info("[%s] 任务入队  file_type=%s  file_path=%s",
                    task.task_id, file_type, file_path)
        return task

    def get_task(self, task_id: str) -> Optional[OCRTask]:
        """查询任务（缓存优先，miss 时查 SQLite）。"""
        return self._store.get(task_id)

    def list_recent(self, limit: int = 50) -> list[OCRTask]:
        return self._store.list_recent(limit)

    # ── Worker 启动 ───────────────────────────────────────────────────

    def start_workers(self):
        recovered = self._store.recover_unfinished()
        for task in recovered:
            if os.path.exists(task.file_path):
                try:
                    self._queue.put_nowait(task)
                except asyncio.QueueFull:
                    logger.warning("恢复任务超过队列容量，未入队: %s", task.task_id)
                    break
            else:
                self._store.update_status(
                    task.task_id, TaskStatus.FAILED,
                    message="服务重启后源文件不存在", finished_at=time.time(),
                )
        for i in range(self._max_workers):
            self._background_tasks.append(asyncio.create_task(self._worker(i)))
        self._background_tasks.append(asyncio.create_task(self._cleanup_loop()))
        logger.info("已启动 %d 个后台 worker + 定时清理协程", self._max_workers)

    async def stop_workers(self):
        for background_task in self._background_tasks:
            background_task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

    # ── 内部实现 ──────────────────────────────────────────────────────

    async def _worker(self, worker_id: int):
        logger.info("Worker-%d 就绪", worker_id)
        while True:
            task: OCRTask = await self._queue.get()
            await self._memory_guard.check(
                label=f"[{task.task_id[:8]}] Worker-{worker_id}"
            )
            logger.info("[%s] Worker-%d 开始处理  %s",
                        task.task_id, worker_id, self._memory_guard.report())
            try:
                await self._process(task)
            except Exception as exc:
                logger.error("[%s] Worker-%d 未捕获异常: %s",
                             task.task_id, worker_id, exc)
            finally:
                self._queue.task_done()

    async def _cleanup_loop(self):
        from core.settings import settings

        logger.info("定时清理协程已启动，TTL=%ds  间隔=%ds",
                    TASK_TTL_SECONDS, settings.temp_cleanup_interval_seconds)
        while True:
            await asyncio.sleep(settings.temp_cleanup_interval_seconds)
            try:
                cutoff  = time.time() - TASK_TTL_SECONDS
                deleted = self._store.delete_finished_before(cutoff)
                if deleted:
                    logger.info("定时清理：已删除 %d 条过期任务记录", deleted)
                self._cleanup_orphan_files()
            except Exception as exc:
                logger.warning("定时清理出错: %s", exc)

    async def _process(self, task: OCRTask):
        """标记处理中 → 线程池执行 OCR → 更新结果 → 回调通知。"""
        self._store.update_status(
            task.task_id,
            status     = TaskStatus.PROCESSING,
            started_at = time.time(),
        )
        try:
            ocr_result_json = await asyncio.get_event_loop().run_in_executor(
                None, self._run_ocr_pipeline, task
            )
            self._store.update_status(
                task.task_id,
                status      = TaskStatus.SUCCESS,
                message     = "OCR 识别成功",
                finished_at = time.time(),
                ocr_result  = ocr_result_json,
            )
            logger.info("[%s] OCR 完成", task.task_id)

        except Exception as exc:
            self._store.update_status(
                task.task_id,
                status      = TaskStatus.FAILED,
                message     = str(exc),
                finished_at = time.time(),
            )
            logger.error("[%s] OCR 失败: %s", task.task_id, exc)

        # 回调通知（取最新状态）
        latest = self._store.get(task.task_id)
        if latest and latest.callback_url:
            await self._notify(latest)

    @staticmethod
    def _run_ocr_pipeline(task: OCRTask) -> str:
        """
        阻塞执行 OCR 识别，在 executor 线程中运行。
        """
        from utils.OCRDetector import OCRDetector
        from core.settings import settings
        from utils.WordDocumentProcessor import WordDocumentProcessor

        try:
            ocr = OCRDetector()
            if task.file_type == "pdf":
                return TaskQueueManager._ocr_pdf(task, ocr, settings)
            if task.file_type == "word":
                return json.dumps(
                    WordDocumentProcessor.process(task.file_path, ocr), ensure_ascii=False
                )
            return TaskQueueManager._ocr_image(task, ocr)
        finally:
            # 在线程内清理，确保协程被取消时也不会在 OCR 尚未结束前误删源文件。
            _remove_file(task.task_id, task.file_path)

    def _cleanup_orphan_files(self) -> None:
        """删除超期孤儿文件，但保留数据库中尚未完成任务的源文件。"""
        from core.settings import settings

        active_paths = self._store.unfinished_file_paths()
        cutoff = time.time() - settings.temp_file_retention_seconds
        removed = 0
        for directory in (settings.temp_upload_path, settings.temp_image_path):
            if not os.path.isdir(directory):
                continue
            try:
                entries = os.scandir(directory)
            except OSError as exc:
                logger.warning("无法扫描临时目录 %s: %s", directory, exc)
                continue
            with entries:
                for entry in entries:
                    path = os.path.abspath(entry.path)
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if path in active_paths or entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                            continue
                        os.remove(path)
                        removed += 1
                    except OSError as exc:
                        logger.warning("过期临时文件删除失败 %s: %s", path, exc)
        if removed:
            logger.info("定时清理：已删除 %d 个过期孤儿临时文件", removed)

    @staticmethod
    def _ocr_pdf(task: OCRTask, ocr, settings) -> str:
        """PDF → 逐页内存渲染 → OCR → JSON。"""
        import fitz
        import cv2
        import numpy as np

        pages = []
        with fitz.open(task.file_path) as doc:
            if doc.page_count > settings.max_pdf_pages:
                raise ValueError(f"PDF 页数不能超过 {settings.max_pdf_pages} 页")
            zoom = 200 / 72
            mat  = fitz.Matrix(zoom, zoom)
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=mat, alpha=False)
                image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                if pix.n == 3:
                    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                result = ocr.detect(image) or {
                    "rec_texts": [], "rec_scores": [], "rec_polys": [], "dt_polys": []
                }
                pages.append({"page": i + 1, **result})
        logger.info("[%s] PDF OCR 完成 %d 页", task.task_id, len(pages))
        return json.dumps({"file_type": "pdf", "pages": pages}, ensure_ascii=False)

    @staticmethod
    def _ocr_image(task: OCRTask, ocr) -> str:
        """单张图片 OCR → JSON。"""
        result = ocr.detect(task.file_path) or {
            "rec_texts": [], "rec_scores": [], "rec_polys": [], "dt_polys": []
        }
        pages = [{"page": 1, **result}]
        return json.dumps({"file_type": "image", "pages": pages},
                          ensure_ascii=False)

    @staticmethod
    async def _notify(task: OCRTask):
        import httpx
        payload = {
            "task_id":    task.task_id,
            "status":     task.status.value,
            "message":    task.message,
            "cost_s":     round((task.finished_at or 0) - (task.started_at or 0), 2),
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(task.callback_url, json=payload)
                logger.info("[%s] 回调成功 status=%s", task.task_id, resp.status_code)
        except Exception as exc:
            logger.warning("[%s] 回调失败: %s", task.task_id, exc)


# ── 工具函数 ───────────────────────────────────────────────────────────

def _remove_file(task_id: str, path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("[%s] 上传文件清理失败 %s: %s", task_id, path, exc)
