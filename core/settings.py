# -*-coding  : utf-8 -*-
# @Author    : zhangtao
# @File      : settings.py
# @Desc      : 
# @Time      : 2025/11/21 14:35
# @Software  : PyCharm

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


current_file_path = Path(__file__).resolve()
root_path = current_file_path.parent.parent
class Settings(BaseSettings):

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # OCR模型路径
    ocr_path:str = f"{root_path}/models"

    # 临时图片输出路径
    temp_image_path:str = f"{root_path}/output/images"

    temp_upload_path: str = f"{root_path}/uploads"
    max_upload_size_mb: int = 100
    max_pdf_pages: int = 200
    max_queue_size: int = 100
    temp_file_retention_seconds: int = 24 * 3600
    temp_cleanup_interval_seconds: int = 3600

    # 临时ocr结果json文件保存路径
    ocr_json_result_path:str = f"{root_path}/output/json_files"

    # 数据库地址
    task_db_path:str = f"{root_path}/task_db/tasks.db"

    # 软限制：worker 等待阈值, 默认 4G
    memory_soft_limit_mb: int = 4096
    # 硬限制：OS RLIMIT_AS 上限, 默认 5G
    memory_hard_limit_mb: int = 5120

    max_workers: int = 1

    # 服务地址配置
    SERVER_HOST: str = "127.0.0.1"
    SERVER_PORT: int = 8000

    # 日志文件配置信息
    log_info: dict = {
        "log_name": "app",
        "log_level": "INFO",
        "log_dir": f"{root_path}/logs",
        "log_size": 50,
        "log_retention": 7,
    }

settings = Settings()
