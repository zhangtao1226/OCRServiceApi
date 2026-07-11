# -*-coding  : utf-8 -*-
# @Author    : zhangtao
# @File      : MemoryGuard.py
# @Desc      : 内存限制守卫（软限制 + 硬限制）
# @Time      : 2026/5/26

import os
import asyncio
import platform
from typing import Optional

from utils.LoggerDetector import logger

# ── 默认阈值（可通过 MemoryGuard(…) 参数覆盖）────────────────────────────
# 软限制：当前进程 RSS 超过此值时，worker 暂停等待（不崩溃）
DEFAULT_SOFT_LIMIT_MB: int = 2048   # 2 GB

# 硬限制：OS 级别虚拟内存上限，超出会触发 MemoryError 而非 SIGKILL
# 设为软限制的 1.5 倍，留出缓冲区让当前任务能安全写出 PDF 再报错
DEFAULT_HARD_LIMIT_MB: int = 3072   # 3 GB

# worker 等待内存释放的轮询间隔（秒）
MEMORY_WAIT_INTERVAL: float = 5.0

# 最长等待时间（秒），超时后记录警告并强制继续，避免任务永久阻塞
MEMORY_WAIT_TIMEOUT: float = 120.0


class MemoryGuard:
    """
    内存使用守卫。

    软限制（soft_limit_mb）
        在 worker 每次取任务前调用 await check()，
        若进程 RSS 超限则异步等待，直到内存回落或超时。
        不会主动终止进程，任务不丢失。

    硬限制（hard_limit_mb）
        服务启动时调用 apply_hard_limit() 向 OS 注册 RLIMIT_AS，
        作为最后一道防线；仅 Linux/macOS 有效，Windows 自动跳过。
    """

    def __init__(self, soft_limit_mb: int = DEFAULT_SOFT_LIMIT_MB, hard_limit_mb: int = DEFAULT_HARD_LIMIT_MB) -> None:
        self.soft_limit_bytes = soft_limit_mb * 1024 * 1024
        self.hard_limit_bytes = hard_limit_mb * 1024 * 1024
        self._psutil = self._import_psutil()
        self._proc   = self._psutil.Process(os.getpid()) if self._psutil else None

    # ── 公共接口 ─────────────────────────────────────────────────────────

    def apply_hard_limit(self) -> None:
        """
        设置 OS 级虚拟内存硬限制（仅 Linux/macOS）。
        必须在服务启动时调用（fork 子进程前），之后调用无效。
        """
        if platform.system() == "Windows":
            logger.info("硬限制：Windows 平台不支持 RLIMIT_AS，已跳过")
            return

        try:
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            new_soft   = self.hard_limit_bytes
            # 不能超过系统硬上限（hard == -1 表示无限制）
            new_hard   = hard if hard != resource.RLIM_INFINITY else new_soft
            resource.setrlimit(resource.RLIMIT_AS, (new_soft, new_hard))
            logger.info(f"内存硬限制已设置：{self.hard_limit_bytes // 1024 // 1024} MB（RLIMIT_AS）")
        except Exception as e:
            # 容器/统信 UOS 环境可能无权限设置，记录警告不中断启动
            logger.warning(f"内存硬限制设置失败（将依赖软限制）: {str(e)}")

    async def check(self, label: str = "") -> None:
        """
        异步软限制检查：若 RSS 超限则等待，直到内存回落或超时。
        在 worker 每次取出任务后、开始 pipeline 前调用。
        """
        if self._proc is None:
            return  # psutil 不可用，跳过检查

        waited = 0.0
        while True:
            rss_mb = self._rss_mb()
            limit_mb = self.soft_limit_bytes // 1024 // 1024

            if rss_mb < limit_mb:
                if waited > 0:
                    logger.info(f"{label} 内存已回落至 {rss_mb} MB，继续处理")
                return

            if waited == 0:
                logger.warning(f"{label} 内存使用 {rss_mb} MB 超过软限制 {limit_mb} MB，暂停等待释放 ···")

            if waited >= MEMORY_WAIT_TIMEOUT:
                logger.warning(f"{label} 等待超时（{int(MEMORY_WAIT_TIMEOUT)}），当前内存 {rss_mb} MB，强制继续（任务可能失败）")
                return

            await asyncio.sleep(MEMORY_WAIT_INTERVAL)
            waited += MEMORY_WAIT_INTERVAL

    def current_mb(self) -> Optional[int]:
        """返回当前进程 RSS（MB），psutil 不可用时返回 None。"""
        return self._rss_mb() if self._proc else None

    def report(self) -> str:
        """返回可打印的内存状态字符串。"""
        rss = self.current_mb()
        if rss is None:
            return "内存监控不可用（psutil 未安装）"
        soft_mb  = self.soft_limit_bytes  // 1024 // 1024
        hard_mb  = self.hard_limit_bytes  // 1024 // 1024
        pct      = rss / soft_mb * 100
        return (f"内存使用: {rss} MB / 软限制 {soft_mb} MB ({pct:.1f}%)，硬限制 {hard_mb} MB")

    # ── 内部工具 ─────────────────────────────────────────────────────────

    def _rss_mb(self) -> int:
        try:
            return self._proc.memory_info().rss // 1024 // 1024
        except Exception:
            return 0

    @staticmethod
    def _import_psutil():
        try:
            import psutil
            return psutil
        except ImportError:
            logger.warning("psutil 未安装，内存软限制监控不可用。请执行: pip install psutil")
            return None
