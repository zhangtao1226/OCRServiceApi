# -*-coding  : utf-8 -*-
# @Author    : zhangtao
# @File      : LoggerDetector.py
# @Desc      : 
# @Time      : 2025/8/15 16:42
# @Software  : PyCharm

import os
import re
import glob
import time
import logging
from pathlib import Path
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from core.settings import settings

class SizeAndTimeRotatingHandler(TimedRotatingFileHandler):
    def __init__(self, filename, when='M', interval=1, backupCount=7, maxBytes=10 * 1024 * 1024,
                 encoding='utf-8', delay=False, utc=False):

        super().__init__(filename, when=when, interval=interval, backupCount=backupCount, encoding=encoding,
                         delay=delay, utc=utc)

        self.maxBytes = maxBytes
        self.suffix = "%Y-%m-%d"
        self.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}(\.\d+)?$")

    def shouldRollover(self, record):
        if self.stream is None:
            self.stream = self._open()

        if self.maxBytes > 0:
            try:
                msg = self.format(record)
                self.stream.seek(0, 2)
                if self.stream.tell() + len(msg.encode(self.encoding)) > self.maxBytes:
                    return 1
            except ValueError:
                return 1

        return super().shouldRollover(record)

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None

        current_time = datetime.now()
        time_str = current_time.strftime(self.suffix)
        base_name = self.baseFilename

        new_filename = f"{base_name}.{time_str}"
        index = 1

        while os.path.exists(f"{new_filename}.{index}" if index > 1 else new_filename):
            if index == 1:
                new_filename = f"{new_filename}.{index}"
            else:
                new_filename = f"{new_filename.rsplit('.', 1)[0]}.{index}"

            index += 1

        try:
            if os.path.exists(base_name):
                os.rename(base_name, new_filename)
        except Exception as e:
            print(f"日志文件重命名失败: {e}")

        self._cleanup_odl_logs()

        if not self.delay:
            self.stream = self._open()

    def _cleanup_odl_logs(self):
        if self.backupCount <= 0:
            return

        log_dir = os.path.dirname(self.baseFilename)
        file_prefix = os.path.basename(self.baseFilename)
        log_files = sorted(glob.glob(os.path.join(log_dir, f"{file_prefix}.*")), key=os.path.getmtime)

        expire_timestamp = time.time() - (self.backupCount * 86400)

        for file_path in log_files:
            try:
                file_mtime = os.path.getmtime(file_path)
                if file_mtime < expire_timestamp:
                    os.remove(file_path)
                    log_files.remove(file_path)
                    continue
            except Exception as e:
                print(f"清理日志文件失败; {file_path}: {e}")

        retain_files = log_files[-self.backupCount:]
        for file_path in log_files:
            if file_path not in retain_files:
                try:
                    os.remove(file_path)
                except Exception as e:
                    print(f"清理日志文件失败; {file_path}:{e}")

def setup_logger(log_dir="logs", log_name="app", max_log_size=10, retention_days=7):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_file_path = os.path.join(log_dir, f"{log_name}.log")

    logger = logging.getLogger(log_name)
    if logger.handlers:
        logger.handlers.clear()

    logger.setLevel(logging.INFO)

    log_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(filename)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = SizeAndTimeRotatingHandler(
        filename=log_file_path,
        when="midnight",
        backupCount=retention_days,
        maxBytes=max_log_size * 1024 * 1024,
        encoding="utf-8"
    )

    file_handler.setFormatter(log_formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

if settings and hasattr(settings, 'log_info'):
    log_config = settings.log_info
    logger = setup_logger(
        log_dir=log_config.get("log_dir", "logs"),
        log_name=log_config.get("log_name", "app"),
        retention_days=log_config.get("retention_days", 7),
        max_log_size=log_config.get("log_size", 10),
    )
else:
    logger = setup_logger(log_dir='logs', log_name='app', retention_days=7, max_log_size=10)
