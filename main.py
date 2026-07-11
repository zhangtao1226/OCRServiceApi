# -*-coding : utf-8 -*
# @Author   : zhangTao
# @File     : main.py
# @Time     : 2026/5/25
# @Desc     : OCR 服务接口（异步队列 + SQLite 持久化）

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware

from core.settings import settings
from utils.LoggerDetector import logger
from utils.MemoryGuard import MemoryGuard
from utils.OCRDetector import OCRDetector
from utils.TaskQueueManager import TaskQueueManager
from utils.TaskStore import TaskStatus
from utils.ResponseUtil import ResponseUtil

# ── 支持的上传格式 ──────────────────────────────────────────────────────
_ALLOWED_IMAGE_MIME = {
    "image/jpeg", "image/png", "image/tiff",
    "image/bmp", "image/webp",
}
_ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}
_ALLOWED_WORD_MIME = {
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_ALLOWED_WORD_EXT = {".doc", ".docx"}

# 上传文件临时存储目录（与 OCR 临时图片目录分开）
UPLOAD_DIR: str = getattr(settings, "temp_upload_path", "uploads")

task_manager = TaskQueueManager(
    max_workers   = settings.max_workers,
    db_path       = settings.task_db_path,
    soft_limit_mb = settings.memory_soft_limit_mb,
    hard_limit_mb = settings.memory_hard_limit_mb,
    max_queue_size = settings.max_queue_size,
)
memory_guard = MemoryGuard(
    soft_limit_mb = settings.memory_soft_limit_mb,
    hard_limit_mb = settings.memory_hard_limit_mb,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. OS 级内存硬限制（fork 前设置）
    memory_guard.apply_hard_limit()
    logger.info("内存配置：%s", memory_guard.report())

    # 2. 预热 OCR 模型（放线程池，不卡事件循环）
    logger.info("服务启动：开始预加载 OCR 离线模型 ···")
    try:
        await asyncio.get_event_loop().run_in_executor(None, OCRDetector.warmup)
        logger.info("OCR 模型预加载完成，%s", memory_guard.report())
    except Exception as e:
        logger.error("OCR 模型预加载失败，请检查模型路径配置: %s", e)
        raise

    # 3. 创建必要目录
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(settings.temp_image_path, exist_ok=True)

    # 4. 启动后台 worker
    task_manager.start_workers()
    logger.info("后台 worker 已就绪")

    try:
        yield
    finally:
        await task_manager.stop_workers()
        logger.info("应用关闭")


app = FastAPI(lifespan=lifespan, redirect_slashes=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 健康检查 ───────────────────────────────────────────────────────────
@app.get("/api/v1")
async def root():
    return ResponseUtil.success(message="OCR 服务运行中")


# ── 提交 OCR 任务（接收文件流）────────────────────────────────────────
@app.post("/api/v1/ocr")
async def create_ocr_task(
    file: UploadFile = File(...),
    callback_url: Optional[str] = Form(default=None),
):
    """
    提交 OCR 识别任务，立即返回 task_id，后台异步处理。

    支持格式：PDF、JPEG、PNG、TIFF、BMP、WebP
    可选表单字段 callback_url：任务完成后将以 POST 回调通知。

    返回示例：
    {
        "code":       200,
        "task_id":    "xxxxxxxx-...",
        "status_url": "/api/v1/ocr/status/{task_id}",
        "result_url": "/api/v1/ocr/result/{task_id}"
    }
    """
    # 判断文件类型
    content_type = (file.content_type or "").lower()
    ext = os.path.splitext(file.filename or "")[-1].lower()

    if content_type == "application/pdf" or ext == ".pdf":
        file_type = "pdf"
    elif content_type in _ALLOWED_IMAGE_MIME or ext in _ALLOWED_IMAGE_EXT:
        file_type = "image"
    elif content_type in _ALLOWED_WORD_MIME or ext in _ALLOWED_WORD_EXT:
        file_type = "word"
    else:
        return ResponseUtil.unsupported_media_type(file_type=ext or content_type or "未知")


    # 保存上传文件到临时目录
    suffix    = ext or f".{file_type}"
    save_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}{suffix}")
    try:
        size = 0
        max_bytes = settings.max_upload_size_mb * 1024 * 1024
        with open(save_path, "wb") as fp:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"上传文件不能超过 {settings.max_upload_size_mb} MB",
                    )
                await asyncio.to_thread(fp.write, chunk)
    except HTTPException:
        if os.path.exists(save_path):
            os.remove(save_path)
        raise
    except Exception as e:
        logger.error("文件保存失败: %s", e)
        return ResponseUtil.server_error(message=f"文件保存失败: {e}")

    try:
        task = task_manager.enqueue(
            file_path=save_path, file_type=file_type, callback_url=callback_url,
        )
    except asyncio.QueueFull:
        if os.path.exists(save_path):
            os.remove(save_path)
        raise HTTPException(status_code=503, detail="任务队列已满，请稍后重试")
    logger.info("OCR 任务入队 task_id=%s  file_type=%s", task.task_id, file_type)

    resp = {
        "message":    "任务已提交，正在排队处理",
        "status":     "Queued",
        "task_id":    task.task_id,
        "status_url": f"/api/v1/ocr/status/{task.task_id}",
        "result_url": f"/api/v1/ocr/result/{task.task_id}",
    }
    return ResponseUtil.accepted(resp, message="任务已提交")


# ── 查询任务状态 ───────────────────────────────────────────────────────
@app.get("/api/v1/ocr/status/{task_id}")
async def get_task_status(task_id: str):
    """
    查询 OCR 任务当前状态。
    status 取值：pending / processing / success / failed
    """
    task = task_manager.get_task(task_id)
    if task is None:
        return ResponseUtil.not_found(message=f"任务不存在: {task_id}")

    resp = {
        "task_id":     task.task_id,
        "status":      task.status.value,
        "message":     task.message,
        "file_type":   task.file_type,
        "created_at":  task.created_at,
        "started_at":  task.started_at,
        "finished_at": task.finished_at,
    }
    if task.started_at and task.finished_at:
        resp["cost_s"] = round(task.finished_at - task.started_at, 2)

    if task.status == TaskStatus.SUCCESS:
        resp["result_url"] = f"/api/v1/ocr/result/{task.task_id}"

    return ResponseUtil.success(resp)


# ── 获取 OCR 识别结果 ──────────────────────────────────────────────────
@app.get("/api/v1/ocr/result/{task_id}")
async def get_ocr_result(task_id: str):
    """
    获取 OCR 识别结果。
    - 任务未完成时返回当前状态，告知客户端等待
    - 任务成功时返回结构化识别结果

    结果格式：
    {
        "file_type": "pdf" | "image",
        "pages": [
            {
                "page":       1,
                "rec_texts":  ["文字1", "文字2", ...],
                "rec_scores": [0.98, 0.95, ...],
                "rec_polys":  [[坐标], ...],
                "dt_polys":   [[坐标], ...]
            },
            ...
        ]
    }
    """
    task = task_manager.get_task(task_id)
    if task is None:
        return ResponseUtil.not_found(message=f"任务不存在: {task_id}")

    if task.status == TaskStatus.PENDING:
        return ResponseUtil.accepted(message="任务排队中，请稍后再查")

    if task.status == TaskStatus.PROCESSING:
        return ResponseUtil.accepted(message="任务识别中，请稍后再查")

    if task.status == TaskStatus.FAILED:
        return ResponseUtil.server_error(message=f"任务失败: {task.message}")

    try:
        ocr_result = json.loads(task.ocr_result) if task.ocr_result else {}
    except Exception:
        ocr_result = {}

    resp = {
        "status":      task.status.value,
        "task_id":     task.task_id,
        "cost_s":      round((task.finished_at or 0) - (task.started_at or 0), 2),
        "finished_at": task.finished_at,
        "ocr_result":  ocr_result,
    }
    return ResponseUtil.success(data=resp)


# ── 查询最近任务列表（调试 / 管理用）──────────────────────────────────
@app.get("/api/v1/ocr/tasks")
async def list_tasks(limit: int = Query(default=20, ge=1, le=100)):
    """返回最近 limit 条任务记录（从 SQLite 查询，上限 100）。"""
    tasks = task_manager.list_recent(limit)
    resp = {
        "total": len(tasks),
        "tasks": [
            {
                "task_id":    t.task_id,
                "status":     t.status.value,
                "file_type":  t.file_type,
                "message":    t.message,
                "created_at": t.created_at,
                "finished_at": t.finished_at,
            }
            for t in tasks
        ],
    }
    return ResponseUtil.success(data=resp)


# ── 回调测试接口 ───────────────────────────────────────────────────────
@app.post("/api/v1/ocr/test_callback")
async def test_callback(request: Request):
    body = await request.json()
    logger.info("收到回调: %s", body)
    return {"code": 200, "message": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
