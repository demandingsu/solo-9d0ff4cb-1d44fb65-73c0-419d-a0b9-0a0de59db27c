# 采购公告采集与结果核查 API

服务包含两组能力：

1. **投标状态采集（`/api/v1/collect`）**：传入采购公告**列表页 URL** 与项目编号，
   自动抓取该页指向的同站点原公告与更正公告，从详情页提取发布时间、投标截止时间与
   取消采购信息，按发布时间合并后返回当前是否可投标、有效截止时间、项目状态及来源链接。
2. **结果公示核查（`/api/v1/verify`）**：传入**原公告详情 URL、结果目录 URL 与项目编号**，
   从原公告分包表读取包号清单，沿同站分页目录抓取该项目的中标、废标详情及结果更正，
   按包号与公告编号关联，逐包输出结论、漏发/冲突原因与全部来源链接。

---

## 一、结果公示核查（`/api/v1/verify`）

### 核查规则

| 规则 | 实现位置 |
| --- | --- |
| 包号清单只从**原公告分包表/正文**读取，保持顺序去重，绝不凭空增删包 | `app/audit_parser.py::extract_packages` |
| 从结果目录出发沿**同站分页链接** BFS 逐页抓取（上限 20 页，触顶标记目录不完整） | `app/audit_collector.py::_crawl_catalog` |
| 详情按**项目编号**归属（详情页编号 + 目录行上下文双重判定），其他项目结果自动过滤 | `app/audit_collector.py` |
| 更正公告须**引用同包已有结果公告编号且发布时间更晚**，才替换该包结论；引用缺失/编号不存在/发布不晚 → 更正不生效，该包 `pending` | `app/reconciler.py::_validate_correction` |
| 并列中标结果的**中标单位不同** → `conflict`（单位一致不视为冲突） | `app/reconciler.py` |
| 同包**中标与废标结论互斥**且无有效更正衔接 → `conflict` | `app/reconciler.py` |
| 目录分页或关联详情**读取失败**、更正**引用缺失** → 相关包 `pending` 待核实 | `app/reconciler.py` |
| **未见结果 ≠ 漏发**：只有目录全部读取成功且该包确无任何结果，才判 `missing` 漏发 | `app/reconciler.py` |
| 详情失败可按目录行**归属到具体包**时只影响该包；无法归属的失败视为全局性失败 | `app/audit_collector.py + reconciler.py` |
| 结果里出现原公告未列示的包号 → 仅告警留痕，不生成该包结论、不据此改判其他包 | `app/reconciler.py` |

### 逐包状态（`packages[].status`）

- `ok`：结论一致且链路完整（`conclusion=award` 中标/成交，或 `reject` 废标/流标）；
- `conflict`：并列中标单位不同，或中标与废标互斥，`reasons` 附冲突双方来源链接；
- `missing`：疑似漏发（原公告列示、目录读全、确无结果）；
- `pending`：待核实（读取失败、更正引用缺失/不生效、发布时间缺失等），**不把未见包断言为漏发**。

项目级 `status` 按 `conflict > pending > missing > ok` 汇总取最严重状态。

### 请求与响应

```bash
curl -X POST http://localhost:8000/api/v1/verify \
  -H 'Content-Type: application/json' \
  -d '{
    "original_url": "http://localhost:8080/detail/r100-original.html",
    "catalog_url":  "http://localhost:8080/results/list.html",
    "project_code": "GSB-2026-100"
  }'
```

GET 形式：

```bash
curl "http://localhost:8000/api/v1/verify?original_url=...&catalog_url=...&project_code=GSB-2026-100"
```

响应骨架：

