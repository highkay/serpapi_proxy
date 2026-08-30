# serpapi_proxy — SerpApi 多 Key 池 / 透明轮转代理

一个独立的 SerpApi key 池服务：接收验证过的 key（来自 harvester 扫描），自动校验
额度（`account.json`），对客户端提供透明轮转的 SerpApi 代理 — 自动换 key、429 冷却、
失效剔除。Python 3.11 + FastAPI + stdlib sqlite3，单容器部署，无外部数据库。

## 快速部署（Docker Compose）

仓库根准备 `.env`：

```bash
# 必填：Bearer 主密钥（管理员接口 + 代理转发共用）
MASTER_KEY=$(openssl rand -hex 32)

# 可选：pip 镜像加速构建（国内网络建议，如 fnos）
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

```bash
docker compose up -d --build
curl -s http://localhost:48081/healthz
# → {"status":"ok","keys":0,"active":0}
```

数据落在 `./data/pool.db`（bind mount `./data:/data`）。容器名 `serpapi-proxy`，
端口 `48081:8001`，含 30s 间隔 healthcheck。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MASTER_KEY` | 无（**必填**） | Bearer 主密钥；缺失直接退出 |
| `POOL_DB_PATH` | `./pool.db` | SQLite 路径（容器内建议 `/data/pool.db`） |
| `PORT` | `8001` | 监听端口 |
| `UPSTREAM_BASE` | `https://serpapi.com` | SerpApi 上游 |
| `REFRESH_INTERVAL_SECONDS` | `600` | 后台配额刷新线程周期（重查所有非 invalid key 的 `account.json`） |
| `PIP_INDEX_URL`（build arg） | 空 = 官方 PyPI | pip 镜像 |

## API 契约

**鉴权**：除 `GET /healthz` 外全部要求 `Authorization: Bearer <MASTER_KEY>`；
否则 `401 {"error":"unauthorized"}`。（中间件级校验，非路由依赖。）

| 方法 / 路径 | 说明 | 返回 |
|---|---|---|
| `GET /healthz` | 无鉴权健康检查 | `{"status":"ok","keys":N,"active":M}` |
| `POST /api/keys` | 新增 key；body `{"key": str, "alias": str?}` | 200 `{"id":id,"status":s}`；400 `{"error":"create_failed"}`（重复）或 `{"error":"invalid_key_format"}`（非 20–64 hex） |
| `GET /api/keys` | 全量列表 | `key_masked = key[:6]…key[-4:]`，**明文永不回显** |
| `DELETE /api/keys/{id}` | 删除 | 200 `{"deleted":id}` / 404 |
| `POST /api/keys/{id}/refresh` | 立即重查该 key 的 `account.json` | 200 行数据（掩码）/ 404 |
| `GET /` | HTML 状态页（掩码表格） | 200 |
| `GET /{path}` | **透明轮转代理**（catch-all） | 上游原样应答 |

要点：

- key 格式门：`^[0-9a-fA-F]{20,64}$`（真实 SerpApi key 为 64 hex）；存储统一小写。
- 重复 key 双重拦截：find-before-add + catch `sqlite3.IntegrityError`（防并发竞态），
  均返回 400 `create_failed`。
- **`POST /api/keys` 会同步调用 `account.json` 验证（≤25s 超时）再返回 200** —
  批量灌新 key 时客户端超时要放大（实测 410 key ≈ 8 分钟；重复推送 ≈ 5 秒，
  因为重复路径跳过验证）。验证结果直接落入库：`active` / `exhausted`（剩余
  searches ≤ 0）/ `invalid`（401）/ `unverified`（上游不可达，保留原状态）。

## 轮转语义（catch-all 代理）

- **选 key 顺序**：`unverified` 优先 → `searches_left` 多者优先 → 最近最少使用（LRU）→ id。
- **逐次尝试，最多 3 次**：
  - `ConnectionError` 等网络异常 → 该 key 冷却 10s，换下一个；
  - 上游 401 → 该 key 置 `invalid`，换下一个；
  - 上游 429 → 该 key 冷却 60s，换下一个；
  - 其它状态码（如 422 参数错误）→ **原样透传，不换 key**（真实参数错误不消耗轮换）。
