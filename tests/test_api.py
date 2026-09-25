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


def test_timezone_offsets_normalized(client):
    post(client, "TZ-1", "T009", 1, "2026-09-25T16:00:00+08:00")
    post(client, "TZ-2", "T009", 2, "2026-09-25T08:40:00+00:00")
    data = client.get("/api/runners/T009/splits").json()
    assert segment(data, 1, 2)["duration_seconds"] == 40 * 60
