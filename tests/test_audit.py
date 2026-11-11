"""结果核查解析器与逐包核查器的单元测试（不依赖网络）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.audit_parser import (
    PackageEntry,
    extract_catalog_links,
    extract_packages,
    normalize_package_no,
    parse_result_detail,
)
from app.models import PackageStatus, ResultKind
from app.reconciler import AuditInputs, AuditNotice, reconcile


# --------------------------------------------------------------------------- #
# 包号归一化
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("第3包", "3"),
        ("第 G1 标段", "G1"),
        ("包号：A-2", "A-2"),
        ("包 5", "5"),
        ("第12包组", "12"),
        ("分包编号 b7", "B7"),
        ("", None),
        ("无编号文本", None),
    ],
)
def test_normalize_package_no(raw, expected):
    assert normalize_package_no(raw) == expected


# --------------------------------------------------------------------------- #
# 原公告分包表
# --------------------------------------------------------------------------- #
PACKAGE_TABLE_HTML = """
<html><body>
<h1>测试项目招标公告</h1>
<table>
  <tr><th>项目编号</th><td>GSB-2026-100</td></tr>
</table>
<table>
  <thead><tr><th>包号</th><th>包名称</th><th>预算</th></tr></thead>
  <tbody>
    <tr><td>1</td><td>道路保洁</td><td>100</td></tr>
    <tr><td>第2包</td><td>垃圾清运</td><td>200</td></tr>
    <tr><td>G3</td><td>护栏清洗</td><td>300</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_extract_packages_from_table():
    assert extract_packages(PACKAGE_TABLE_HTML) == [
        ("1", "道路保洁"),
        ("2", "垃圾清运"),
        ("G3", "护栏清洗"),
    ]


def test_extract_packages_inline_fallback():
    html = """
    <html><body><h1>招标公告</h1>
    <p>本项目分三个标段实施：第1包为主城区服务，第2包为郊区服务，第 G3 标段为应急保障。</p>
    </body></html>
    """
    assert [no for no, _ in extract_packages(html)] == ["1", "2", "G3"]


def test_extract_packages_none_when_no_structure():
    # 没有任何包号线索时返回空列表，绝不臆造包号
    assert extract_packages("<html><body><h1>无分包公告</h1><p>整包采购</p></body></html>") == []


# --------------------------------------------------------------------------- #
# 目录页：分页与详情链接
# --------------------------------------------------------------------------- #
def test_catalog_separates_pagination_and_detail_links():
    html = """
    <html><body>
    <a href="/login.html">登录</a>
    <a href="https://example.gov/x.html">外站</a>
    <a href="/detail/a.html">第1包中标结果</a>
    <a href="/results/list-2.html">下一页</a>
    <a href="/results/list-2.html?page=3">3</a>
    <a href="/results/list.html">目录自身</a>
    </body></html>
    """
    details, pages = extract_catalog_links(html, "http://localhost/results/list.html")
    detail_urls = [u for u, _, _ in details]
    assert detail_urls == ["http://localhost/detail/a.html"]
    assert "http://localhost/results/list-2.html" in pages
    assert "http://localhost/results/list-2.html?page=3" in pages
    assert not any("login" in u or "example.gov" in u for u in detail_urls + pages)


# --------------------------------------------------------------------------- #
# 结果详情解析
# --------------------------------------------------------------------------- #
def test_parse_award_detail_single_package():
    html = """
    <html><head><title>测试项目中标结果公告</title></head><body>
    <h1>测试项目中标结果公告</h1>
    <ul>
      <li data-field="project-code" data-value="GSB-2026-100"></li>
      <li data-field="notice-no" data-value="JG01"></li>
      <li data-field="published-at" data-value="2026-11-08 10:00"></li>
      <li data-field="package-no" data-value="1"></li>
      <li data-field="winners" data-value="甲公司、乙公司"></li>
    </ul>
    </body></html>
    """
    d = parse_result_detail(html, "http://x/a.html")
    assert d.kind_hint == "award"
    assert d.notice_no == "JG01"
    assert d.published_at == datetime(2026, 11, 8, 10, 0)
    assert d.entries[0].package_no == "1"
    assert d.entries[0].winners == ["甲公司", "乙公司"]


def test_parse_multi_package_result_table():
    html = """
    <html><head><title>测试项目废标公告</title></head><body>
    <h1>测试项目废标公告</h1>
    <ul>
      <li data-field="project-code" data-value="GSB-2026-100"></li>
      <li data-field="notice-no" data-value="FB01"></li>
      <li data-field="published-at" data-value="2026-11-09 15:30"></li>
    </ul>
    <table>
      <thead><tr><th>包号</th><th>废标原因</th></tr></thead>
      <tbody>
        <tr><td>第6包</td><td>供应商不足三家</td></tr>
        <tr><td>第7包</td><td>计划调整</td></tr>
      </tbody>
    </table>
    </body></html>
    """
    d = parse_result_detail(html, "http://x/b.html")
    assert d.kind_hint == "reject"
    assert [(e.package_no, e.reject_reason) for e in d.entries] == [
        ("6", "供应商不足三家"),
        ("7", "计划调整"),
    ]


