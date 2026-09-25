"""核心业务逻辑：打卡写入 / 更正 / 撤销 / 分段重算。"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from .models import (
    CheckinCreate,
    CheckinOut,
    CorrectionPreviewOut,
    RunnerCorrectionPreview,
    RunnerResultSummary,
    Segment,
    SegmentChange,
    SplitsOut,
    StationTime,
)

ACTIVE = "active"
SUPERSEDED = "superseded"
REVOKED = "revoked"


def station_count() -> int:
    """赛道站点总数（分段数 = 站点数 - 1）。"""
    return int(os.environ.get("STATION_COUNT", "6"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    """统一为 UTC；无时区信息的时间按 UTC 处理。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_duration(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h}:{m:02d}:{s:02d}"


def _row_to_out(row: sqlite3.Row) -> CheckinOut:
    return CheckinOut(
        event_no=row["event_no"],
        runner_id=row["runner_id"],
        station_seq=row["station_seq"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        status=row["status"],
        corrects=row["corrects"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _get_row(conn: sqlite3.Connection, event_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM checkins WHERE event_no = ?", (event_no,)
    ).fetchone()


def _validate_checkin(
    conn: sqlite3.Connection, payload: CheckinCreate
) -> sqlite3.Row | None:
    """站点范围 / 事件号唯一性 / 更正目标校验（正式登记与更正预演共用）。

    返回被更正的原事件行；payload.corrects 为空时返回 None。
    """
    count = station_count()
    if payload.station_seq > count:
        raise HTTPException(
            status_code=422,
            detail=f"站点序号 {payload.station_seq} 超出范围（1..{count}）",
        )
    if _get_row(conn, payload.event_no) is not None:
        raise HTTPException(status_code=409, detail=f"事件号 {payload.event_no} 已存在")

    origin = None
    if payload.corrects is not None:
        origin = _get_row(conn, payload.corrects)
        if origin is None:
            raise HTTPException(
                status_code=404, detail=f"被更正的事件 {payload.corrects} 不存在"
            )
        if origin["status"] != ACTIVE:
            raise HTTPException(
                status_code=409,
                detail=f"事件 {payload.corrects} 状态为 {origin['status']}，不能更正；"
                "请更正当前有效的替代事件",
            )
    return origin


def _apply_checkin(conn: sqlite3.Connection, payload: CheckinCreate) -> CheckinOut:
    """写入打卡；携带 corrects 时把原事件标记为 superseded。事务边界由调用方负责。"""
    occurred = _to_utc(payload.occurred_at)
    created = _now()
    if payload.corrects is not None:
        conn.execute(
            "UPDATE checkins SET status = ? WHERE event_no = ?",
            (SUPERSEDED, payload.corrects),
        )
    conn.execute(
        "INSERT INTO checkins"
        " (event_no, runner_id, station_seq, occurred_at, status, corrects, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            payload.event_no,
            payload.runner_id,
            payload.station_seq,
            occurred.isoformat(),
            ACTIVE,
            payload.corrects,
            created.isoformat(),
        ),
    )
    return CheckinOut(
        event_no=payload.event_no,
        runner_id=payload.runner_id,
        station_seq=payload.station_seq,
        occurred_at=occurred,
        status=ACTIVE,
        corrects=payload.corrects,
        created_at=created,
    )


def create_checkin(conn: sqlite3.Connection, payload: CheckinCreate) -> CheckinOut:
    """登记打卡；携带 corrects 时视为更正，原事件被取代。"""
    _validate_checkin(conn, payload)
    with conn:  # 事务：更正与写入同时成功或失败
        return _apply_checkin(conn, payload)


def revoke_checkin(conn: sqlite3.Connection, event_no: str) -> CheckinOut:
    """按事件号撤销一条有效打卡。"""
    row = _get_row(conn, event_no)
    if row is None:
        raise HTTPException(status_code=404, detail=f"事件 {event_no} 不存在")
    if row["status"] != ACTIVE:
        raise HTTPException(
            status_code=409,
            detail=f"事件 {event_no} 状态为 {row['status']}，无需撤销",
        )
    with conn:
        conn.execute(
            "UPDATE checkins SET status = ? WHERE event_no = ?", (REVOKED, event_no)
        )
    return CheckinOut(
        event_no=row["event_no"],
        runner_id=row["runner_id"],
        station_seq=row["station_seq"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        status=REVOKED,
        corrects=row["corrects"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def get_checkin(conn: sqlite3.Connection, event_no: str) -> CheckinOut:
    row = _get_row(conn, event_no)
    if row is None:
        raise HTTPException(status_code=404, detail=f"事件 {event_no} 不存在")
    return _row_to_out(row)


def list_checkins(
    conn: sqlite3.Connection, runner_id: str | None = None
) -> list[CheckinOut]:
    if runner_id is None:
        rows = conn.execute(
            "SELECT * FROM checkins ORDER BY created_at, event_no"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM checkins WHERE runner_id = ? ORDER BY created_at, event_no",
            (runner_id,),
        ).fetchall()
    return [_row_to_out(r) for r in rows]


def get_splits(conn: sqlite3.Connection, runner_id: str) -> SplitsOut:
    """按站点顺序重算某选手的相邻站分段用时。

    - 仅统计 status='active' 的打卡；同站多条有效打卡取最早时间；
    - 相邻站缺打卡或时间倒退的分段标记为 pending 并说明原因；
    - 每次查询都基于最新数据重算，因此补传、更正、撤销即时生效。
    """
    splits = _compute_splits(conn, runner_id)
    if splits is None:
        raise HTTPException(status_code=404, detail=f"选手 {runner_id} 无任何打卡记录")
    return splits


def _compute_splits(conn: sqlite3.Connection, runner_id: str) -> SplitsOut | None:
    """重算选手分段结果；选手无任何打卡记录时返回 None。"""
    any_row = conn.execute(
        "SELECT 1 FROM checkins WHERE runner_id = ? LIMIT 1", (runner_id,)
    ).fetchone()
    if any_row is None:
        return None

    rows = conn.execute(
        "SELECT * FROM checkins WHERE runner_id = ? AND status = ?",
        (runner_id, ACTIVE),
    ).fetchall()

    # 同站多条有效打卡取最早时间
    best: dict[int, tuple[datetime, str]] = {}
    for r in rows:
        t = datetime.fromisoformat(r["occurred_at"])
        cur = best.get(r["station_seq"])
        if cur is None or t < cur[0]:
            best[r["station_seq"]] = (t, r["event_no"])

    count = station_count()
    stations = [
        StationTime(station_seq=seq, event_no=ev, occurred_at=t)
        for seq, (t, ev) in sorted(best.items())
    ]

    segments: list[Segment] = []
    for k in range(1, count):
        a = best.get(k)
        b = best.get(k + 1)
        if a is None and b is None:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=f"站点 {k} 与站点 {k + 1} 均缺少有效打卡",
            )
        elif a is None:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=f"站点 {k} 缺少有效打卡",
            )
        elif b is None:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=f"站点 {k + 1} 缺少有效打卡",
            )
        elif b[0] < a[0]:
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="pending",
                reason=(
                    f"站点 {k + 1} 时间 {b[0].isoformat()} 早于"
                    f"站点 {k} 时间 {a[0].isoformat()}（时间倒退）"
                ),
            )
        else:
            seconds = int((b[0] - a[0]).total_seconds())
            seg = Segment(
                from_station=k,
                to_station=k + 1,
                status="ok",
                duration_seconds=seconds,
                duration=_fmt_duration(seconds),
            )
        segments.append(seg)

    complete = all(s.status == "ok" for s in segments)
    total_seconds = (
        sum(s.duration_seconds for s in segments) if complete else None
    )
    return SplitsOut(
        runner_id=runner_id,
        station_count=count,
        complete=complete,
        total_seconds=total_seconds,
        total=_fmt_duration(total_seconds) if total_seconds is not None else None,
        stations=stations,
        segments=segments,
    )


def preview_correction(
    conn: sqlite3.Connection, payload: CheckinCreate
) -> CorrectionPreviewOut:
    """预演一次更正：套用与正式更正相同的校验与写入，随后整体回滚。

    - 不占用事件号、不改变任何打卡状态或成绩，相同请求随后可正式提交；
    - 按现有同站最早与相邻站规则计算更正前后结果；
    - 更正跨选手时，同时给出原选手与新选手的前后结果。
    """
    if payload.corrects is None:
        raise HTTPException(
            status_code=422,
            detail="预览更正必须携带 corrects 字段（指向被更正的有效事件）",
        )
    origin = _validate_checkin(conn, payload)

    affected: list[str] = []
    for rid in (origin["runner_id"], payload.runner_id):
        if rid not in affected:
            affected.append(rid)

    before = {rid: _compute_splits(conn, rid) for rid in affected}

    conn.execute("SAVEPOINT preview_correction")
    try:
        _apply_checkin(conn, payload)
        after = {rid: _compute_splits(conn, rid) for rid in affected}
    finally:
        # 整体回滚：预演不落库
        conn.execute("ROLLBACK TO SAVEPOINT preview_correction")
        conn.execute("RELEASE SAVEPOINT preview_correction")

    runners = [
        RunnerCorrectionPreview(
            runner_id=rid,
            before=_result_summary(before[rid]),
            after=_result_summary(after[rid]),
            changed_segments=_diff_segments(before[rid], after[rid]),
        )
        for rid in affected
    ]
    return CorrectionPreviewOut(
        event_no=payload.event_no,
        runner_id=payload.runner_id,
        station_seq=payload.station_seq,
        occurred_at=_to_utc(payload.occurred_at),
        corrects=payload.corrects,
        runners=runners,
    )


def _result_summary(splits: SplitsOut | None) -> RunnerResultSummary:
    if splits is None:
        return RunnerResultSummary(complete=False, total_seconds=None, total=None)
    return RunnerResultSummary(
        complete=splits.complete,
        total_seconds=splits.total_seconds,
        total=splits.total,
    )


def _diff_segments(
    before: SplitsOut | None, after: SplitsOut | None
) -> list[SegmentChange]:
    """仅保留发生变化的分段；选手无记录的一侧以 None 表示。"""
    before_map = {s.from_station: s for s in before.segments} if before else {}
    after_map = {s.from_station: s for s in after.segments} if after else {}
    changes = []
    for frm in sorted(before_map.keys() | after_map.keys()):
        b = before_map.get(frm)
        a = after_map.get(frm)
        if b != a:
            changes.append(
                SegmentChange(from_station=frm, to_station=frm + 1, before=b, after=a)
            )
    return changes
