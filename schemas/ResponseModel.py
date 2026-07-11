# -*-coding  : utf-8 -*-
# @Author    : zhangtao
# @File      : ResponseModel.py
# @Desc      : 
# @Time      : 2026/5/28 10:16
# @Software  : PyCharm

from pydantic import BaseModel
from typing import Generic, TypeVar, Optional, Any

# 定义泛型变量
T = TypeVar('T')

class BaseResponse(BaseModel, Generic[T]):
    """统一响应结构体"""
    code: int
    message: str
    data: Optional[T] = None

class ErrorResponse(BaseModel):
    """错误响应结构体"""
    code: int
    message: str
    details: Optional[Any] = None
