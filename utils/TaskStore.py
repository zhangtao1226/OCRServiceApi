# -*-coding  : utf-8 -*-
# @Author    : zhangtao
# @File      : TaskStore.py
# @Desc      : 任务持久化存储（SQLite）+ 内存热缓存
# @Time      : 2026/5/25

import sqlite3
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from utils.LoggerDetector import logger


# ── 状态枚举 ──────────────────────────────────────────────────────────
class TaskStatus(str, Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    SUCCESS    = "success"
    FAILED     = "failed"


# ── 任务数据类 ─────────────────────────────────────────────────────────
@dataclass
class OCRTask:
    task_id:      str
    file_path:    str                    # 上传文件的临时存储路径
    file_type:    str                    # "pdf" | "image"
    callback_url: Optional[str]
    created_at:   float                  = field(default_factory=time.time)
    started_at:   Optional[float]        = None
    finished_at:  Optional[float]        = None
    status:       TaskStatus             = TaskStatus.PENDING
    message:      str                    = ""
    ocr_result:   Optional[str]          = None  # JSON 字符串，OCR 识别结果


# ── SQLite 持久化 + 内存缓存 ──────────────────────────────────────────
class TaskStore:
    """
    线程安全的任务存储。
    - 写操作同步写入 SQLite，同时更新内存缓存
    - 读操作优先命中内存缓存；缓存 miss 时回落到 SQLite
    - 内存缓存只保留最近 MAX_CACHE 条，防止长期运行内存膨胀
    - SQLite 按 created_at 保留最近 MAX_ROWS 行，超出自动清理
    """

    MAX_CACHE = 500
    MAX_ROWS  = 100_000
    DB_FILE   = "tasks.db"

    def __init__(self, db_path: str = DB_FILE):
        self._db_path      = db_path
        self._lock         = threading.Lock()
        self._cache: dict[str, OCRTask] = {}
        self._cache_order: list[str]    = []
        self._init_db()
        logger.info("TaskStore 初始化完成  db=%s", db_path)

    # ── 公共写接口 ────────────────────────────────────────────────────

    def save(self, task: OCRTask) -> None:
        """新增或全量更新一条任务，并在记录数超限时自动清理旧数据。"""
        with self._lock:
            self._upsert_db(task)
            self._set_cache(task)
            self._trim_db()

    def update_status(
        self,
        task_id:     str,
        status:      TaskStatus,
        message:     str            = "",
        started_at:  Optional[float] = None,
        finished_at: Optional[float] = None,
        ocr_result:  Optional[str]   = None,
    ) -> None:
        """局部更新任务状态字段（只改变传入的非 None 字段）。"""
        task = self.get(task_id)
        if task is None:
            logger.warning("update_status: task_id=%s 不存在", task_id)
            return
        task.status  = status
        task.message = message
        if started_at  is not None: task.started_at  = started_at
        if finished_at is not None: task.finished_at = finished_at
        if ocr_result  is not None: task.ocr_result  = ocr_result
        self.save(task)

    # ── 公共读接口 ────────────────────────────────────────────────────

    def get(self, task_id: str) -> Optional[OCRTask]:
        """按 task_id 查询，缓存 miss 时读 SQLite。"""
        with self._lock:
            if task_id in self._cache:
                return self._cache[task_id]
            return self._load_from_db(task_id)

    def list_recent(self, limit: int = 50) -> list[OCRTask]:
        """返回最新的 limit 条任务（从 SQLite 查，保证持久数据可见）。"""
        sql = """
            SELECT task_id, file_path, file_type, callback_url,
                   created_at, started_at, finished_at,
                   status, message, ocr_result
            FROM tasks
            ORDER BY created_at DESC
            LIMIT ?
        """
        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, (limit,)).fetchall()
            conn.close()
        return [self._row_to_task(r) for r in rows]

    def recover_unfinished(self) -> list[OCRTask]:
        """服务重启时把 pending/processing 任务恢复为 pending。"""
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE tasks SET status='pending', started_at=NULL, finished_at=NULL "
                "WHERE status IN ('pending', 'processing')"
            )
            rows = conn.execute(
                "SELECT task_id, file_path, file_type, callback_url, created_at, "
                "started_at, finished_at, status, message, ocr_result FROM tasks "
                "WHERE status='pending' ORDER BY created_at ASC"
            ).fetchall()
            conn.commit()
            conn.close()
        return [self._row_to_task(row) for row in rows]

    def delete_finished_before(self, cutoff_ts: float) -> int:
        """
        删除 finished_at < cutoff_ts 且状态为 success/failed 的任务记录。
        同步清理内存缓存，返回实际删除行数。
        """
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """
                DELETE FROM tasks
                WHERE finished_at IS NOT NULL
                  AND finished_at < ?
                  AND status IN ('success', 'failed')
                """,
                (cutoff_ts,),
            )
            deleted = cur.rowcount
            conn.commit()
            conn.close()

            if deleted:
                stale = [
                    tid for tid, t in self._cache.items()
                    if t.finished_at and t.finished_at < cutoff_ts
                    and t.status in (TaskStatus.SUCCESS, TaskStatus.FAILED)
                ]
                for tid in stale:
                    self._cache.pop(tid, None)
                    try:
                        self._cache_order.remove(tid)
                    except ValueError:
                        pass

        return deleted

    # ── 内部：SQLite 操作 ─────────────────────────────────────────────

    # 当前表结构所需的列名集合，与下方 CREATE TABLE 保持同步
    _REQUIRED_COLUMNS = {
        "task_id", "file_path", "file_type", "callback_url",
        "created_at", "started_at", "finished_at",
        "status", "message", "ocr_result",
    }

    def _init_db(self) -> None:
        conn = self._connect()

        # 若表已存在但列不匹配（旧版 schema），直接重建
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if existing and existing != self._REQUIRED_COLUMNS:
            logger.warning(
                "tasks 表结构与当前版本不符，自动重建（旧数据将清空）。"
                "旧列: %s", existing - self._REQUIRED_COLUMNS or "无差异"
            )
            conn.execute("DROP TABLE IF EXISTS tasks")
            conn.commit()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id      TEXT PRIMARY KEY,
                file_path    TEXT NOT NULL,
                file_type    TEXT NOT NULL,
                callback_url TEXT,
                created_at   REAL NOT NULL,
                started_at   REAL,
                finished_at  REAL,
                status       TEXT NOT NULL DEFAULT 'pending',
                message      TEXT NOT NULL DEFAULT '',
                ocr_result   TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON tasks(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status  ON tasks(status)")
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _upsert_db(self, task: OCRTask) -> None:
        sql = """
            INSERT INTO tasks
                (task_id, file_path, file_type, callback_url,
                 created_at, started_at, finished_at,
                 status, message, ocr_result)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(task_id) DO UPDATE SET
                started_at  = excluded.started_at,
                finished_at = excluded.finished_at,
                status      = excluded.status,
                message     = excluded.message,
                ocr_result  = excluded.ocr_result
        """
        conn = self._connect()
        conn.execute(sql, (
            task.task_id, task.file_path, task.file_type, task.callback_url,
            task.created_at, task.started_at, task.finished_at,
            task.status.value, task.message, task.ocr_result,
        ))
        conn.commit()
        conn.close()

    def _load_from_db(self, task_id: str) -> Optional[OCRTask]:
        sql = """
            SELECT task_id, file_path, file_type, callback_url,
                   created_at, started_at, finished_at,
                   status, message, ocr_result
            FROM tasks WHERE task_id = ?
        """
        conn = self._connect()
        row  = conn.execute(sql, (task_id,)).fetchone()
        conn.close()
        if row is None:
            return None
        task = self._row_to_task(row)
        self._set_cache(task)
        return task

    def _trim_db(self) -> None:
        """当行数超过 MAX_ROWS 时，删除最旧的 10%（在锁内调用）。"""
        conn  = self._connect()
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if count > self.MAX_ROWS:
            delete_n = count - int(self.MAX_ROWS * 0.9)
            conn.execute("""
                DELETE FROM tasks WHERE task_id IN (
                    SELECT task_id FROM tasks
                    ORDER BY created_at ASC LIMIT ?
                )
            """, (delete_n,))
            conn.commit()
            logger.info("TaskStore 自动清理旧记录 %d 条（当前共 %d 条）", delete_n, count)
        conn.close()

    # ── 内部：内存缓存 LRU 管理 ───────────────────────────────────────

    def _set_cache(self, task: OCRTask) -> None:
        if task.task_id in self._cache:
            self._cache[task.task_id] = task
            return
        if len(self._cache_order) >= self.MAX_CACHE:
            oldest = self._cache_order.pop(0)
            self._cache.pop(oldest, None)
        self._cache[task.task_id] = task
        self._cache_order.append(task.task_id)

    @staticmethod
    def _row_to_task(row) -> OCRTask:
        return OCRTask(
            task_id      = row["task_id"],
            file_path    = row["file_path"],
            file_type    = row["file_type"],
            callback_url = row["callback_url"],
            created_at   = row["created_at"],
            started_at   = row["started_at"],
            finished_at  = row["finished_at"],
            status       = TaskStatus(row["status"]),
            message      = row["message"] or "",
            ocr_result   = row["ocr_result"],
        )