```json
{
  "project_code": "GSB-2026-100",
  "status": "conflict",
  "packages": [
    {
      "package_no": "1",
      "status": "ok",
      "conclusion": "award",
      "winners": ["乙环境科技集团股份有限公司"],
      "reasons": [],
      "notices": [
        { "kind": "award", "notice_no": "...JG01", "applied": true, "url": "..." },
        { "kind": "correction", "notice_no": "...GZ01", "refers_to": "...JG01",
          "applied": true, "url": "..." }
      ]
    }
  ],
  "warnings": ["关联结果详情读取失败：http://.../r100-p6-missing.html"],
  "sources": [
    { "role": "original", "url": "..." },
    { "role": "catalog", "url": "..." },
    { "role": "award", "url": "..." },
    { "role": "failed", "url": "...", "title": "读取失败-待核实" }
  ]
}
```

### 示例项目 GSB-2026-100 的七个包（`sample-site/results/`）

| 包号 | 场景 | 预期结论 |
| --- | --- | --- |
| 1 | 中标 JG01 → 更正 GZ01（引用 JG01 且发布更晚，更换中标单位） | `ok`，采信更正后的乙公司 |
| 2 | 两条并列中标公告，单位不同且无更正衔接 | `conflict`，列出两家单位与来源 |
| 3 | 先中标后废标，废标公告未引用中标公告 | `conflict`（中标/废标互斥） |
| 4 | 目录两页全部读取成功，无任何结果公示 | `missing` 疑似漏发 |
| 5 | 更正引用的公告编号在同包不存在 | `pending`，更正 `applied=false` |
| 6 | 目录有链接但详情页已下线（真实 404） | `pending`，失败链接进 `sources` |
| 7 | 废标 FB02 + 更正（引用 FB02 但发布时间更早） | `pending`，更正不生效、废标不被替换 |

另有：其他项目 `GSB-2026-099` 的结果（验证项目过滤）、原公告未列示的“第9包”
（验证不臆造包号，仅告警）、外站/登录干扰链接（验证同站过滤）。

---

## 二、投标状态采集（`/api/v1/collect`）

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 只采集列表页指向的**同站点**详情链接，外站链接、登录/导航链接自动忽略 | `app/parser.py::extract_detail_links` |
| 按**发布时间**从早到晚合并原公告与更正公告 | `app/merger.py::merge_notices` |
| 更正公告**未提及截止时间**时，沿用此前已生效的截止时间 | `app/merger.py` |
| 更正公告给出新截止时间（如延期）时，以新时间覆盖 | `app/merger.py` |
| 公告明确**取消采购** → 状态 `closed`、不可投标，不再返回有效截止时间（旧时间仅在公告明细中保留） | `app/merger.py` |
| **任一关联详情页**抓取/解析失败（404、超时、结构异常等）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，**绝不把旧时间当作最终期限** | `app/merger.py` + `app/collector.py` |
| 发布时间缺失、缺少原公告、全部公告均无截止时间等信息链不完整情况，同样标记 `pending` | `app/merger.py` |

### 项目状态（`status`）

- `open`：可投标，`effective_deadline` 为合并后的有效截止时间；
- `closed`：采购已取消，`can_bid=false`，无有效截止时间；
- `pending`：待核实，存在关联页面失败或信息不完整，`effective_deadline=null`；
- `not_found`：列表页未发现与该项目编号相关的公告。

## 目录结构