def test_parse_correction_without_ref_warns():
    html = """
    <html><head><title>测试项目中标结果更正公告</title></head><body>
    <h1>测试项目中标结果更正公告</h1>
    <p>项目编号：GSB-2026-100</p>
    <p>发布时间：2026-11-12 15:00</p>
    <p>包号：第1包</p>
    <p>中标单位：乙公司</p>
    </body></html>
    """
    d = parse_result_detail(html, "http://x/c.html")
    assert d.kind_hint == "correction"
    assert d.refers_to is None
    assert any("未引用原结果公告编号" in w for w in d.warnings)


# --------------------------------------------------------------------------- #
# 核查器：构造通知的小工具
# --------------------------------------------------------------------------- #
def _notice(
    kind,
    pkg,
    *,
    url,
    notice_no=None,
    refers_to=None,
    published=None,
    winners=None,
    reject_reason=None,
):
    from app.audit_parser import PackageEntry

    return AuditNotice(
        url=url,
        title=f"公告-{notice_no or url}",
        kind=kind,
        project_code="GSB-2026-100",
        notice_no=notice_no,
        refers_to=refers_to,
        published_at=published,
        entries=[PackageEntry(package_no=pkg, winners=winners or [], reject_reason=reject_reason)],
    )


def _by_no(packages, no):
    return next(p for p in packages if p.package_no == no)


_BASE_PACKAGES = [("1", "包一"), ("2", "包二"), ("3", "包三"), ("4", "包四")]


def test_valid_correction_replaces_older_conclusion():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            _notice(ResultKind.AWARD, "1", url="http://x/a1", notice_no="JG01",
                    published=datetime(2026, 11, 8), winners=["甲公司"]),
            _notice(ResultKind.CORRECTION, "1", url="http://x/g1", notice_no="GZ01",
                    refers_to="JG01", published=datetime(2026, 11, 12), winners=["乙公司"]),
        ],
    )
    packages, warnings = reconcile(inputs)
    p1 = _by_no(packages, "1")
    assert p1.status == PackageStatus.OK
    assert p1.winners == ["乙公司"]  # 更正后的单位替换原结论
    applied = [n for n in p1.notices if n.kind == ResultKind.CORRECTION]
    assert applied and applied[0].applied is True
    assert warnings == []


def test_correction_missing_reference_pending():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            _notice(ResultKind.CORRECTION, "2", url="http://x/g2", notice_no="GZ02",
                    refers_to="JG99-NOPE", published=datetime(2026, 11, 12), winners=["辛公司"]),
        ],
    )
    packages, warnings = reconcile(inputs)
    p2 = _by_no(packages, "2")
    assert p2.status == PackageStatus.PENDING
    assert any("不存在" in r for r in p2.reasons)
    assert any("更正公告未生效" in w for w in warnings)


def test_correction_not_later_pending():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            _notice(ResultKind.REJECT, "3", url="http://x/f1", notice_no="FB01",
                    published=datetime(2026, 11, 9), reject_reason="不足三家"),
            _notice(ResultKind.CORRECTION, "3", url="http://x/g3", notice_no="GZ03",
                    refers_to="FB01", published=datetime(2026, 11, 8), winners=["寅公司"]),
        ],
    )
    packages, _ = reconcile(inputs)
    p3 = _by_no(packages, "3")
    assert p3.status == PackageStatus.PENDING
    assert any("不晚于" in r for r in p3.reasons)


def test_parallel_awards_with_different_winners_conflict():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            _notice(ResultKind.AWARD, "1", url="http://x/a", notice_no="JG01",
                    published=datetime(2026, 11, 8), winners=["丙公司"]),
            _notice(ResultKind.AWARD, "1", url="http://x/b", notice_no="JG02",
                    published=datetime(2026, 11, 9), winners=["丁公司"]),
        ],
    )
    packages, _ = reconcile(inputs)
    p1 = _by_no(packages, "1")
    assert p1.status == PackageStatus.CONFLICT
    assert set(p1.winners) == {"丙公司", "丁公司"}


def test_parallel_awards_same_winner_is_ok():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            _notice(ResultKind.AWARD, "1", url="http://x/a", notice_no="JG01",
                    published=datetime(2026, 11, 8), winners=["丙公司"]),
            _notice(ResultKind.AWARD, "1", url="http://x/b", notice_no="JG02",
                    published=datetime(2026, 11, 9), winners=["丙公司"]),
        ],
    )
    packages, _ = reconcile(inputs)
    assert _by_no(packages, "1").status == PackageStatus.OK


