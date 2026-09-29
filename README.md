# 低温光谱仪校准液路 · 带整数下界的最小费用流

在多个阀站间转运标准样品时，核对带最小/最大通量的管路能否同时满足各站净供需，
并在可行时给出**成本最低且可复算**的方案。浏览器录入（至多 12 站点、36 条有向管路），
提交后由真实 API 返回逐管路流量、总成本与最优性证据。

## 算法（全程精确整数，无浮点 / 无贪心 / 无固定轮次）

1. **消去下界**：令 `f = lower + x`，把下界预流量从各站净供需扣除，得到调整供需。
2. **可行流判定**：加超源/超汇，用 Dinic 在整数残量网络求最大流；
   超源侧全部饱和 ⇔ 存在可行流。不可行时返回三类失败证据：
   - 各站**未满足供应量 / 未满足需求量**（精确到站点与数量）；
   - 最终残量网络中从超源**可达的站点集合**（最小割的 S 侧）；
   - 横切该割、已被顶到上界的管路。
3. **消除负费用改进**：在原网络的**完整残量网络**（含正反向残量弧）上，
   反复消去 **Karp 最小均值负环**（Tarjan SCC + 精确分式比较，交叉相乘不用浮点），
   停止条件是"残量网络不存在负环"这一最优性判定本身。
4. **站点势证书**：Bellman-Ford 求整数势 `p`，使每条正残量弧 `a→b`
   满足 `cost + p[a] − p[b] ≥ 0`，即**每条正残量边约化费用非负**，
   API 逐边返回正反向残量与约化费用，可独立复算。

输入中 `balance` 正为净供应、负为净需求；总供需必须持平，否则连同非法边界
（负下界、上界小于下界等）一起返回定位到字段的错误列表。`1.5`、`true` 等
非整数严格拒绝。

## 稳定审计标识

- 请求带 `audit_key` 时，服务端对去掉该字段后的载荷做规范化 SHA-256 指纹并留档；
- **同载荷重传**：返回原始记录（`idempotent_replay`，含原创建时间），不重新求解；
- **同载荷并发提交**：只形成一份最终记录——首个请求求解并保存期间，
  其余同载荷请求等待其完成后复用同一创建时间与完整结果；
  若首个请求求解失败/异常，预约即被回滚，后续同标识请求重新求解，
  不会残留可永久回放的半成品；
- **改载荷复用同一标识**：HTTP 409 `audit_conflict` 拒绝（含在途期间，立即拒绝），
  原记录不受影响；
- 记录可经 `GET /api/audit/{key}` 取回。

## 运行（Docker Compose）

```bash
cp .env.example .env          # 可选：修改 HOST_PORT（默认 8080）
docker compose up -d web
# 浏览器打开 http://localhost:${HOST_PORT:-8080}
```

健康检查：`GET /healthz`（Compose 已配置 healthcheck）。

一次性核对（求解器测试 + 镜像构建 + 本题 API 冒烟），`verify` 容器在
`web` 健康后启动、自行退出并以退出码报告：

```bash
docker compose up --abort-on-container-exit --exit-code-from verify verify
# 或：docker compose run verify；echo $?
```

## 本地开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests -q          # 48 项测试（含 100 个满规模 fuzz 算例与并发审计用例）
uvicorn app.main:app --port 8080
python scripts/verify_all.py       # 需先启动服务（WEB_BASE_URL 可覆盖）
```

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/solve` | 求解；返回 feasible/infeasible、逐边流量、势与约化费用 |
| `GET` | `/api/audit/{key}` | 取回审计留档 |
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/` | 录入与证据查看页面 |

请求体示例：

```json
{
  "nodes": [{"id": "S", "balance": 6}, {"id": "T", "balance": -6}],
  "edges": [{"id": "e1", "from": "S", "to": "T",
             "lower": 0, "upper": 6, "cost": 2}],
  "audit_key": "run-2026-09-28-01"
}
```