```
.
├── app/
│   ├── main.py           # FastAPI 入口、路由、浏览器演示页（collect + verify）
│   ├── collector.py      # 采集编排：列表发现 → 并发抓取 → 归属判定 → 合并
│   ├── parser.py         # HTML 解析：同站链接发现、三层字段提取、取消识别
│   ├── merger.py         # 投标状态合并规则（时间继承、延期覆盖、取消、待核实）
│   ├── audit_parser.py   # 核查解析：分包表提取、目录分页发现、结果详情字段提取
│   ├── audit_collector.py# 核查编排：原公告包号 → 分页目录 BFS → 详情抓取归属
│   ├── reconciler.py     # 逐包核查：更正校验、并列/互斥冲突、漏发与待核实
│   └── models.py         # 请求/响应 Pydantic 模型
├── sample-site/          # 本地示例公告站点（nginx 静态托管）
│   ├── list.html         # 投标公告列表页（含外站/登录干扰链接）
│   ├── results/          # 结果公示目录（list.html + list-2.html 分页）
│   └── detail/           # 原公告/更正/取消 + 7 个包的中标/废标/结果更正详情
├── nginx/default.conf    # 示例站点 nginx 配置（缺失页面返回真实 404）
├── tests/                # 单测 + 本地静态服务器端到端测试（71 个用例）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 采集/核查 API | http://localhost:8000 | Swagger 文档：http://localhost:8000/docs |
| 浏览器演示页 | http://localhost:8000/ | 两个表单分别发起采集与逐包核查 |
| 示例公告站点 | http://localhost:8080/list.html | nginx 托管的静态采购公告 |
| 示例结果目录 | http://localhost:8080/results/list.html | 含分页的结果公示目录 |

停止：`docker compose down`。

## API 用法

### POST（推荐）

```bash
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'
```

### GET（便于浏览器快速验证）

```bash
curl "http://localhost:8000/api/v1/collect?list_url=http://localhost:8080/list.html&project_code=GSB-2026-001"
```

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "status": "open",
  "can_bid": true,
  "effective_deadline": "2026-11-15T14:00:00",
  "cancelled": false,
  "notices": [ { "kind": "original", "published_at": "...", "deadline": "..." } ],
  "sources": [ { "role": "list", "url": "..." }, { "role": "correction", "url": "..." } ],
  "warnings": []
}
```

## 示例站点内置的三个演示项目

| 项目编号 | 场景 | 预期结果 |
| --- | --- | --- |
| `GSB-2026-001` | 原公告 → 澄清更正（**未提截止时间，沿用原时间**）→ 延期更正（**新时间覆盖**） | `open`，有效截止时间 `2026-11-15 14:00` |
| `GSB-2026-002` | 原公告 → 取消公告（预算收回） | `closed`，不可投标，无有效截止时间 |
| `GSB-2026-003` | 原公告 → 更正页**已下线（404）** | `pending` 待核实，`effective_deadline=null`，来源中列出失败链接 |

另外列表页还包含：外站链接、登录导航链接、其他项目公告（`GSB-2026-099`）、
无关项目的坏链接（`GSB-2025-888`），用于验证同站过滤、项目归属判定与失败隔离。

## 详情页解析策略（三层回退）

1. **结构化标记**：`data-field="published-at|deadline|cancelled|..."` 元素或
   `<meta property="article:published_time">`；
2. **标签匹配**：`发布时间 / 投标截止时间 / 响应文件递交截止时间 / 取消原因` 等
   中文标签，兼容 `th/td`、`dt/dd`、`li/p` 等布局，支持
   `2026-11-10 09:30`、`2026年11月15日 14时00分` 等常见写法；
3. **正文正则兜底**：在含“截止/递交/提交/受理”等时限语义的块级元素中识别日期，
   并排除“发布时间”句、资质业绩说明中的历史日期（如“近三年（2023年1月1日起）”）。

取消采购识别支持 `data-field="cancelled"` 标记、取消原因标签以及
“本项目取消采购 / 终止本次招标”等显式句式。

> 接入真实采购平台时，若页面结构不同，优先在详情页模板中补 `data-field` 标记，
> 或在 `app/parser.py` 顶部的标签/关键词元组中登记该平台的字段叫法。

## 本地开发与测试（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 A：托管示例公告页
cd sample-site && python -m http.server 8080
# 终端 B：启动 API
uvicorn app.main:app --reload --port 8000

# 测试（自带静态服务器，无需外部依赖）
pytest -q
```

## 设计说明

- 列表页抓取失败直接返回 `502`；详情页失败不影响整体响应，而是按业务规则沉淀为
  `pending` + 失败来源链接，便于投标专员人工核实。
- 关联归属判定结合详情页项目编号与列表行上下文（含编号列），
  即使详情页下线（只能看到列表行）也能正确判定失败是否与目标项目相关；
  无关项目的坏链接不会污染查询结果。
- 详情页并发抓取（上限 5），统一超时、自定义 UA、跟随重定向。