def test_award_and_reject_together_conflict():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            _notice(ResultKind.AWARD, "2", url="http://x/a", notice_no="JG03",
                    published=datetime(2026, 11, 8), winners=["戊公司"]),
            _notice(ResultKind.REJECT, "2", url="http://x/f", notice_no="FB02",
                    published=datetime(2026, 11, 10), reject_reason="资格问题"),
        ],
    )
    packages, _ = reconcile(inputs)
    assert _by_no(packages, "2").status == PackageStatus.CONFLICT


def test_missing_package_when_catalog_complete():
    inputs = AuditInputs(expected_packages=_BASE_PACKAGES, notices=[])
    packages, _ = reconcile(inputs)
    p4 = _by_no(packages, "4")
    assert p4.status == PackageStatus.MISSING
    assert any("漏发" in r for r in p4.reasons)


def test_unseen_package_pending_not_missing_on_catalog_failure():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[],
        catalog_failures=["http://x/results/list-3.html"],
    )
    packages, _ = reconcile(inputs)
    assert all(p.status == PackageStatus.PENDING for p in packages)


def test_attributed_detail_failure_only_blocks_its_package():
    # 包 4 的详情 404 且可归属到包 4；目录读取完整，包 3 无任何记录 → 仍是漏发
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[],
        detail_failures=[("http://x/p6-missing.html", ("4",))],
    )
    packages, _ = reconcile(inputs)
    assert _by_no(packages, "4").status == PackageStatus.PENDING
    assert _by_no(packages, "3").status == PackageStatus.MISSING


def test_unattributed_detail_failure_blocks_all_empty_packages():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[],
        detail_failures=[("http://x/broken.html", ())],  # 无法归属 → 全局失败
    )
    packages, _ = reconcile(inputs)
    assert all(p.status == PackageStatus.PENDING for p in packages)


def test_catalog_incomplete_marks_unseen_packages_pending_not_missing():
    # 触达分页上限，目录未读全：未见结果的包不得判漏发
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[],
        catalog_incomplete=True,
    )
    packages, _ = reconcile(inputs)
    assert all(p.status == PackageStatus.PENDING for p in packages)
    assert not any(p.status == PackageStatus.MISSING for p in packages)


def test_reject_only_is_ok():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            _notice(ResultKind.REJECT, "1", url="http://x/f", notice_no="FB01",
                    published=datetime(2026, 11, 9), reject_reason="不足三家"),
        ],
    )
    packages, _ = reconcile(inputs)
    p1 = _by_no(packages, "1")
    assert p1.status == PackageStatus.OK
    assert p1.conclusion == "reject"


def test_orphan_package_does_not_invent_expected_package():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            _notice(ResultKind.REJECT, "9", url="http://x/f9", notice_no="FB9",
                    published=datetime(2026, 11, 9), reject_reason="计划调整"),
        ],
    )
    packages, warnings = reconcile(inputs)
    # 原公告的 4 个包照常出结论，包 9 不进入 PackageResult，只产生告警
    assert {p.package_no for p in packages} == {"1", "2", "3", "4"}
    assert any("包号 9" in w for w in warnings)


def test_undated_notice_forces_pending():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            _notice(ResultKind.AWARD, "1", url="http://x/a", notice_no="JG01",
                    published=None, winners=["甲公司"]),
        ],
    )
    packages, _ = reconcile(inputs)
    assert _by_no(packages, "1").status == PackageStatus.PENDING


def test_unknown_kind_notice_forces_pending():
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            AuditNotice(
                url="http://x/u", title="无法识别的公告", kind=ResultKind.UNKNOWN,
                project_code="GSB-2026-100", notice_no="X1", refers_to=None,
                published_at=datetime(2026, 11, 9),
                entries=[PackageEntry(package_no="1", winners=["甲公司"])],
            ),
        ],
    )
    packages, _ = reconcile(inputs)
    assert _by_no(packages, "1").status == PackageStatus.PENDING


def test_chained_corrections_apply_in_order():
    # 更正链：JG01（甲）→ GZ01（乙，引用 JG01）→ GZ02（丙，引用 GZ01），逐级更晚
    inputs = AuditInputs(
        expected_packages=_BASE_PACKAGES,
        notices=[
            _notice(ResultKind.AWARD, "1", url="http://x/a", notice_no="JG01",
                    published=datetime(2026, 11, 8), winners=["甲公司"]),
            _notice(ResultKind.CORRECTION, "1", url="http://x/g1", notice_no="GZ01",
                    refers_to="JG01", published=datetime(2026, 11, 10), winners=["乙公司"]),
            _notice(ResultKind.CORRECTION, "1", url="http://x/g2", notice_no="GZ02",
                    refers_to="GZ01", published=datetime(2026, 11, 12), winners=["丙公司"]),
        ],
    )
    packages, warnings = reconcile(inputs)
    p1 = _by_no(packages, "1")
    assert p1.status == PackageStatus.OK
    assert p1.winners == ["丙公司"]
    assert warnings == []
