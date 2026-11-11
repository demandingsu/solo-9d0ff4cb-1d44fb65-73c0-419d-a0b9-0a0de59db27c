"""FastAPI 入口：公告采集 API + 结果公示核查 API。

端点：
* GET  /                          演示页（可直接在浏览器发起采集/核查）
* POST /api/v1/collect            传入列表页 URL 与项目编号，返回合并后的投标状态
* GET  /api/v1/collect            同上的查询参数形式，便于浏览器/curl 快速演示
* POST /api/v1/verify             结果公示核查：原公告 URL + 结果目录 URL + 项目编号
* GET  /api/v1/verify             同上的查询参数形式
* GET  /healthz                   健康检查
"""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from app.audit_collector import verify
from app.collector import collect
from app.models import FetchRequest, FetchResponse, VerifyRequest, VerifyResponse

app = FastAPI(
    title="采购公告采集与结果核查 API",
    description="抓取同站点原公告与更正公告，按发布时间合并投标状态；并对结果公示逐包核查漏发与结论冲突。",
    version="2.0.0",
)


_DEMO_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>采购公告采集 / 结果核查 API 演示</title>
<style>
body { font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 2rem; max-width: 1020px; }
input { width: 32rem; max-width: 80vw; padding: .4rem; }
button { padding: .45rem 1.2rem; cursor: pointer; }
pre { background: #f4f4f6; padding: 1rem; border-radius: 8px; overflow:auto; max-height: 55vh; }
.hint { color: #666; font-size: .9rem; }
fieldset { margin-bottom: 1.5rem; border: 1px solid #ccd; border-radius: 10px; padding: 1rem 1.4rem; }
legend { font-weight: 600; padding: 0 .4rem; }
</style>
</head>
<body>
<h1>采购公告采集 / 结果核查 API 演示</h1>
<p class="hint">示例站点由 docker compose 中的 nginx 提供，默认地址 <code>http://localhost:8080/</code></p>

<fieldset>
<legend>① 投标状态采集（/api/v1/collect）</legend>
<form id="f1">
  <p><label>列表页 URL <input id="list_url" value="http://localhost:8080/list.html"></label></p>
  <p><label>项目编号 <input id="project_code" value="GSB-2026-001"></label></p>
  <button type="submit">采集并合并</button>
</form>
</fieldset>

<fieldset>
<legend>② 结果公示核查（/api/v1/verify）</legend>
<form id="f2">
  <p><label>原公告详情 URL <input id="original_url" value="http://localhost:8080/detail/r100-original.html"></label></p>
  <p><label>结果目录 URL <input id="catalog_url" value="http://localhost:8080/results/list.html"></label></p>
  <p><label>项目编号 <input id="verify_code" value="GSB-2026-100"></label></p>
  <button type="submit">逐包核查</button>
</form>
</fieldset>

<pre id="out">响应将显示在这里……</pre>
<script>
async function post(url, payload) {
  const out = document.getElementById('out');
  out.textContent = '请求中……';
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    out.textContent = JSON.stringify(await resp.json(), null, 2);
  } catch (err) {
    out.textContent = '请求失败：' + err;
  }
}
document.getElementById('f1').addEventListener('submit', (e) => {
  e.preventDefault();
  post('/api/v1/collect', {
    list_url: document.getElementById('list_url').value,
    project_code: document.getElementById('project_code').value
  });
});
document.getElementById('f2').addEventListener('submit', (e) => {
  e.preventDefault();
  post('/api/v1/verify', {
    original_url: document.getElementById('original_url').value,
    catalog_url: document.getElementById('catalog_url').value,
    project_code: document.getElementById('verify_code').value
  });
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def demo_page() -> str:
    return _DEMO_PAGE


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/collect", response_model=FetchResponse, tags=["collect"])
async def collect_post(req: FetchRequest) -> FetchResponse:
    return await collect(req)


@app.get("/api/v1/collect", response_model=FetchResponse, tags=["collect"])
async def collect_get(
    list_url: str = Query(..., description="采购公告列表页 URL"),
    project_code: str = Query(..., description="项目编号"),
    timeout: float = Query(10.0, ge=1.0, le=60.0),
) -> FetchResponse:
    return await collect(FetchRequest(list_url=list_url, project_code=project_code, timeout=timeout))


@app.post("/api/v1/verify", response_model=VerifyResponse, tags=["verify"])
async def verify_post(req: VerifyRequest) -> VerifyResponse:
    return await verify(req)


@app.get("/api/v1/verify", response_model=VerifyResponse, tags=["verify"])
async def verify_get(
    original_url: str = Query(..., description="原招标/采购公告详情 URL（读取包号清单）"),
    catalog_url: str = Query(..., description="结果公示目录 URL（沿同站分页抓取）"),
    project_code: str = Query(..., description="项目编号"),
    timeout: float = Query(10.0, ge=1.0, le=60.0),
) -> VerifyResponse:
    return await verify(
        VerifyRequest(
            original_url=original_url,
            catalog_url=catalog_url,
            project_code=project_code,
            timeout=timeout,
        )
    )
