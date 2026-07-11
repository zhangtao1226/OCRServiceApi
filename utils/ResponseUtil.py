# -*-coding  : utf-8 -*-
# @Author    : zhangtao
# @File      : ResponseUtil.py
# @Desc      : 响应工具类
# @Time      : 2026/5/28 10:17
# @Software  : PyCharm

from typing import Any, TypeVar
from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from schemas.ResponseModel import BaseResponse, ErrorResponse

# 定义泛型变量，用于支持 IDE 的 data 类型推导
T = TypeVar('T')

class ResponseUtil:
    """FastAPI 统一响应工具类"""

    # ==========================================
    # 成功系列响应 (2xx)
    # ==========================================

    @staticmethod
    def success(data: T = None, message: str = "success") -> JSONResponse:
        """成功响应 (200 OK) - 用于查询、常规成功操作"""
        return ResponseUtil._json(status.HTTP_200_OK, message, data=data)

    @staticmethod
    def accepted(data: T = None, message: str = "任务排队中，请稍后再查") -> JSONResponse:
        """已接受请求 (202 Accepted) - 用于异步任务、后台排队耗时操作"""
        return ResponseUtil._json(status.HTTP_202_ACCEPTED, message, data=data)

    @staticmethod
    def created(data: T = None, message: str = "创建成功") -> BaseResponse[T]:
        """创建成功 (201 Created) - 用于 POST 创建新资源"""
        return BaseResponse(
            code=status.HTTP_201_CREATED,
            message=message,
            data=jsonable_encoder(data) if data is not None else None
        )

    @staticmethod
    def updated(data: T = None, message: str = "更新成功") -> BaseResponse[T]:
        """更新成功 (200 OK) - 用于 PUT/PATCH 修改资源"""
        return BaseResponse(
            code=status.HTTP_200_OK,
            message=message,
            data=jsonable_encoder(data) if data is not None else None
        )

    @staticmethod
    def deleted(message: str = "删除成功") -> BaseResponse[None]:
        """删除成功 (200 OK) - 用于 DELETE 删除资源"""
        return BaseResponse(
            code=status.HTTP_200_OK,
            message=message,
            data=None
        )

    # ==========================================
    # 客户端错误系列响应 (4xx)
    # ==========================================

    @staticmethod
    def error(
        code: int = status.HTTP_400_BAD_REQUEST,
        message: str = "请求错误",
        details: Any = None
    ) -> ErrorResponse:
        """常规错误响应 (400 Bad Request) - 兜底的客户端请求错误"""
        return ErrorResponse(
            code=code,
            message=message,
            details=jsonable_encoder(details) if details is not None else None
        )

    @staticmethod
    def unsupported_media_type(file_type: str, details: Any = None) -> JSONResponse:
        """
        不支持的媒体类型 (415 Unsupported Media Type) - 用于文件上传格式校验失败
        """
        message = (
            f"不支持的文件类型: {file_type}，"
            f"仅支持 PDF 与常见图片格式（JPEG/PNG/TIFF/BMP/WebP）"
        )
        return ResponseUtil._json(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, message, details=details)

    @staticmethod
    def unauthorized(message: str = "未登录或登录已过期", details: Any = None) -> ErrorResponse:
        """未授权 (401 Unauthorized) - Token 缺失、错误、过期"""
        return ErrorResponse(
            code=status.HTTP_401_UNAUTHORIZED,
            message=message,
            details=jsonable_encoder(details) if details is not None else None
        )

    @staticmethod
    def forbidden(message: str = "权限不足，拒绝访问", details: Any = None) -> ErrorResponse:
        """禁止访问 (403 Forbidden) - 已登录但没有该操作权限"""
        return ErrorResponse(
            code=status.HTTP_403_FORBIDDEN,
            message=message,
            details=jsonable_encoder(details) if details is not None else None
        )

    @staticmethod
    def not_found(message: str = "请求的资源不存在", details: Any = None) -> JSONResponse:
        """资源不存在 (404 Not Found) - 找不到对应的数据库记录或路由"""
        return ResponseUtil._json(status.HTTP_404_NOT_FOUND, message, details=details)

    @staticmethod
    def conflict(message: str = "资源冲突", details: Any = None) -> ErrorResponse:
        """资源冲突 (409 Conflict) - 如用户名已存在、唯一索引冲突"""
        return ErrorResponse(
            code=status.HTTP_409_CONFLICT,
            message=message,
            details=jsonable_encoder(details) if details is not None else None
        )

    @staticmethod
    def validate_failed(message: str = "参数校验失败", details: Any = None) -> ErrorResponse:
        """参数校验失败 (422 Unprocessable Entity) - 配合 FastAPI Pydantic 校验异常使用"""
        return ErrorResponse(
            code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            message=message,
            details=jsonable_encoder(details) if details is not None else None
        )

    # ==========================================
    # 服务端错误系列响应 (5xx)
    # ==========================================

    @staticmethod
    def server_error(message: str = "服务器内部错误，请稍后再试", details: Any = None) -> JSONResponse:
        """系统内部错误 (500 Internal Server Error) - 代码抛出未捕获异常时使用"""
        return ResponseUtil._json(status.HTTP_500_INTERNAL_SERVER_ERROR, message, details=details)

    @staticmethod
    def _json(code: int, message: str, data: Any = None, details: Any = None) -> JSONResponse:
        content = {"code": code, "message": message}
        if data is not None:
            content["data"] = jsonable_encoder(data)
        elif details is not None:
            content["details"] = jsonable_encoder(details)
        return JSONResponse(status_code=code, content=content)
