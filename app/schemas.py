"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import datetime

from pydantic import BaseModel, Field, model_validator


class ReportIn(BaseModel):
    lab_no: str = Field(..., min_length=1, description="实验室号（非空）")
    report_no: str = Field(..., min_length=1, description="实验室内报告号（非空）")
    measured: int = Field(..., description="整数测值")


class ReportOut(ReportIn):
    id: int


class BatchReportIn(BaseModel):
    items: list[ReportIn] = Field(..., min_length=1)


# ------------------------------ 质控放行 ---------------------------------

class QcVerifyIn(BaseModel):
    lab_no: str = Field(..., min_length=1, description="实验室号（非空）")
    report_date: datetime.date = Field(
        ..., description="核对的 UTC 日历日（ISO 日期，如 2026-11-18）"
    )
    lower_bound: int = Field(..., description="测值下限（含，整数）")
    upper_bound: int = Field(..., description="测值上限（含，整数）")

    @model_validator(mode="after")
    def _check_bounds(self) -> "QcVerifyIn":
        if self.lower_bound > self.upper_bound:
            raise ValueError("lower_bound 不能大于 upper_bound")
        return self


class QcReleaseOut(BaseModel):
    lab_no: str
    report_date: datetime.date
    lower_bound: int
    upper_bound: int
    status: str = Field(..., description="released=当前放行；revoked=当前撤销")
    checked_report_count: int
    violation_count: int
    violation_ids: list[int]
    revoke_reason: str | None = None
    checked_at: datetime.datetime


class SupplyReportOut(BaseModel):
    id: int
    lab_no: str
    report_no: str
    measured: int
    created_at: datetime.datetime
    report_date: datetime.date


class SupplyListOut(BaseModel):
    total: int
    items: list[SupplyReportOut]