- 全部不可用 → `503 {"error":"no_available_keys"}`。
- 入站 query 中的 `api_key` 一律剔除并替换为池选中的 key。
- 非 GET 方法落在非保留路径 → FastAPI 默认 405。

## 接入方：harvester 推送（已上线）

harvester-web 容器环境变量（harvester 仓库 `docker-compose.yml` 已含透传行）：

```bash
SERPAPI_PROXY_BASE_URL=http://192.168.1.18:48081   # fnos 宿主机：48081
SERPAPI_PROXY_AUTH_KEY=<与池相同的 MASTER_KEY>
```

行为（`web/serpapi_push.py`，每次 serpapi 扫描完成后自动触发）：

- 读 `valid-keys.txt` → 逐 key `POST /api/keys`，body `{"key":k,"alias":"harvester"}`；
- 200 → `added`；400 `create_failed`|`invalid_key_format` → `ignored`；401 → fail-fast；
  429/5xx → 退避重试（1/3/9s × 3）；结束写一行 `push_logs`。

手动验证整条链路：

```bash
docker exec harvester-web python -c \
  "from web.serpapi_push import get_serpapi_push_service; \
   get_serpapi_push_service().push_valid_keys('serpapi', 'e2e-check')"
# 预期: status=success added=0 ignored=0(或重复时>0)
docker exec harvester-web python -c \
  "import sqlite3; c=sqlite3.connect('/app/data/harvester.db'); \
   print(c.execute(\"SELECT status,keys_count,added_count,ignored_count \
     FROM push_logs WHERE run_id='e2e-check'\").fetchall())"
```

## 客户端调用

任何 SerpApi 路径都可以经池转发，额度耗尽自动换 key：

```bash
curl -s -H "Authorization: Bearer $MASTER_KEY" \
  "http://localhost:48081/search.json?engine=google&q=hello"
```

返回体 / 状态码与 SerpApi 原生一致（200 时才记 `last_used_at` 并参与 LRU）。

## 运维

- **备份**：`data/pool.db` 一个文件即全部状态，但**不要在运行中直接 `cp`** —
  拷贝可能横跨一次未完成的写提交，得到损坏文件。两种安全做法：
  1. 停机复制：`docker compose down && cp data/pool.db pool.db.bak && docker compose up -d`；
  2. 在线备份（不用停机，走 SQLite 热备份 API）：
     ```bash
     docker exec serpapi-proxy python -c \
       "import sqlite3; src=sqlite3.connect('/data/pool.db'); \
        dst=sqlite3.connect('/data/pool-backup.db'); src.backup(dst); dst.close()"
     # 备份文件落在宿主机 data/pool-backup.db（bind mount）
     ```
  按做法 1 换机迁移已实测零丢失（2026-08-30, 410→420 key）。
- **换 MASTER_KEY**：改 `.env` → `docker compose up -d`（自动 recreate）；
  同步更新所有接入方（harvester `SERPAPI_PROXY_AUTH_KEY`）。
- **监控**：`/healthz` 探针 + 容器 healthcheck（30s/10s/3 次）。
- **安全**：`account.json` 响应回显 `api_key` — 代码从不落库、不记日志、不回显；
  响应仅用掩码；`.env`/`*.db`/`data/` 均已 gitignore。鉴权是 ASGI 中间件 —
  不要改回 FastAPI 路由依赖写法（≥0.116 会静默忽略依赖返回的 Response）。
- **生产形态（fnos，2026-08-30 实测）**：仓库 `/home/admin/serpapi_proxy`，
  端口 `48081`，数据 `/home/admin/serpapi_proxy/data/pool.db`；池当前 ~420 key
  （229 active / 191 exhausted）；harvester 每次扫描完自动续灌。

## 测试

```bash
python -m unittest discover -s serpapi_proxy/tests -t .   # 27 tests
ruff check serpapi_proxy
pyright serpapi_proxy
```