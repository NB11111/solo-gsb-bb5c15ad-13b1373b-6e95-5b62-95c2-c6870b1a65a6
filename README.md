# 越野赛分段计时 API

基于 FastAPI + SQLite 的越野赛分段计时服务：

- 接收带**唯一事件号**、选手号、站点序号、发生时间的打卡；
- 支持按事件号**更正**（新事件取代原事件）与**撤销**；
- 支持**更正预演**：按正式更正相同的校验与规则试算前后结果，整体回滚不落库；
- 同一站点多条有效打卡取**最早时间**；
- 按站点顺序重算相邻站分段用时，**乱序补传**可填补缺段，更正 / 撤销即时更新结果；
- 相邻站缺打卡或**时间倒退**时，该分段标记为 `pending`（待定）并说明原因；
- 提供选手分段结果查询。

## 启动

```bash
docker compose up --build
```

一条命令即可：容器启动时自动建表，数据库为空时写入示例数据（选手 R001–R006，覆盖完整成绩、乱序补传、缺站、更正、撤销、时间倒退等场景）。

- 本地端口：**8000**
- API 地址：http://localhost:8000
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/health

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STATION_COUNT` | `6` | 赛道站点总数，分段为 1→2 … (N-1)→N |
| `DB_PATH` | 容器内 `/data/timing.db`，本地 `timing.db` | SQLite 数据库文件路径 |

Compose 通过命名卷 `timing-data` 持久化数据；删除卷（`docker compose down -v`）后重启会重新初始化并播种示例数据。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/checkins` | 登记打卡；携带 `corrects` 字段即为更正指定事件 |
| `POST` | `/api/checkins/preview-correction` | 更正预演：试算前后结果并整体回滚，不落库 |
| `POST` | `/api/checkins/{event_no}/revoke` | 按事件号撤销打卡 |
| `GET` | `/api/checkins?runner_id=` | 打卡列表（含已更正 / 已撤销记录） |
| `GET` | `/api/checkins/{event_no}` | 按事件号查询单条打卡 |
| `GET` | `/api/runners/{runner_id}/splits` | 选手分段结果 |

### 示例

登记打卡：

```bash
curl -X POST http://localhost:8000/api/checkins \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"E1001","runner_id":"R100","station_seq":1,"occurred_at":"2026-09-25T08:00:00"}'
```

更正（`E1002` 取代 `E1001`）：

```bash
curl -X POST http://localhost:8000/api/checkins \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"E1002","runner_id":"R100","station_seq":1,"occurred_at":"2026-09-25T08:05:00","corrects":"E1001"}'
```

撤销：

```bash
curl -X POST http://localhost:8000/api/checkins/E1002/revoke
```

更正预演（`corrects` 必填，指向当前有效事件；请求体与正式更正完全相同）。
下面把种子数据中 R004 第 2 站的打卡 `S-R004-2`（08:40）预演更正为 08:50：

```bash
curl -X POST http://localhost:8000/api/checkins/preview-correction \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"E1003","runner_id":"R004","station_seq":2,"occurred_at":"2026-09-25T08:50:00","corrects":"S-R004-2"}'
```

响应（每位受影响选手给出更正前后的完成状态、总用时，以及仅发生变化的分段）：

```json
{
  "event_no": "E1003",
  "runner_id": "R004",
  "station_seq": 2,
  "occurred_at": "2026-09-25T08:50:00Z",
  "corrects": "S-R004-2",
  "runners": [
    {
      "runner_id": "R004",
      "before": {"complete": true, "total_seconds": 13680, "total": "3:48:00"},
      "after": {"complete": true, "total_seconds": 13680, "total": "3:48:00"},
      "changed_segments": [
        {
          "from_station": 1,
          "to_station": 2,
          "before": {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 2280, "duration": "0:38:00", "reason": null},
          "after": {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 2880, "duration": "0:48:00", "reason": null}
        },
        {
          "from_station": 2,
          "to_station": 3,
          "before": {"from_station": 2, "to_station": 3, "status": "ok", "duration_seconds": 2400, "duration": "0:40:00", "reason": null},
          "after": {"from_station": 2, "to_station": 3, "status": "ok", "duration_seconds": 1800, "duration": "0:30:00", "reason": null}
        }
      ]
    }
  ]
}
```

说明：

- 预演复用正式更正的全部校验（站点范围 422、事件号重复 409、`corrects` 目标不存在 404 / 非有效状态 409），且 `corrects` 必填（缺失返回 422）；
- 预演在事务保存点内试算后**整体回滚**：不占用事件号、不改变任何打卡状态或成绩，**相同请求随后可原样正式提交**到 `POST /api/checkins`；
- 更正跨选手时（新事件的 `runner_id` 与被更正事件不同），`runners` 会同时包含原选手与新选手；选手此前无记录时 `before` 分段为 `null`。

查询分段结果：

```bash
curl http://localhost:8000/api/runners/R001/splits
```

响应节选：

```json
{
  "runner_id": "R001",
  "complete": true,
  "total_seconds": 12600,
  "total": "3:30:00",
  "segments": [
    {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 1920, "duration": "0:32:00"},
    {"from_station": 3, "to_station": 4, "status": "pending", "reason": "站点 4 缺少有效打卡"}
  ]
}
```

## 本地开发

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # 默认监听 8000 端口
pytest                               # 运行测试
```

## 实现说明

- 打卡记录永久保留，更正 / 撤销只改变状态（`active` / `superseded` / `revoked`），便于审计；
- 分段结果在查询时基于全部有效打卡实时重算，因此补传、更正、撤销立即反映到结果中；
- 更正预演与正式更正共用同一套校验与写入逻辑：预演在 `SAVEPOINT` 内套用写入、重算前后结果后 `ROLLBACK TO` 回滚，数据库不留任何痕迹；
- 时间统一归一化为 UTC 存储，无时区的时间按 UTC 处理；
- 更正与写入在同一事务中完成，事件号唯一性由数据库主键保证（重复返回 409）。
