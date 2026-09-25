"""请求 / 响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class CheckinCreate(BaseModel):
    event_no: str = Field(..., min_length=1, max_length=64, description="唯一事件号")
    runner_id: str = Field(..., min_length=1, max_length=64, description="选手号")
    station_seq: int = Field(..., ge=1, description="站点序号（从 1 开始）")
    occurred_at: datetime = Field(..., description="打卡发生时间，ISO 8601")
    corrects: Optional[str] = Field(
        default=None, description="要更正的原事件号；填写后原事件被本事件取代"
    )


class CheckinOut(BaseModel):
    event_no: str
    runner_id: str
    station_seq: int
    occurred_at: datetime
    status: str = Field(..., description="active=有效，superseded=已被更正，revoked=已撤销")
    corrects: Optional[str]
    created_at: datetime


class StationTime(BaseModel):
    station_seq: int
    event_no: str
    occurred_at: datetime


class Segment(BaseModel):
    from_station: int
    to_station: int
    status: Literal["ok", "pending"]
    duration_seconds: Optional[int] = Field(default=None, description="分段用时（秒）")
    duration: Optional[str] = Field(default=None, description="分段用时，H:MM:SS")
    reason: Optional[str] = Field(default=None, description="待定原因")


class SplitsOut(BaseModel):
    runner_id: str
    station_count: int
    complete: bool = Field(..., description="所有分段均有效时为 true")
    total_seconds: Optional[int] = Field(default=None, description="总用时（秒），仅 complete 时给出")
    total: Optional[str] = Field(default=None, description="总用时，H:MM:SS")
    stations: list[StationTime] = Field(..., description="各站点采用的有效打卡（同站取最早）")
    segments: list[Segment]


class RunnerResultSummary(BaseModel):
    complete: bool = Field(..., description="所有分段均有效时为 true")
    total_seconds: Optional[int] = Field(default=None, description="总用时（秒），仅 complete 时给出")
    total: Optional[str] = Field(default=None, description="总用时，H:MM:SS")


class SegmentChange(BaseModel):
    from_station: int
    to_station: int
    before: Optional[Segment] = Field(
        default=None, description="更正前该分段；选手此前无任何打卡记录时为 null"
    )
    after: Optional[Segment] = Field(default=None, description="更正后该分段")


class RunnerCorrectionPreview(BaseModel):
    runner_id: str
    before: RunnerResultSummary = Field(..., description="更正前的完成状态与总用时")
    after: RunnerResultSummary = Field(..., description="更正后的完成状态与总用时")
    changed_segments: list[SegmentChange] = Field(..., description="仅列出发生变化的分段")


class CorrectionPreviewOut(BaseModel):
    event_no: str = Field(..., description="预演的更正事件号（未落库，可再次正式提交）")
    runner_id: str
    station_seq: int
    occurred_at: datetime
    corrects: str = Field(..., description="被更正的原事件号")
    runners: list[RunnerCorrectionPreview] = Field(
        ..., description="受影响选手的前后结果；更正跨选手时包含原选手与新选手"
    )
