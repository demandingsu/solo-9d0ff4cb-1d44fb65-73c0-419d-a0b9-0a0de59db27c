"""结果核查 API 端到端测试：对 sample-site 启动本地静态服务器跑完整核查链路。"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample-site"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def static_server():
    handler = functools.partial(_QuietHandler, directory=str(SAMPLE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _verify(client, base):
    resp = client.post(
        "/api/v1/verify",
        json={
            "original_url": f"{base}/detail/r100-original.html",
            "catalog_url": f"{base}/results/list.html",
            "project_code": "GSB-2026-100",
            "timeout": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _pkg(data, no):
    return next(p for p in data["packages"] if p["package_no"] == no)


def test_full_audit_overall_conflict_and_all_seven_packages(client, static_server):
    data = _verify(client, static_server)
    assert {p["package_no"] for p in data["packages"]} == {"1", "2", "3", "4", "5", "6", "7"}
    # 存在冲突包 → 项目级汇总为 conflict
    assert data["status"] == "conflict"


def test_package_1_valid_correction_replaces_winner(client, static_server):
    data = _verify(client, static_server)
    p1 = _pkg(data, "1")
    assert p1["status"] == "ok"
    assert p1["conclusion"] == "award"
    assert p1["winners"] == ["乙环境科技集团股份有限公司"]
    # 原中标公告与生效更正都在留痕中，且更正标记 applied=true
    kinds = {n["kind"] for n in p1["notices"]}
    assert "award" in kinds and "correction" in kinds
    corr = next(n for n in p1["notices"] if n["kind"] == "correction")
    assert corr["applied"] is True
    assert corr["refers_to"] == "GSB-2026-100-JG01"


def test_package_2_parallel_awards_conflict(client, static_server):
    data = _verify(client, static_server)
    p2 = _pkg(data, "2")
    assert p2["status"] == "conflict"
    assert set(p2["winners"]) == {"丙清运服务有限公司", "丁环卫集团有限公司"}
    assert any("中标单位不一致" in r for r in p2["reasons"])
    # 冲突原因须附来源链接
    assert all("http://" in r for r in p2["reasons"])


def test_package_3_award_vs_reject_conflict(client, static_server):
    data = _verify(client, static_server)
    p3 = _pkg(data, "3")
    assert p3["status"] == "conflict"
    assert any("中标与废标" in r for r in p3["reasons"])


def test_package_4_missing_publication(client, static_server):
    data = _verify(client, static_server)
    p4 = _pkg(data, "4")
    assert p4["status"] == "missing"
    assert p4["notices"] == []
    assert any("漏发" in r for r in p4["reasons"])


def test_package_5_dangling_correction_pending(client, static_server):
    data = _verify(client, static_server)
    p5 = _pkg(data, "5")
    assert p5["status"] == "pending"
    assert any("GSB-2026-100-JG99-NOPE" in r for r in p5["reasons"])
    corr = p5["notices"][0]
    assert corr["applied"] is False


def test_package_6_detail_404_pending_and_listed_as_failed_source(client, static_server):
    data = _verify(client, static_server)
    p6 = _pkg(data, "6")
    assert p6["status"] == "pending"
    assert any("待核实" in r for r in p6["reasons"])
    failed = [s for s in data["sources"] if s["role"] == "failed"]
    assert any("r100-p6-missing.html" in s["url"] for s in failed)
    # 404 只归属包 6，不影响包 4 的漏发判定（上一用例已验证包 4=missing）
    assert any("读取失败" in w for w in data["warnings"])


def test_package_7_early_correction_keeps_reject_pending(client, static_server):
    data = _verify(client, static_server)
    p7 = _pkg(data, "7")
    # 早于原结果发布的更正不生效 → 信息链待核实，废标结论不被替换
    assert p7["status"] == "pending"
    assert any("不晚于" in r for r in p7["reasons"])


def test_sources_include_original_catalog_and_roles(client, static_server):
    data = _verify(client, static_server)
    roles = {s["role"] for s in data["sources"]}
    assert {"original", "catalog", "award", "reject", "correction", "failed"} <= roles
    # 两个分页目录都应留痕
    catalogs = [s["url"] for s in data["sources"] if s["role"] == "catalog"]
    assert len(catalogs) == 2
    # 外站与登录链接不出现
    all_urls = [s["url"] for s in data["sources"]]
    assert not any("example.gov" in u or "login" in u for u in all_urls)


def test_other_project_results_are_filtered_out(client, static_server):
    data = _verify(client, static_server)
    # GSB-2026-099 的成交结果不得混入本项目任何包
    for pkg in data["packages"]:
        for notice in pkg["notices"]:
            assert "GSB-2026-099" not in notice["url"]
    assert not any("other-result" in s["url"] for s in data["sources"])


def test_orphan_package_number_only_warns(client, static_server):
    data = _verify(client, static_server)
    # 第9包不在原公告分包表中：只告警，不生成对应 PackageResult
    assert "9" not in {p["package_no"] for p in data["packages"]}
    assert any("包号 9" in w for w in data["warnings"])


def test_get_endpoint_works_the_same(client, static_server):
    resp = client.get(
        "/api/v1/verify",
        params={
            "original_url": f"{static_server}/detail/r100-original.html",
            "catalog_url": f"{static_server}/results/list.html",
            "project_code": "GSB-2026-100",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "conflict"


def test_original_fetch_failure_returns_502(client):
    resp = client.post(
        "/api/v1/verify",
        json={
            "original_url": "http://127.0.0.1:1/no-original.html",
            "catalog_url": "http://127.0.0.1:1/results.html",
            "project_code": "X",
            "timeout": 2,
        },
    )
    assert resp.status_code == 502


def test_catalog_failure_marks_every_package_pending(client, static_server):
    # 原公告可读取，但目录指向不存在的分页入口 → 全部包待核实，不得判漏发
    resp = client.post(
        "/api/v1/verify",
        json={
            "original_url": f"{static_server}/detail/r100-original.html",
            "catalog_url": f"{static_server}/results/does-not-exist.html",
            "project_code": "GSB-2026-100",
            "timeout": 5,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert all(p["status"] == "pending" for p in data["packages"])
    assert not any(p["status"] == "missing" for p in data["packages"])
