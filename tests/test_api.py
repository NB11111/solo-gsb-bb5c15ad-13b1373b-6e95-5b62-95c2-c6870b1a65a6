"""接口与分段逻辑测试。每个用例使用独立的临时数据库。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("STATION_COUNT", "6")
    with TestClient(app) as c:  # 触发 lifespan：建表 + 写入种子数据
        yield c


def post(client, event_no, runner, station, occurred_at, corrects=None):
    body = {
        "event_no": event_no,
        "runner_id": runner,
        "station_seq": station,
        "occurred_at": occurred_at,
    }
    if corrects is not None:
        body["corrects"] = corrects
    return client.post("/api/checkins", json=body)


def segment(splits, frm, to):
    return next(
        s for s in splits["segments"]
        if s["from_station"] == frm and s["to_station"] == to
    )


# ---------- 种子数据场景 ----------

def test_seed_complete_runner(client):
    r = client.get("/api/runners/R001/splits")
    assert r.status_code == 200
    data = r.json()
    assert data["complete"] is True
    assert len(data["segments"]) == 5
    assert all(s["status"] == "ok" for s in data["segments"])
    # 08:00 -> 11:30 = 3.5 小时
    assert data["total_seconds"] == 3 * 3600 + 30 * 60
    assert data["total"] == "3:30:00"
    assert segment(data, 1, 2)["duration_seconds"] == 32 * 60


def test_seed_out_of_order_backfill(client):
    # R002 的打卡是乱序写入的，但按站点顺序重算后分段完整
    data = client.get("/api/runners/R002/splits").json()
    assert data["complete"] is True
    assert segment(data, 1, 2)["duration_seconds"] == 40 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 30 * 60


def test_seed_missing_station_pending(client):
    data = client.get("/api/runners/R003/splits").json()
    assert data["complete"] is False
    assert data["total_seconds"] is None
    s34 = segment(data, 3, 4)
    s45 = segment(data, 4, 5)
    assert s34["status"] == "pending" and "站点 4" in s34["reason"]
    assert s45["status"] == "pending" and "站点 4" in s45["reason"]
    assert segment(data, 2, 3)["status"] == "ok"


def test_seed_correction_applied(client):
    # R004 第 2 站的错误打卡 12:30 已被更正为 08:40
    data = client.get("/api/runners/R004/splits").json()
    assert data["complete"] is True
    assert segment(data, 1, 2)["duration_seconds"] == 38 * 60
    origin = client.get("/api/checkins/S-R004-2X").json()
    assert origin["status"] == "superseded"


def test_seed_revoked_checkin(client):
    data = client.get("/api/runners/R005/splits").json()
    assert segment(data, 1, 2)["status"] == "pending"
    assert segment(data, 2, 3)["status"] == "pending"
    assert client.get("/api/checkins/S-R005-2").json()["status"] == "revoked"


def test_seed_time_regression_pending(client):
    data = client.get("/api/runners/R006/splits").json()
    s23 = segment(data, 2, 3)
    assert s23["status"] == "pending"
    assert "时间倒退" in s23["reason"]


# ---------- 交互场景 ----------

def test_out_of_order_backfill_fills_gap(client):
    assert post(client, "T-1", "T001", 1, "2026-09-25T08:00:00").status_code == 201
    assert post(client, "T-3", "T001", 3, "2026-09-25T09:00:00").status_code == 201
    data = client.get("/api/runners/T001/splits").json()
    assert segment(data, 1, 2)["status"] == "pending"
    assert segment(data, 2, 3)["status"] == "pending"
    # 乱序补传第 2 站后，缺段被填补
    assert post(client, "T-2", "T001", 2, "2026-09-25T08:30:00").status_code == 201
    data = client.get("/api/runners/T001/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 30 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 30 * 60


def test_same_station_takes_earliest(client):
    post(client, "D-1", "T002", 1, "2026-09-25T08:00:00")
    post(client, "D-2a", "T002", 2, "2026-09-25T09:00:00")
    post(client, "D-2b", "T002", 2, "2026-09-25T08:40:00")  # 更早的同站打卡
    data = client.get("/api/runners/T002/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 40 * 60
    st2 = next(s for s in data["stations"] if s["station_seq"] == 2)
    assert st2["event_no"] == "D-2b"


def test_correction_updates_results(client):
    post(client, "C-1", "T003", 1, "2026-09-25T08:00:00")
    post(client, "C-2X", "T003", 2, "2026-09-25T12:00:00")  # 错误时间
    post(client, "C-3", "T003", 3, "2026-09-25T09:00:00")
    data = client.get("/api/runners/T003/splits").json()
    assert segment(data, 2, 3)["status"] == "pending"  # 时间倒退
    # 更正第 2 站后，受影响分段恢复
    r = post(client, "C-2", "T003", 2, "2026-09-25T08:30:00", corrects="C-2X")
    assert r.status_code == 201
    data = client.get("/api/runners/T003/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 30 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 30 * 60
    assert client.get("/api/checkins/C-2X").json()["status"] == "superseded"


def test_revoke_updates_results(client):
    post(client, "V-1", "T004", 1, "2026-09-25T08:00:00")
    post(client, "V-2", "T004", 2, "2026-09-25T08:30:00")
    data = client.get("/api/runners/T004/splits").json()
    assert segment(data, 1, 2)["status"] == "ok"
    r = client.post("/api/checkins/V-2/revoke")
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"
    data = client.get("/api/runners/T004/splits").json()
    assert segment(data, 1, 2)["status"] == "pending"
    assert "站点 2" in segment(data, 1, 2)["reason"]


# ---------- 校验与错误 ----------

def test_duplicate_event_no_conflict(client):
    assert post(client, "X-1", "T005", 1, "2026-09-25T08:00:00").status_code == 201
    r = post(client, "X-1", "T005", 1, "2026-09-25T08:05:00")
    assert r.status_code == 409


def test_corrects_must_target_active_event(client):
    post(client, "Y-1", "T006", 1, "2026-09-25T08:00:00")
    client.post("/api/checkins/Y-1/revoke")
    assert post(client, "Y-2", "T006", 1, "2026-09-25T08:01:00", corrects="Y-1").status_code == 409
    assert post(client, "Y-3", "T006", 1, "2026-09-25T08:01:00", corrects="NOPE").status_code == 404


def test_revoke_twice_conflict(client):
    post(client, "Z-1", "T007", 1, "2026-09-25T08:00:00")
    assert client.post("/api/checkins/Z-1/revoke").status_code == 200
    assert client.post("/api/checkins/Z-1/revoke").status_code == 409
    assert client.post("/api/checkins/NOPE/revoke").status_code == 404


def test_station_out_of_range(client):
    assert post(client, "W-1", "T008", 7, "2026-09-25T08:00:00").status_code == 422
    assert post(client, "W-2", "T008", 0, "2026-09-25T08:00:00").status_code == 422


def test_unknown_runner_404(client):
    assert client.get("/api/runners/NOBODY/splits").status_code == 404


# ---------- 更正预演 ----------

def preview(client, event_no, runner, station, occurred_at, corrects=None):
    body = {
        "event_no": event_no,
        "runner_id": runner,
        "station_seq": station,
        "occurred_at": occurred_at,
    }
    if corrects is not None:
        body["corrects"] = corrects
    return client.post("/api/checkins/preview-correction", json=body)


def test_preview_correction_rolls_back(client):
    post(client, "P-1", "T100", 1, "2026-09-25T08:00:00")
    post(client, "P-2X", "T100", 2, "2026-09-25T12:00:00")  # 错误时间
    post(client, "P-3", "T100", 3, "2026-09-25T09:00:00")
    before = client.get("/api/runners/T100/splits").json()
    assert segment(before, 2, 3)["status"] == "pending"  # 时间倒退

    body = {
        "event_no": "P-2",
        "runner_id": "T100",
        "station_seq": 2,
        "occurred_at": "2026-09-25T08:30:00",
        "corrects": "P-2X",
    }
    r = client.post("/api/checkins/preview-correction", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["event_no"] == "P-2"
    assert data["corrects"] == "P-2X"
    assert [rn["runner_id"] for rn in data["runners"]] == ["T100"]
    rn = data["runners"][0]
    assert rn["before"]["complete"] is False
    assert rn["after"]["complete"] is False  # 仅 1-3 站，6 站赛道不完整
    # 仅列出发生变化的分段：1→2 与 2→3
    changed = {(c["from_station"], c["to_station"]): c for c in rn["changed_segments"]}
    assert set(changed) == {(1, 2), (2, 3)}
    # 1→2：更正前按错误的 12:00 算出 4 小时，更正后为 30 分钟
    assert changed[(1, 2)]["before"]["duration_seconds"] == 4 * 3600
    assert changed[(1, 2)]["after"]["duration_seconds"] == 30 * 60
    # 2→3：更正前时间倒退待定，更正后恢复为 30 分钟
    assert changed[(2, 3)]["before"]["status"] == "pending"
    assert changed[(2, 3)]["after"]["duration_seconds"] == 30 * 60

    # 预演整体回滚：事件号未占用、原事件状态未变、成绩未变
    assert client.get("/api/checkins/P-2").status_code == 404
    assert client.get("/api/checkins/P-2X").json()["status"] == "active"
    assert client.get("/api/runners/T100/splits").json() == before

    # 相同请求随后可正式提交，结果与预演一致
    assert client.post("/api/checkins", json=body).status_code == 201
    data = client.get("/api/runners/T100/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 30 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 30 * 60
    assert client.get("/api/checkins/P-2X").json()["status"] == "superseded"


def test_preview_reports_totals(client):
    times = ["08:00", "08:30", "09:00", "09:30", "10:00", "10:30"]
    for i, t in enumerate(times, start=1):
        assert post(client, f"F-{i}", "T300", i, f"2026-09-25T{t}:00").status_code == 201
    # 完整成绩 2:30:00；把第 6 站更正为 10:40 后总用时变为 2:40:00
    r = preview(client, "F-6B", "T300", 6, "2026-09-25T10:40:00", corrects="F-6")
    assert r.status_code == 200
    rn = r.json()["runners"][0]
    assert rn["before"] == {"complete": True, "total_seconds": 9000, "total": "2:30:00"}
    assert rn["after"] == {"complete": True, "total_seconds": 9600, "total": "2:40:00"}
    changed = {(c["from_station"], c["to_station"]): c for c in rn["changed_segments"]}
    assert set(changed) == {(5, 6)}
    assert changed[(5, 6)]["before"]["duration_seconds"] == 30 * 60
    assert changed[(5, 6)]["after"]["duration_seconds"] == 40 * 60
    # 回滚后成绩仍是 2:30:00
    assert client.get("/api/runners/T300/splits").json()["total"] == "2:30:00"


def test_preview_correction_cross_runner(client):
    post(client, "X-1", "TA", 1, "2026-09-25T08:00:00")
    post(client, "X-2", "TA", 2, "2026-09-25T08:40:00")
    # 更正把第 2 站改挂到选手 TB 名下：同时覆盖原选手 TA 与新选手 TB
    r = preview(client, "X-2B", "TB", 2, "2026-09-25T08:40:00", corrects="X-2")
    assert r.status_code == 200
    runners = {rn["runner_id"]: rn for rn in r.json()["runners"]}
    assert set(runners) == {"TA", "TB"}

    # TA 失去第 2 站：1→2 由 ok 变 pending，2→3 待定原因变化
    ta_changes = {
        (c["from_station"], c["to_station"]): c
        for c in runners["TA"]["changed_segments"]
    }
    assert set(ta_changes) == {(1, 2), (2, 3)}
    assert ta_changes[(1, 2)]["before"]["status"] == "ok"
    assert ta_changes[(1, 2)]["after"]["status"] == "pending"

    # TB 此前无任何记录：before 分段为 null，after 体现新打卡
    tb_changes = {
        (c["from_station"], c["to_station"]): c
        for c in runners["TB"]["changed_segments"]
    }
    assert tb_changes[(1, 2)]["before"] is None
    assert tb_changes[(1, 2)]["after"]["status"] == "pending"  # TB 缺第 1 站
    assert runners["TB"]["before"]["complete"] is False

    # 回滚：TB 仍无记录，TA 成绩未变
    assert client.get("/api/runners/TB/splits").status_code == 404
    assert segment(client.get("/api/runners/TA/splits").json(), 1, 2)["status"] == "ok"


def test_preview_requires_corrects(client):
    r = preview(client, "NP-1", "T200", 1, "2026-09-25T08:00:00")
    assert r.status_code == 422
    # 校验失败同样不落库
    assert client.get("/api/checkins/NP-1").status_code == 404


def test_preview_reuses_formal_validation(client):
    post(client, "Q-1", "T201", 1, "2026-09-25T08:00:00")
    client.post("/api/checkins/Q-1/revoke")
    base = {"runner_id": "T201", "occurred_at": "2026-09-25T08:05:00"}
    # 站点超出范围 → 422
    r = client.post("/api/checkins/preview-correction",
                    json={**base, "event_no": "Q-2", "station_seq": 7, "corrects": "Q-1"})
    assert r.status_code == 422
    # 事件号重复 → 409
    r = client.post("/api/checkins/preview-correction",
                    json={**base, "event_no": "Q-1", "station_seq": 1, "corrects": "Q-1"})
    assert r.status_code == 409
    # corrects 目标不存在 → 404
    r = client.post("/api/checkins/preview-correction",
                    json={**base, "event_no": "Q-3", "station_seq": 1, "corrects": "NOPE"})
    assert r.status_code == 404
    # corrects 目标非 active → 409
    r = client.post("/api/checkins/preview-correction",
                    json={**base, "event_no": "Q-4", "station_seq": 1, "corrects": "Q-1"})
    assert r.status_code == 409
    # 以上预演均未落库
    for ev in ("Q-2", "Q-3", "Q-4"):
        assert client.get(f"/api/checkins/{ev}").status_code == 404


def test_timezone_offsets_normalized(client):
    post(client, "TZ-1", "T009", 1, "2026-09-25T16:00:00+08:00")
    post(client, "TZ-2", "T009", 2, "2026-09-25T08:40:00+00:00")
    data = client.get("/api/runners/T009/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 40 * 60
