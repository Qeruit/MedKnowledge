from datetime import date  #限制日期
from typing import Literal #限制字段
from uuid import UUID      #限制字段为唯一标识符

from pydantic import BaseModel, Field  #定义可以解析和校验数据的模型，给字段添加默认值、长度、范围等规则


class PageRequest(BaseModel):
    current: int = Field(default=1, ge=1)#默认第一页，大于等于1
    size: int = Field(default=10, ge=1, le=100) #默认每页10条，最少1条，最多15条


class CustomerCompletionRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    fileId: int = Field(gt=0, description="当前问答限定的已发布资料 ID")#大于
    conversationId: UUID | None = None #这个字段可以不传；如果传入，就应该是有效的 UUID


class MarkdownPageRequest(PageRequest):
    fileName: str | None = None
    reviewStatus: Literal["draft", "pending_review", "approved", "rejected", "superseded"] | None = None #审核状态
    startDate: date | None = None
    endDate: date | None = None


class MarkdownIdRequest(BaseModel):
    id: int = Field(gt=0)


class UpdateMarkdownRequest(MarkdownIdRequest):
    remark: str | None = Field(default=None, max_length=5000)


def ok(data=None):
    return {"success": True, "message": None, "errorCode": None, "data": data}


def fail(message: str, code: str | None = None):
    return {"success": False, "message": message, "errorCode": code, "data": None}


def page(data: list, total: int, current: int, size: int):
    return {**ok(data), "total": total, "current": current, "size": size, "pages": (total + size - 1) // size}
